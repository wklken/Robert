import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests.support import review_coordinator as review_fixture
from tests.support import vote_coordinator as vote_fixture
from robert_agent.workflows.review import coordinator as review_coordinator
from robert_agent.workflows.vote import coordinator as vote_coordinator


class DirectModelExecutionTests(unittest.TestCase):
    def test_importing_stage_executor_does_not_load_workflow_implementations(self):
        script = """
import sys
from robert_agent.execution import stages as model_execution

workflow_modules = {
    'native_rounds',
    'review_coordinator', 'review_files', 'review_store', 'review_workspace',
    'vote_coordinator', 'vote_files', 'vote_store', 'vote_workspace',
}
loaded = workflow_modules & {name.removeprefix('robert_agent.') for name in sys.modules}
assert not loaded, f'stage executor loaded workflows: {sorted(loaded)}'
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def no_system_sandbox(self):
        original = shutil.which

        def which(command, *args, **kwargs):
            if command in {"bwrap", "sandbox-exec"}:
                return None
            return original(command, *args, **kwargs)

        return patch.object(shutil, "which", side_effect=which)

    def test_review_defaults_run_without_system_sandbox(self):
        fixture = review_fixture.ReviewCoordinatorFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.cycle([fixture.event()])
        task, attempt = fixture.current()
        calls = []

        def launch(**kwargs):
            self.assertTrue(Path(kwargs["trusted_environment"]["TMPDIR"]).is_dir())
            calls.append(kwargs["trusted_environment"]["ROBERT_REVIEW_OUTPUT_DIR"])
            return fixture.launcher(**kwargs)

        with (
            self.no_system_sandbox(),
            patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "", "CODEX_HOME": ""}),
        ):
            driver = review_coordinator.Coordinator(
                fixture.db, task, attempt, fetch_pr=fixture.pr, launcher=launch
            )
            report = driver.run()
        self.assertTrue(report.is_file())
        self.assertEqual(len(calls), 4)  # prepare, two reviewers, synthesis

    def test_vote_defaults_run_without_system_sandbox(self):
        fixture = vote_fixture.VoteCoordinatorFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.fixture.cycle([fixture.event()])
        task, attempt = fixture.fixture.current()
        calls = []

        def launch(**kwargs):
            self.assertTrue(Path(kwargs["trusted_environment"]["TMPDIR"]).is_dir())
            calls.append(kwargs["trusted_environment"]["ROBERT_VOTE_OUTPUT_DIR"])
            return fixture.launcher(**kwargs)

        with (
            self.no_system_sandbox(),
            patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "", "CODEX_HOME": ""}),
        ):
            driver = vote_coordinator.Coordinator(
                fixture.db, task, attempt, source_reader=fixture.source, launcher=launch
            )
            report = driver.run()
        self.assertTrue(report.is_file())
        self.assertEqual(len(calls), 4)

    def test_optional_claude_config_supports_atomic_updates_with_direct_cli(self):
        from robert_agent.adapters.processes.environment import stage_environment

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            output, config = root / "output", root / "claude"
            output.mkdir()
            config.mkdir()
            (config / ".claude.json").write_text('{"existing": true}')
            with patch.dict(
                os.environ,
                {"CLAUDE_CONFIG_DIR": str(config), "CODEX_HOME": str(root / "codex")},
            ):
                env = stage_environment(
                    {
                        "context_dir": str(root / "context"),
                        "worktree_path": str(root / "tree"),
                    },
                    output,
                    root / "data",
                    workflow_name="review",
                )
            script = """import json,os
from pathlib import Path
config=Path(os.environ['CLAUDE_CONFIG_DIR'])
assert json.loads((config/'.claude.json').read_text())['existing']
(config/'.claude.json.lock').touch()
(config/'.claude.json.tmp.123.456').write_text('{}')
(config/'.claude.json.tmp.123.456').replace(config/'.claude.json')
Path(os.environ['ROBERT_REVIEW_OUTPUT_DIR'],'result').write_text('done')
Path(os.environ['TMPDIR'],'scratch').write_text('temporary')
"""
            completed = subprocess.run(
                [sys.executable, "-c", script],
                env={**os.environ, **env},
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(env["CODEX_HOME"], str(root / "codex"))
            self.assertEqual(json.loads((config / ".claude.json").read_text()), {})
            self.assertEqual((output / "result").read_text(), "done")
            self.assertEqual(Path(env["TMPDIR"], "scratch").read_text(), "temporary")

    def test_optional_claude_config_cannot_overlap_inputs_or_robert_data(self):
        from robert_agent.adapters.processes.environment import stage_environment

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            output = root / "output"
            output.mkdir()
            manifest = {
                "context_dir": str(root / "context"),
                "worktree_path": str(root / "tree"),
            }
            for config in (root, root / "tree", root / "data" / "private"):
                config.mkdir(parents=True, exist_ok=True)
                with (
                    self.subTest(config=config),
                    patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config)}),
                ):
                    with self.assertRaisesRegex(RuntimeError, "must not overlap"):
                        stage_environment(
                            manifest, output, root / "data", workflow_name="vote"
                        )
