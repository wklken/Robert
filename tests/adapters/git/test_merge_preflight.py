"""Merge prerequisite refusals happen without executing hooks or changing Git."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent.adapters.git import merge_preflight as module


SIMPLE_WRAPPER = """#!/bin/sh
if [ "$SKIP_SIMPLE_GIT_HOOKS" = "1" ]; then
    echo "[INFO] SKIP_SIMPLE_GIT_HOOKS is set to 1, skipping hook."
    exit 0
fi
if [ -f "$SIMPLE_GIT_HOOKS_RC" ]; then
    . "$SIMPLE_GIT_HOOKS_RC"
fi
"""


class MergePreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "NODE_", "NPM_", "npm_"))}
        self.env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, PATH=str(self.bin))
        self.git_executable = shutil.which("git")
        (self.bin / "git").symlink_to(self.git_executable)
        self.git("init", "-b", "main")
        self.git("config", "user.useConfigOnly", "true")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        self.hooks = self.repo / ".git/hooks"

    def git(self, *args):
        return subprocess.run(
            [self.git_executable, *args], cwd=self.repo, env=self.env, text=True, capture_output=True, check=True
        ).stdout.strip()

    def executable(self, path, body):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        path.chmod(0o755)
        return path

    def hook(self, body, name="commit-msg"):
        return self.executable(self.hooks / name, body)

    def node_hook(self, *, wrapper=True):
        self.hook(
            (SIMPLE_WRAPPER if wrapper else "#!/bin/sh\n") + "cd src/dashboard-front && npx commitlint --edit $1\n"
        )
        front = self.repo / "src/dashboard-front"
        front.mkdir(parents=True, exist_ok=True)
        (front / "package.json").write_text(
            json.dumps(
                {
                    "engines": {"node": ">=20.19.3"},
                    "devDependencies": {"@commitlint/cli": "^19.8.1", "@commitlint/config-conventional": "^19.8.1"},
                    "simple-git-hooks": {"commit-msg": "cd src/dashboard-front && npx commitlint --edit $1"},
                }
            )
        )
        (front / ".commitlintrc.cjs").write_text("module.exports = {extends: ['@commitlint/config-conventional']};\n")
        for package, bin_value in [
            ("@commitlint/cli", {"commitlint": "cli.js"}),
            ("@commitlint/config-conventional", None),
        ]:
            directory = front / "node_modules" / package
            directory.mkdir(parents=True)
            manifest = {"name": package, "version": "19.8.1", "engines": {"node": ">=v18"}}
            if bin_value:
                manifest["bin"] = bin_value
                self.executable(directory / "cli.js", '#!/usr/bin/env node\nthrow new Error("NEVER EXECUTE");\n')
            (directory / "package.json").write_text(json.dumps(manifest))
        local_bin = front / "node_modules/.bin"
        local_bin.mkdir()
        (local_bin / "commitlint").symlink_to("../@commitlint/cli/cli.js")
        self.executable(self.bin / "node", '#!/bin/sh\nprintf "v22.16.0\\n"\n')
        self.executable(self.bin / "npx", '#!/usr/bin/env node\nthrow new Error("NEVER EXECUTE");\n')
        return front

    def check(self):
        return module.check_merge_environment(self.repo, env=self.env)

    def test_valid_identity_no_hooks_preserves_config_index_and_environment(self):
        before = (self.repo / ".git/config").read_bytes()
        environment = dict(self.env)
        self.assertIsNone(self.check())
        self.assertEqual((self.repo / ".git/config").read_bytes(), before)
        self.assertEqual(self.env, environment)
        self.assertFalse((self.repo / ".git/index").exists())
        self.assertFalse((self.repo / ".git/MERGE_HEAD").exists())

    def test_git_author_and_committer_effective_identities_are_both_checked(self):
        for name in ["GIT_AUTHOR_NAME", "GIT_COMMITTER_EMAIL"]:
            with self.subTest(name=name):
                self.env[name] = ""
                with self.assertRaisesRegex(module.MergePreflightError, "identity"):
                    self.check()
                self.env.pop(name)

    def test_missing_config_identity_refuses_without_setting_it(self):
        self.git("config", "--unset", "user.email")
        before = (self.repo / ".git/config").read_bytes()
        with self.assertRaisesRegex(module.MergePreflightError, "identity"):
            self.check()
        self.assertEqual((self.repo / ".git/config").read_bytes(), before)

    def test_effective_env_identity_can_supply_missing_local_config(self):
        self.git("config", "--unset", "user.name")
        self.git("config", "--unset", "user.email")
        self.env.update(
            GIT_AUTHOR_NAME="Author",
            GIT_AUTHOR_EMAIL="author@example.invalid",
            GIT_COMMITTER_NAME="Committer",
            GIT_COMMITTER_EMAIL="committer@example.invalid",
        )
        self.assertIsNone(self.check())

    def test_unknown_hook_is_refused_and_never_executed(self):
        sentinel = self.root / "side-effect"
        self.hook(f'#!/bin/sh\ntouch "{sentinel}"\nsecret_token=private-material\n')
        with self.assertRaisesRegex(module.MergePreflightError, "commit-msg.*dependencies") as caught:
            self.check()
        self.assertNotIn("private-material", str(caught.exception))
        self.assertFalse(sentinel.exists())

    def test_nonexecutable_hook_is_ignored_as_git_ignores_it(self):
        self.hook("#!/bin/sh\nunknown-side-effect\n").chmod(0o644)
        self.assertIsNone(self.check())

    def test_effective_relative_hooks_path_and_declared_interpreter(self):
        self.git("config", "core.hooksPath", "custom-hooks")
        self.hooks = self.repo / "custom-hooks"
        self.hook("#!/usr/bin/env unavailable-shell\nexit 0\n")
        with self.assertRaisesRegex(module.MergePreflightError, "interpreter"):
            self.check()

    def test_real_simple_git_hooks_commitlint_dependencies_present(self):
        self.node_hook()
        self.assertIsNone(self.check())

    def test_missing_node_refuses_before_npx_or_hook_execution(self):
        self.node_hook()
        (self.bin / "node").unlink()
        with self.assertRaisesRegex(module.MergePreflightError, "node.*PATH"):
            self.check()

    def test_missing_local_commitlint_refuses_network_install_fallback(self):
        front = self.node_hook()
        (front / "node_modules/.bin/commitlint").unlink()
        with self.assertRaisesRegex(module.MergePreflightError, "commitlint.*install"):
            self.check()

    def test_node_engine_mismatch_is_actionable(self):
        self.node_hook()
        self.executable(self.bin / "node", '#!/bin/sh\nprintf "v18.20.0\\n"\n')
        with self.assertRaisesRegex(module.MergePreflightError, "node.*version"):
            self.check()

    def test_dynamic_hook_init_is_refused_without_sourcing(self):
        self.node_hook()
        rc = self.root / "init.sh"
        rc.write_text("private-token=credential\n")
        self.env["SIMPLE_GIT_HOOKS_RC"] = str(rc)
        with self.assertRaisesRegex(module.MergePreflightError, "initialization") as caught:
            self.check()
        self.assertNotIn(str(rc), str(caught.exception))

    def test_node_options_are_not_executed_or_exposed(self):
        self.node_hook()
        self.env["NODE_OPTIONS"] = "--require=/private/credential.js"
        with self.assertRaisesRegex(module.MergePreflightError, "NODE_OPTIONS") as caught:
            self.check()
        self.assertNotIn("credential.js", str(caught.exception))

    def test_git_timeout_is_bounded_and_sanitized(self):
        with patch.object(
            module, "run_command", side_effect=subprocess.TimeoutExpired("git", 1, stderr="private-material")
        ):
            with self.assertRaisesRegex(module.MergePreflightError, "timed out") as caught:
                self.check()
        self.assertNotIn("private-material", str(caught.exception))

    def test_actual_target_commitlint_config_is_static_and_supported(self):
        front = self.node_hook()
        (front / ".commitlintrc.cjs").write_text("""module.exports = {
  ignores: [commit => commit.includes('init')],
  extends: ['@commitlint/config-conventional'],
  rules: {'type-enum': [2, 'always', ['feat', 'fix', 'chore',]],},
};
""")
        self.assertIsNone(self.check())

    def test_dynamic_commitlint_config_is_refused_without_execution(self):
        front = self.node_hook()
        (front / ".commitlintrc.cjs").write_text(
            "module.exports = {extends: ['@commitlint/config-conventional'], rules: getRules()};"
        )
        with self.assertRaisesRegex(module.MergePreflightError, "dynamic or unknown"):
            self.check()

    def test_missing_transitive_package_refuses_before_runtime_probe(self):
        front = self.node_hook()
        manifest = front / "node_modules/@commitlint/cli/package.json"
        content = json.loads(manifest.read_text())
        content["dependencies"] = {"@commitlint/load": "^19.8.1"}
        manifest.write_text(json.dumps(content))
        with self.assertRaisesRegex(module.MergePreflightError, "runtime dependency.*missing"):
            self.check()

    def test_all_merge_hooks_are_inspected_but_pre_commit_is_not_run_by_merge(self):
        self.hook("#!/bin/sh\nunknown-side-effect\n", "pre-commit")
        self.assertIsNone(self.check())
        for name in ["pre-merge-commit", "prepare-commit-msg", "post-merge"]:
            with self.subTest(name=name):
                self.hook("#!/bin/sh\nunknown-side-effect\n", name)
                with self.assertRaisesRegex(module.MergePreflightError, name + ".*dependencies"):
                    self.check()
                (self.hooks / name).unlink()

    def test_runtime_probe_and_git_queries_are_the_only_processes(self):
        self.node_hook()
        calls = []
        original = module.run_command

        def record(args, **kwargs):
            calls.append((args, kwargs))
            return original(args, **kwargs)

        with patch.object(module, "run_command", side_effect=record):
            self.check()
        self.assertEqual(
            [args[1:] for args, _ in calls],
            [
                ["var", "GIT_AUTHOR_IDENT"],
                ["var", "GIT_COMMITTER_IDENT"],
                ["rev-parse", "--path-format=absolute", "--git-path", "hooks"],
                ["--version"],
            ],
        )
        self.assertTrue(all(0 < kwargs["timeout"] <= 10 for _, kwargs in calls))
        self.assertFalse((self.repo / ".git/index").exists())

    def test_real_husky_v9_effective_driver_and_commitlint_payload(self):
        front = self.node_hook(wrapper=False)
        # Same commands relocated into the actual payload; effective Git hook is Husky's dispatcher.
        self.hooks = self.repo / ".husky/_"
        self.git("config", "core.hooksPath", ".husky/_")
        self.hook(module._HUSKY_WRAPPER + "\n")
        self.executable(self.hooks / "h", module._HUSKY_DRIVER + "\n")
        (self.hooks.parent / "commit-msg").write_text('cd src/dashboard-front && npx commitlint --edit "$1"\n')
        for name in ["sh", "basename", "dirname"]:
            (self.bin / name).symlink_to(shutil.which(name))
        self.env["XDG_CONFIG_HOME"] = str(self.root / "config")
        self.assertIsNone(self.check())
        # Both dispatcher and payload use the exact same local package environment.
        (front / "node_modules/.bin/commitlint").unlink()
        with self.assertRaisesRegex(module.MergePreflightError, "commitlint.*install"):
            self.check()

    def test_husky_modified_driver_and_init_are_refused_without_sourcing(self):
        self.node_hook(wrapper=False)
        self.hooks = self.repo / ".husky/_"
        self.git("config", "core.hooksPath", ".husky/_")
        self.hook(module._HUSKY_WRAPPER + "\n")
        self.executable(self.hooks / "h", module._HUSKY_DRIVER + "\n")
        (self.hooks.parent / "commit-msg").write_text('npx commitlint --edit "$1"\n')
        for name in ["sh", "basename", "dirname"]:
            (self.bin / name).symlink_to(shutil.which(name))
        self.env["XDG_CONFIG_HOME"] = str(self.root / "config")
        startup = self.root / "config/husky/init.sh"
        self.executable(startup, "#!/bin/sh\nunknown-side-effect\n")
        with self.assertRaisesRegex(module.MergePreflightError, "initialization"):
            self.check()
        startup.unlink()
        (self.hooks / "h").write_text(module._HUSKY_DRIVER + "\nunknown-side-effect\n")
        with self.assertRaisesRegex(module.MergePreflightError, "unknown Husky"):
            self.check()

    def test_environment_preflight_never_bypasses_configured_hooks(self):
        self.node_hook()
        self.env["SKIP_SIMPLE_GIT_HOOKS"] = "1"
        with self.assertRaisesRegex(module.MergePreflightError, "disables hooks"):
            self.check()

    def test_unknown_engine_expression_refuses_instead_of_guessing(self):
        front = self.node_hook()
        manifest = front / "package.json"
        value = json.loads(manifest.read_text())
        value["engines"]["node"] = "some-custom-version-policy"
        manifest.write_text(json.dumps(value))
        with self.assertRaisesRegex(module.MergePreflightError, "version.*unsupported"):
            self.check()

    def test_nonregular_hook_is_refused_without_blocking(self):
        os.mkfifo(self.hooks / "commit-msg", 0o755)
        with self.assertRaisesRegex(module.MergePreflightError, "unreadable"):
            self.check()

    def test_interpreter_flags_with_unknown_execution_are_refused(self):
        self.hook("#!/bin/sh -c arbitrary-command\nexit 0\n")
        with self.assertRaisesRegex(module.MergePreflightError, "interpreter arguments"):
            self.check()

    def test_node_package_cycle_is_bounded_without_execution(self):
        front = self.node_hook()
        cli = front / "node_modules/@commitlint/cli/package.json"
        value = json.loads(cli.read_text())
        value["dependencies"] = {"@commitlint/config-conventional": "^19.8.1"}
        cli.write_text(json.dumps(value))
        preset = front / "node_modules/@commitlint/config-conventional/package.json"
        value = json.loads(preset.read_text())
        value["dependencies"] = {"@commitlint/cli": "^19.8.1"}
        preset.write_text(json.dumps(value))
        self.assertIsNone(self.check())

    def test_absolute_hooks_path_uses_actual_target_hook_not_default(self):
        self.hook("#!/bin/sh\nunknown-side-effect\n")
        external = self.root / "actual-hooks"
        external.mkdir()
        self.executable(external / "commit-msg", "#!/bin/sh\nexit 0\n")
        self.git("config", "core.hooksPath", str(external))
        self.assertIsNone(self.check())

    def test_broken_executable_npx_is_actionable(self):
        self.node_hook()
        (self.bin / "npx").unlink()
        with self.assertRaisesRegex(module.MergePreflightError, "npx.*PATH"):
            self.check()

    def test_git_failure_stderr_is_never_in_error(self):
        with patch.object(
            module, "run_command", side_effect=subprocess.CalledProcessError(128, "git", stderr="private-material")
        ):
            with self.assertRaises(module.MergePreflightError) as caught:
                self.check()
        self.assertNotIn("private-material", str(caught.exception))

    def test_shell_initialization_refuses_without_sourcing(self):
        self.node_hook()
        self.env["BASH_ENV"] = "/private/credential-init.sh"
        with self.assertRaisesRegex(module.MergePreflightError, "shell initialization") as caught:
            self.check()
        self.assertNotIn("credential-init.sh", str(caught.exception))

    def test_node_probe_timeout_is_sanitized_and_leaves_no_merge_state(self):
        self.node_hook()
        original = module.run_command

        def timeout_node(args, **kwargs):
            if args[1:] == ["--version"]:
                self.assertTrue(0 < kwargs["timeout"] <= 10)
                raise subprocess.TimeoutExpired(args, kwargs["timeout"], stderr="private-material")
            return original(args, **kwargs)

        with patch.object(module, "run_command", side_effect=timeout_node):
            with self.assertRaisesRegex(module.MergePreflightError, "timed out") as caught:
                self.check()
        self.assertNotIn("private-material", str(caught.exception))
        self.assertFalse((self.repo / ".git/MERGE_HEAD").exists())

    def test_linked_worktree_uses_common_hooks_and_keeps_local_identity(self):
        # The adapter must inspect the actual assigned worktree, not a chosen repo cwd.
        self.git("commit", "--allow-empty", "-m", "initial")
        tree = self.root / "assigned"
        self.git("worktree", "add", "-b", "assigned", str(tree))
        self.hook("#!/bin/sh\nunknown-side-effect\n")
        with self.assertRaisesRegex(module.MergePreflightError, "commit-msg.*dependencies"):
            module.check_merge_environment(tree, env=self.env)
        (self.hooks / "commit-msg").unlink()
        before = (self.repo / ".git/config").read_bytes()
        self.assertIsNone(module.check_merge_environment(tree, env=self.env))
        self.assertEqual(before, (self.repo / ".git/config").read_bytes())

    def test_nonfinite_or_unbounded_timeout_is_not_used(self):
        for value in [0, -1, float("inf"), float("nan")]:
            with self.subTest(value=value):
                with self.assertRaisesRegex(module.MergePreflightError, "timeout"):
                    module.check_merge_environment(self.repo, env=self.env, timeout_seconds=value)
