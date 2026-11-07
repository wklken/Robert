"""Credential boundaries exercised by the restricted repair probe."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent.adapters.processes import worker as dispatch
from robert_agent.workflows.maintenance import repair as supervision_repair
from robert_agent.workflows.maintenance import worker as supervision_repair_worker


# Portable process fixture for the existing Robert DB/config/Git denials. Any
# remaining file reads are real. The offline repair simulation uses OS isolation.
_LAUNCHER = """
import errno, json, os, sys
from pathlib import Path

scope = json.loads(sys.argv[1])
hidden = [Path(value) for value in scope['hidden']]
opaque = set(map(Path, scope['opaque']))
original_open = os.open
original_listdir = os.listdir

def denied(path):
    return Path(path) in opaque or any(Path(path).is_relative_to(item) for item in hidden)

def open_file(path, *args, **kwargs):
    if denied(path):
        raise PermissionError(errno.EACCES, 'fixture denial')
    return original_open(path, *args, **kwargs)

def list_dir(path):
    if denied(path):
        raise PermissionError(errno.EACCES, 'fixture denial')
    return original_listdir(path)

os.open = open_file
os.listdir = list_dir
assert sys.argv[2] == '--'
assert sys.argv[4:6] == ['-I', '-c']
program = sys.argv[6]
sys.argv = ['-c', *sys.argv[7:]]
exec(program, {'__name__': '__main__'})
"""


class RepairCredentialIsolationTests(unittest.TestCase):
    def test_probe_rejects_readable_controller_credential_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "source"
            tree_root = root / "trees"
            tree = tree_root / "repair"
            data = root / "runtime"
            controller_home = root / "controller-home"
            gh_override = root / "custom-gh"
            xdg_config = root / "xdg"
            for path in (source, tree, data, controller_home):
                path.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", "--template=", str(source)], check=True)
            database = data / "state.sqlite3"
            config_file = root / "config.json"
            for path in (database, config_file):
                path.touch()
            launcher = root / "launcher.py"
            launcher.write_text(_LAUNCHER)
            config = {
                "db_path": str(database),
                "data_dir": str(data),
                "config_path": str(config_file),
            }
            old_denials = [data, config_file, source, tree_root]
            linked_gh = root / "linked-gh"
            backing_gh = root / "backing-gh"
            linked_gh.symlink_to(backing_gh, target_is_directory=True)
            cases = (
                ({}, controller_home / ".config" / "gh" / "hosts.yml"),
                ({"GH_CONFIG_DIR": str(gh_override)}, gh_override / "hosts.yml"),
                ({"XDG_CONFIG_HOME": str(xdg_config)}, xdg_config / "gh" / "hosts.yml"),
                (
                    {"XDG_CONFIG_HOME": str(xdg_config)},
                    xdg_config / "git" / "credentials",
                ),
                ({"GH_CONFIG_DIR": str(linked_gh)}, backing_gh / "hosts.yml"),
                ({}, controller_home / ".git-credentials"),
                ({}, controller_home / ".ssh" / "fixture_identity"),
            )
            for overrides, exposed in cases:
                with self.subTest(store=exposed.relative_to(root)):
                    exposed.parent.mkdir(parents=True, exist_ok=True)
                    exposed.write_text("fixture canary")
                    # Hide even the directory itself while leaving the known
                    # file readable, as an execute-only directory might.
                    with patch.dict(
                        os.environ,
                        {"HOME": str(controller_home), **overrides},
                        clear=True,
                    ):
                        scope = {
                            "hidden": list(map(str, old_denials)),
                            "opaque": [str(exposed.parent)],
                        }
                        if exposed.parent == backing_gh:
                            scope["hidden"].append(str(linked_gh))
                        authorization = {
                            "repo": {
                                "repo_root": str(source),
                                "worktree_root": str(tree_root),
                            },
                            "policy": {
                                "launcher": [
                                    sys.executable,
                                    str(launcher),
                                    json.dumps(scope),
                                ]
                            },
                            "worker": {"environment_allowlist": []},
                        }
                        try:
                            rejected = supervision_repair.probe_restricted_launcher(
                                config, tree, authorization=authorization
                            )
                            self.assertFalse(rejected["ok"], rejected)
                            scope["hidden"].append(str(exposed))
                            authorization["policy"]["launcher"][-1] = json.dumps(scope)
                            accepted = supervision_repair.probe_restricted_launcher(
                                config, tree, authorization=authorization
                            )
                            self.assertTrue(accepted["ok"], accepted)
                        finally:
                            exposed.unlink()
                            if exposed.parent != controller_home:
                                exposed.parent.rmdir()

    def test_trusted_broker_receives_store_paths_and_inner_environment_drops_them(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            paths = {
                "HOME": str(root / "controller"),
                "GH_CONFIG_DIR": str(root / "gh"),
                "XDG_CONFIG_HOME": str(root / "xdg"),
            }
            worker = {"timeout_seconds": 5, "environment_allowlist": ["MODEL_FIXTURE"]}
            profile = supervision_repair_worker.launch_profile(
                {"config_path": str(root / "config.json")}, "task", "attempt", worker
            )
            with patch.dict(
                os.environ,
                {**paths, "GH_TOKEN": "fixture env marker", "MODEL_FIXTURE": "enabled"},
                clear=True,
            ):
                broker = dispatch._worker_environment(profile["environment_allowlist"])
                for key, value in paths.items():
                    self.assertEqual(broker.get(key), value)
                self.assertNotIn("GH_TOKEN", broker)
                with patch.dict(os.environ, broker, clear=True):
                    inner = supervision_repair.repair_environment({"worker": worker}, root / "private")
                self.assertEqual(inner["HOME"], str(root / "private"))
                self.assertEqual(inner["MODEL_FIXTURE"], "enabled")
                self.assertNotIn("GH_CONFIG_DIR", inner)
                self.assertNotIn("XDG_CONFIG_HOME", inner)
