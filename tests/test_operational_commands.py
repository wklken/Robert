from contextlib import closing, redirect_stdout
import io
import json
import os
import shlex
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tests.support import PACKAGE_ROOT


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT = PACKAGE_ROOT


class OperationalCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_daemon_resource_key_uses_configured_repo_set(self):
        from robert_agent import daemon_state

        config_result = {
            "repos": [
                {"full_name": "Org/repo-b"},
                {"full_name": "Org/repo-a"},
            ]
        }

        key = daemon_state.repo_id_for_config(config_result)

        self.assertEqual(key, "config:Org/repo-a,Org/repo-b")

    def test_loop_engine_local_work_accepts_repo_id_list(self):
        from robert_agent import loop_engine
        from robert_agent import storage

        data_dir = self.root / "data"
        repo_root = self.root / "repo-a"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        worktree_root = repo_root / ".worktrees"
        worktree_root.mkdir()
        db_path = data_dir / "dd.sqlite3"
        storage.init_database(db_path)
        now = "2026-07-04T00:00:00+00:00"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(
                "INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root) VALUES ('repo:a', 'Org/a', 'robot', 'main', ?, ?)",
                (str(repo_root), str(worktree_root)),
            )
            conn.execute(
                "INSERT INTO wakeups(wakeup_id, repo_id, reason, dedupe_key, status, not_before_at, created_at, updated_at) VALUES ('wakeup-a', 'repo:a', 'manual_operator_request', 'manual-a', 'pending', ?, ?, ?)",
                (now, now, now),
            )
            self.assertTrue(loop_engine.has_runnable_local_work(conn, repo_ids=["repo:a"]))
            self.assertFalse(loop_engine.has_runnable_local_work(conn, repo_ids=["repo:b"]))

    def test_known_workstream_context_uses_latest_dd_mention_only(self):
        from robert_agent import discover
        class Completed:
            def __init__(self, stdout):
                self.stdout = stdout

        def runner(args, **_kwargs):
            if args[:3] == ["gh", "api", "repos/example/repo/issues/456/comments"]:
                return Completed(
                    json.dumps(
                        [
                            {
                                "id": "comment-mention",
                                "user": {"login": "wklken"},
                                "author_association": "OWNER",
                                "body": "@robot please retry the original task",
                                "created_at": "2026-06-23T01:00:00Z",
                            },
                            {
                                "id": "comment-no-mention",
                                "user": {"login": "wklken"},
                                "author_association": "OWNER",
                                "body": "this is just a status update",
                                "created_at": "2026-06-23T02:00:00Z",
                            },
                        ]
                    )
                )
            if args[:3] in [
                ["gh", "api", "repos/example/repo/pulls/456/reviews"],
                ["gh", "api", "repos/example/repo/pulls/456/comments"],
            ]:
                return Completed("[]")
            raise AssertionError(args)

        raw = {
            "id": "notification-1",
            "number": 456,
            "source_type": "pull_request",
            "event_type": "notification",
            "actor_login": "github",
        }
        result = discover._enrich_known_workstream_context(
            raw,
            {"full_name": "example/repo", "github_account": "robot"},
            runner,
        )

        self.assertEqual(result["id"], "comment-mention")
        self.assertEqual(result["event_fingerprint"], "comment:comment-mention")
        self.assertIn("@robot", result["body"])

    def test_worktree_plans_new_branch_from_upstream_base(self):
        from robert_agent import worktree
        result = worktree.plan_worktree(
            repo_root=self.root / "repo",
            worktree_root=self.root / "repo" / ".worktrees",
            source_number=123,
            short_slug="fix-timeout",
            base_branch="master",
            dry_run=True,
        )

        self.assertEqual(result["status"], "planned")
        self.assertEqual(result["branch_name"], "codex/dd-123-fix-timeout")
        self.assertEqual(result["start_point"], "upstream/master")
        self.assertIn("git fetch upstream", result["commands"][0])
        self.assertIn("git worktree add", result["commands"][1])

    def test_worktree_reuses_existing_pr_head_branch(self):
        from robert_agent import worktree
        result = worktree.plan_worktree(
            repo_root=self.root / "repo",
            worktree_root=self.root / "repo" / ".worktrees",
            source_number=456,
            short_slug="review-fix",
            base_branch="master",
            existing_pr_head_branch="codex/dd-123-fix-timeout",
            dry_run=True,
        )

        self.assertEqual(result["branch_name"], "codex/dd-123-fix-timeout")
        self.assertEqual(result["mode"], "reuse_existing_pr")

    def test_worktree_reuses_existing_worktree_when_branch_already_checked_out(self):
        from robert_agent import worktree
        repo_root = self.root / "repo"
        repo_root.mkdir()
        worktree_root = repo_root / ".worktrees"
        worktree_root.mkdir()

        original_run = worktree.subprocess.run

        class Completed:
            def __init__(self, stdout):
                self.stdout = stdout

        def fake_run(args, **kwargs):
            self.assertEqual(args, ["git", "worktree", "list", "--porcelain"])
            self.assertEqual(kwargs["cwd"], repo_root)
            return Completed(
                """worktree /tmp/repo\nHEAD abc\nbranch refs/heads/master\n\nworktree /tmp/repo/.worktrees/issue-2878-mcp-proxy\nHEAD def\nbranch refs/heads/codex/issue-2878-mcp-proxy\n\n"""
            )

        try:
            worktree.subprocess.run = fake_run
            result = worktree.plan_worktree(
                repo_root=repo_root,
                worktree_root=worktree_root,
                source_number=456,
                short_slug="review-fix",
                base_branch="master",
                existing_pr_head_branch="codex/issue-2878-mcp-proxy",
                dry_run=True,
            )
        finally:
            worktree.subprocess.run = original_run

        self.assertEqual(result["mode"], "reuse_existing_worktree")
        self.assertEqual(result["worktree_path"], "/tmp/repo/.worktrees/issue-2878-mcp-proxy")
        self.assertEqual(result["commands"], ["reuse existing worktree /tmp/repo/.worktrees/issue-2878-mcp-proxy"])

    def test_review_worktree_fetches_pr_head(self):
        from robert_agent import worktree
        result = worktree.plan_review_worktree(
            repo_root=Path("/tmp/repo"),
            worktree_root=Path("/tmp/repo/.worktrees"),
            source_number=707,
            short_slug="Review Feature PR",
            base_branch="master",
            dry_run=True,
        )

        self.assertEqual(result["mode"], "review_pr")
        self.assertEqual(result["branch_name"], "review/pr-707-review-feature-pr")
        self.assertEqual(result["worktree_path"], "/tmp/repo/.worktrees/review__pr-707-review-feature-pr")
        self.assertEqual(result["start_point"], "upstream/master")
        self.assertEqual(
            result["commands"],
            [
                "git fetch upstream master",
                "git fetch upstream pull/707/head:review/pr-707-review-feature-pr",
                "git worktree add /tmp/repo/.worktrees/review__pr-707-review-feature-pr review/pr-707-review-feature-pr",
            ],
        )

    def test_dispatch_builds_worker_command_with_prompt_and_worktree(self):
        from robert_agent import dispatch
        prompt_path = self.root / "task" / "prompt.md"
        prompt_path.parent.mkdir()
        prompt = "- prompt that starts like an option\nquotes: ' \" ` $() ; | --"
        prompt_path.write_text(prompt, encoding="utf-8")
        command = dispatch.build_worker_command(
            prompt_path=prompt_path,
            worktree_path=Path("/tmp/repo/.worktrees/dd-123"),
            model="gpt-5.4",
            reasoning_effort="high",
        )

        self.assertEqual(command[:2], ["cbc", "-p"])
        self.assertNotIn("/bin/zsh", command)
        self.assertIn("--model", command)
        self.assertIn("gpt-5.4", command)
        self.assertIn("--effort", command)
        self.assertNotIn("--reasoning-effort", command)
        self.assertIn("--input-format", command)
        self.assertEqual(command[command.index("--input-format") + 1], "text")
        self.assertIn("--output-format", command)
        self.assertEqual(command[command.index("--output-format") + 1], "stream-json")
        self.assertIn("--add-dir", command)
        self.assertIn(str(prompt_path.parent), command)
        self.assertIn("/tmp/repo/.worktrees/dd-123", command)
        self.assertIn("--disallowedTools", command)
        disallowed = command[command.index("--disallowedTools") + 1]
        self.assertIn("TaskCreate", disallowed)
        self.assertIn("TeamCreate", disallowed)
        self.assertIn("SkillManage", disallowed)
        self.assertNotIn("Skill,", f"{disallowed},")
        self.assertNotIn("--", command)
        self.assertNotIn(prompt, command)

    def test_worker_agents_are_discovered_from_adapter_modules(self):
        from robert_agent import worker_adapters

        self.assertIn("cbc", worker_adapters.available_worker_agents())
        self.assertIn("codex", worker_adapters.available_worker_agents())
        self.assertEqual(worker_adapters._load_adapter("cbc").AGENT_NAME, "cbc")
        self.assertEqual(worker_adapters._load_adapter("codex").AGENT_NAME, "codex")

    def test_dispatch_builds_worker_command_with_configured_launcher(self):
        from robert_agent import dispatch
        prompt_path = self.root / "task" / "prompt.md"
        prompt_path.parent.mkdir()
        prompt_path.write_text("hello", encoding="utf-8")

        command = dispatch.build_worker_command(
            prompt_path=prompt_path,
            worker_command="custom-cbc",
        )

        self.assertEqual(command[0], "custom-cbc")
        self.assertIn("--input-format", command)
        self.assertNotIn("--", command)
        self.assertNotIn("hello", command)

    def test_dispatch_builds_codex_worker_command_with_stdin_prompt(self):
        from robert_agent import dispatch
        prompt_path = self.root / "task" / "prompt.md"
        prompt_path.parent.mkdir()
        prompt = "codex prompt"
        prompt_path.write_text(prompt, encoding="utf-8")

        command = dispatch.build_worker_command(
            prompt_path=prompt_path,
            worktree_path=Path("/tmp/repo/.worktrees/dd-123"),
            model="gpt-5.4",
            reasoning_effort="high",
            worker_command="/opt/homebrew/bin/codex",
            worker_agent="codex",
        )

        self.assertEqual(command[0], "/opt/homebrew/bin/codex")
        self.assertIn("--model", command)
        self.assertEqual(command[command.index("--model") + 1], "gpt-5.4")
        self.assertIn("--config", command)
        self.assertEqual(
            command[command.index("--config") + 1],
            'model_reasoning_effort="high"',
        )
        self.assertLess(command.index("--config"), command.index("exec"))
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertIn("--sandbox", command)
        self.assertEqual(command[command.index("--sandbox") + 1], "danger-full-access")
        self.assertNotIn("--ask-for-approval", command)
        self.assertIn("--json", command)
        self.assertIn("--cd", command)
        self.assertEqual(command[command.index("--cd") + 1], "/tmp/repo/.worktrees/dd-123")
        self.assertIn("--add-dir", command)
        self.assertIn(str(prompt_path.parent), command)
        self.assertEqual(command[-1], "-")
        self.assertNotIn("--input-format", command)
        self.assertNotIn("--output-format", command)
        self.assertNotIn(prompt, command)

    def test_dispatch_builds_tcodex_worker_command_with_reasoning_effort(self):
        from robert_agent import dispatch
        prompt_path = self.root / "task" / "prompt.md"
        prompt_path.parent.mkdir()
        prompt_path.write_text("tcodex prompt", encoding="utf-8")

        command = dispatch.build_worker_command(
            prompt_path=prompt_path,
            model="gpt-5.6-sol",
            reasoning_effort="high",
            worker_command="tcodex",
            worker_agent="tcodex",
        )

        self.assertEqual(command[0], "tcodex")
        self.assertEqual(command[command.index("--model") + 1], "gpt-5.6-sol")
        self.assertEqual(
            command[command.index("--config") + 1],
            'model_reasoning_effort="high"',
        )
        self.assertLess(command.index("--config"), command.index("exec"))

    def test_dispatch_splits_configured_launcher_command(self):
        from robert_agent import dispatch
        prompt_path = self.root / "task" / "prompt.md"
        prompt_path.parent.mkdir()
        prompt_path.write_text("hello", encoding="utf-8")

        command = dispatch.build_worker_command(
            prompt_path=prompt_path,
            worker_command="/opt/bin/codex --profile dd-worker",
            worker_agent="codex",
        )

        self.assertEqual(command[:3], ["/opt/bin/codex", "--profile", "dd-worker"])
        self.assertLess(command.index("--config"), command.index("exec"))

    def test_dispatch_expands_user_in_configured_launcher_path(self):
        from robert_agent import dispatch
        prompt_path = self.root / "task" / "prompt.md"
        prompt_path.parent.mkdir()
        prompt_path.write_text("hello", encoding="utf-8")

        command = dispatch.build_worker_command(
            prompt_path=prompt_path,
            worker_command="~/bin/codex --profile dd-worker",
            worker_agent="codex",
        )

        self.assertEqual(
            command[:3],
            [str(Path("~/bin/codex").expanduser()), "--profile", "dd-worker"],
        )
        self.assertLess(command.index("--config"), command.index("exec"))

    def test_dispatch_accepts_normalized_worker_agent_config(self):
        from robert_agent import dispatch
        prompt_path = self.root / "task" / "prompt.md"
        prompt_path.parent.mkdir()
        prompt_path.write_text("hello", encoding="utf-8")

        command = dispatch.build_worker_command(
            prompt_path=prompt_path,
            worker_agent_config={
                "agent": "codex",
                "command": "/opt/homebrew/bin/codex",
                "command_argv": ["/opt/homebrew/bin/codex"],
            },
        )

        self.assertEqual(command[0], "/opt/homebrew/bin/codex")
        self.assertLess(command.index("--config"), command.index("exec"))

    def test_dispatch_worker_environment_defaults_bash_timeout(self):
        from robert_agent import dispatch
        env = dispatch._worker_environment(
            environ={"KEEP": "1"},
        )

        self.assertEqual(env["BASH_DEFAULT_TIMEOUT_MS"], "300000")
        self.assertNotIn("KEEP", env)
        self.assertNotIn("BASH_MAX_TIMEOUT_MS", env)
        overridden = dispatch._worker_environment(
            allowed_names=["BASH_DEFAULT_TIMEOUT_MS"],
            environ={"BASH_DEFAULT_TIMEOUT_MS": "450000"},
        )
        self.assertEqual(overridden["BASH_DEFAULT_TIMEOUT_MS"], "450000")

    def test_usage_parser_extracts_cbc_stream_json_result(self):
        from robert_agent import usage

        log = "\n".join(
            [
                "not json",
                json.dumps({"type": "message", "usage": {"input_tokens": 1}}),
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "model": "claude-sonnet-4.6",
                        "duration_ms": 1234,
                        "num_turns": 1,
                        "total_cost_usd": 0.0123,
                        "usage": {
                            "input_tokens": 10,
                            "output_tokens": 5,
                            "cache_creation_input_tokens": 2,
                            "cache_read_input_tokens": 3,
                        },
                    },
                    sort_keys=True,
                ),
            ]
        )

        parsed = usage.parse_cbc_stream_json(log)

        self.assertTrue(parsed["usage_available"], parsed)
        self.assertEqual(parsed["source"], "cbc_stream_json")
        self.assertEqual(parsed["usage"]["input_tokens"], 10)
        self.assertEqual(parsed["total_cost_usd"], 0.0123)
        self.assertEqual(parsed["duration_ms"], 1234)
        self.assertEqual(parsed["num_turns"], 1)

    def test_usage_parser_handles_missing_result_event(self):
        from robert_agent import usage

        parsed = usage.parse_cbc_stream_json('{"type": "message"}\nnot json')

        self.assertFalse(parsed["usage_available"], parsed)
        self.assertEqual(parsed["source"], "no_result_event")

    def test_dispatch_dry_run_records_prepared_attempt(self):
        from robert_agent import dispatch
        prompt_path = self.root / "task" / "prompt.md"
        prompt_path.parent.mkdir()
        prompt_path.write_text("hello", encoding="utf-8")
        result = dispatch.dispatch_worker(
            task_id="task-1",
            attempt_id="attempt-1",
            prompt_path=prompt_path,
            worktree_path=self.root / "worktree",
            dry_run=True,
        )

        self.assertEqual(result["status"], "prepared")
        self.assertIsNone(result["pid"])
        self.assertIn("command", result)
        self.assertEqual(result["prompt_path"], str(prompt_path))
        self.assertNotIn("hello", result["command"])

    def test_dispatch_detects_immediate_worker_exit_with_log_artifacts(self):
        from robert_agent import dispatch
        prompt_path = self.root / "task" / "prompt.md"
        prompt_path.parent.mkdir()
        prompt_path.write_text("hello", encoding="utf-8")
        calls = []
        original_popen = dispatch.subprocess.Popen
        original_environment = dispatch._worker_environment

        class ExitedProcess:
            pid = 23456

            def poll(self):
                return 7

        def fake_worker_environment(allowed_names=()):
            return {"BASH_DEFAULT_TIMEOUT_MS": "300000", "WORKER_ENV": "set"}

        def fake_popen(command, **kwargs):
            calls.append((command, kwargs, kwargs["stdin"].read()))
            return ExitedProcess()

        try:
            dispatch.subprocess.Popen = fake_popen
            dispatch._worker_environment = fake_worker_environment
            result = dispatch.dispatch_worker(
                task_id="task-1",
                attempt_id="attempt-1",
                prompt_path=prompt_path,
                worktree_path=self.root / "worktree",
                dry_run=False,
                startup_probe_seconds=0,
            )
        finally:
            dispatch.subprocess.Popen = original_popen
            dispatch._worker_environment = original_environment

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "failed_dispatch")
        self.assertEqual(result["returncode"], 7)
        self.assertIn("stdout_path", result)
        self.assertIn("stderr_path", result)
        self.assertTrue(Path(result["stdout_path"]).exists())
        self.assertTrue(Path(result["stderr_path"]).exists())
        self.assertEqual(calls[0][1]["stdout"].name, result["stdout_path"])
        self.assertEqual(calls[0][1]["stderr"].name, result["stderr_path"])
        self.assertEqual(calls[0][1]["env"]["BASH_DEFAULT_TIMEOUT_MS"], "300000")
        self.assertEqual(calls[0][1]["env"]["WORKER_ENV"], "set")
        self.assertEqual(calls[0][1]["stdin"].name, str(prompt_path))
        self.assertEqual(calls[0][2], "hello")
        self.assertTrue(calls[0][1]["start_new_session"])

    def test_dispatch_reports_popen_failure_with_log_artifacts(self):
        from robert_agent import dispatch
        prompt_path = self.root / "task" / "prompt.md"
        prompt_path.parent.mkdir()
        prompt_path.write_text("hello", encoding="utf-8")
        original_popen = dispatch.subprocess.Popen

        def fake_popen(_command, **_kwargs):
            raise FileNotFoundError("cbc")

        try:
            dispatch.subprocess.Popen = fake_popen
            result = dispatch.dispatch_worker(
                task_id="task-1",
                attempt_id="attempt-1",
                prompt_path=prompt_path,
                worktree_path=self.root / "worktree",
                dry_run=False,
                startup_probe_seconds=0,
            )
        finally:
            dispatch.subprocess.Popen = original_popen

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "failed_dispatch")
        self.assertIn("cbc", result["safe_error"])
        self.assertTrue(Path(result["stdout_path"]).exists())
        self.assertTrue(Path(result["stderr_path"]).exists())

    def test_acceptance_preflight_reports_live_gate_statuses(self):
        from robert_agent import acceptance
        config_path = self._write_acceptance_config()

        def command_exists(command):
            return {
                "python3": "/usr/bin/python3",
                "gh": "/usr/bin/gh",
                "cbc": None,
            }.get(command)

        class Completed:
            returncode = 1
            stderr = "not logged in"

        result = acceptance.acceptance_preflight(
            config_path,
            command_exists=command_exists,
            run_command=lambda *_args, **_kwargs: Completed(),
        )

        gates = {gate["id"]: gate for gate in result["gates"]}
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(gates["config_valid"]["status"], "passed")
        self.assertEqual(gates["repo_checkout"]["status"], "passed")
        self.assertEqual(gates["worktree_root"]["status"], "passed")
        self.assertEqual(gates["python_bin"]["status"], "passed")
        self.assertEqual(gates["gh_cli"]["status"], "passed")
        self.assertEqual(gates["gh_auth"]["status"], "failed")
        self.assertEqual(gates["worker_command"]["status"], "failed")
        self.assertIn("Run gh auth login", result["next_actions"])
        self.assertIn("Install or configure cbc worker agent command: cbc", result["next_actions"])

    def test_acceptance_preflight_marks_live_gates_skipped_explicitly(self):
        from robert_agent import acceptance
        config_path = self._write_acceptance_config()

        result = acceptance.acceptance_preflight(
            config_path,
            skip_external=True,
            command_exists=lambda command: f"/usr/bin/{command}",
        )

        gates = {gate["id"]: gate for gate in result["gates"]}
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(gates["python_bin"]["status"], "passed")
        self.assertEqual(gates["gh_auth"]["status"], "skipped")
        self.assertIn(
            "Run without --skip-external for live GitHub auth verification",
            result["next_actions"],
        )

    def test_acceptance_preflight_checks_configured_worker_command(self):
        from robert_agent import acceptance
        config_path = self._write_acceptance_config()
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "database: dd.sqlite3",
                "database: dd.sqlite3\nworker_command: custom-cbc",
            ),
            encoding="utf-8",
        )
        probed = []

        def command_exists(command):
            probed.append(command)
            return {
                "python3": "/usr/bin/python3",
                "gh": "/usr/bin/gh",
                "custom-cbc": "/opt/bin/custom-cbc",
            }.get(command)

        class Completed:
            returncode = 0
            stderr = ""

        result = acceptance.acceptance_preflight(
            config_path,
            command_exists=command_exists,
            run_command=lambda *_args, **_kwargs: Completed(),
        )

        gates = {gate["id"]: gate for gate in result["gates"]}
        self.assertTrue(result["ok"], result)
        self.assertIn("python3", probed)
        self.assertIn("custom-cbc", probed)
        self.assertEqual(gates["python_bin"]["status"], "passed")
        self.assertEqual(gates["worker_command"]["status"], "passed")
        self.assertEqual(gates["worker_command"]["evidence"], "/opt/bin/custom-cbc")

    def test_acceptance_preflight_checks_codex_worker_agent_command(self):
        from robert_agent import acceptance
        config_path = self._write_acceptance_config()
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "database: dd.sqlite3",
                "database: dd.sqlite3\nworker_agent: codex\nworker_command: /opt/homebrew/bin/codex",
            ),
            encoding="utf-8",
        )
        probed = []

        def command_exists(command):
            probed.append(command)
            return {
                "python3": "/usr/bin/python3",
                "gh": "/usr/bin/gh",
                "/opt/homebrew/bin/codex": "/opt/homebrew/bin/codex",
            }.get(command)

        class Completed:
            returncode = 0
            stderr = ""

        result = acceptance.acceptance_preflight(
            config_path,
            command_exists=command_exists,
            run_command=lambda *_args, **_kwargs: Completed(),
        )

        gates = {gate["id"]: gate for gate in result["gates"]}
        self.assertTrue(result["ok"], result)
        self.assertIn("/opt/homebrew/bin/codex", probed)
        self.assertEqual(gates["worker_command"]["status"], "passed")
        self.assertEqual(gates["worker_command"]["summary"], "codex worker agent command is available")
        self.assertEqual(gates["worker_command"]["evidence"], "/opt/homebrew/bin/codex")

    def test_acceptance_preflight_checks_every_named_worker_command(self):
        from robert_agent import acceptance
        config_path = self._write_acceptance_config()
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "database: dd.sqlite3",
                "database: dd.sqlite3\n"
                "workers:\n"
                "  - name: default\n"
                "    agent: cbc\n"
                "    command: /opt/bin/cbc\n"
                "    default_model: gpt-5.4\n"
                "    default_effort: high\n"
                "  - name: reviewer\n"
                "    agent: codex\n"
                "    command: /opt/bin/review-codex\n"
                "    default_model: gpt-5.6-sol\n"
                "    default_effort: xhigh",
            ),
            encoding="utf-8",
        )
        probed = []

        def command_exists(command):
            probed.append(command)
            return {
                "python3": "/usr/bin/python3",
                "gh": "/usr/bin/gh",
                "/opt/bin/cbc": "/opt/bin/cbc",
                "/opt/bin/review-codex": "/opt/bin/review-codex",
            }.get(command)

        class Completed:
            returncode = 0
            stderr = ""

        result = acceptance.acceptance_preflight(
            config_path,
            command_exists=command_exists,
            run_command=lambda *_args, **_kwargs: Completed(),
        )

        gates = {gate["id"]: gate for gate in result["gates"]}
        self.assertTrue(result["ok"], result)
        self.assertIn("/opt/bin/cbc", probed)
        self.assertIn("/opt/bin/review-codex", probed)
        self.assertEqual(gates["worker_command:default"]["status"], "passed")
        self.assertEqual(gates["worker_command:reviewer"]["status"], "passed")
        self.assertEqual(
            gates["worker_command:reviewer"]["summary"],
            "reviewer (codex) worker command is available",
        )

    def test_acceptance_preflight_checks_configured_python_bin(self):
        from robert_agent import acceptance
        config_path = self._write_acceptance_config()
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "database: dd.sqlite3",
                "database: dd.sqlite3\npython_bin: /opt/python/bin/python3",
            ),
            encoding="utf-8",
        )
        probed = []

        def command_exists(command):
            probed.append(command)
            return {
                "/opt/python/bin/python3": "/opt/python/bin/python3",
                "gh": "/usr/bin/gh",
                "cbc": "/usr/bin/cbc",
            }.get(command)

        class Completed:
            returncode = 0
            stderr = ""

        result = acceptance.acceptance_preflight(
            config_path,
            command_exists=command_exists,
            run_command=lambda *_args, **_kwargs: Completed(),
        )

        gates = {gate["id"]: gate for gate in result["gates"]}
        self.assertTrue(result["ok"], result)
        self.assertIn("/opt/python/bin/python3", probed)
        self.assertEqual(gates["python_bin"]["status"], "passed")
        self.assertEqual(gates["python_bin"]["evidence"], "/opt/python/bin/python3")

    def test_acceptance_preflight_reports_missing_python_bin(self):
        from robert_agent import acceptance
        config_path = self._write_acceptance_config()
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "database: dd.sqlite3",
                "database: dd.sqlite3\npython_bin: python",
            ),
            encoding="utf-8",
        )

        result = acceptance.acceptance_preflight(
            config_path,
            skip_external=True,
            command_exists=lambda command: None if command == "python" else f"/usr/bin/{command}",
        )

        gates = {gate["id"]: gate for gate in result["gates"]}
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(gates["python_bin"]["status"], "failed")
        self.assertIn("Install or configure python_bin: python", result["next_actions"])

    def test_acceptance_preflight_reports_prepared_dispatch_state(self):
        from robert_agent import acceptance
        from robert_agent import storage

        config_path = self._write_acceptance_config()
        db_path = self.root / "data" / "dd.sqlite3"
        storage.init_database(db_path)
        now = "2026-06-18T08:00:00+00:00"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root)
                VALUES ('repo-1', 'x/y', 'robot', 'master', '/repo', '/repo/.worktrees')
                """
            )
            conn.execute(
                """
                INSERT INTO github_sources(source_id, repo_id, source_key, source_type, number)
                VALUES ('source-1', 'repo-1', 'github:x/y#1', 'issue', 1)
                """
            )
            conn.execute(
                """
                INSERT INTO workstreams(workstream_id, repo_id, primary_source_id, lifecycle, active_task_id, created_at, updated_at)
                VALUES ('github:x/y#1', 'repo-1', 'source-1', 'active', 'task-1', ?, ?)
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO tasks(task_id, workstream_id, lifecycle, priority, route_id, expected_output, created_at, updated_at)
                VALUES ('task-1', 'github:x/y#1', 'queued', 'P1', 'new-pr', 'new_pr', ?, ?)
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO attempts(attempt_id, task_id, attempt_no, status, started_at, heartbeat_at)
                VALUES ('attempt-1', 'task-1', 1, 'prepared', ?, ?)
                """,
                (now, now),
            )

        result = acceptance.acceptance_preflight(
            config_path,
            skip_external=True,
            command_exists=lambda command: f"/usr/bin/{command}",
        )

        gates = {gate["id"]: gate for gate in result["gates"]}
        self.assertIn("control_plane_state", gates)
        self.assertEqual(gates["control_plane_state"]["status"], "passed")
        self.assertEqual(
            gates["control_plane_state"]["summary"],
            "1 prepared attempt will be eligible for dispatch on the next non-dry-run cycle",
        )
        self.assertEqual(gates["control_plane_state"]["evidence"]["prepared_attempts"], 1)
        self.assertIn(
            f"Review prepared attempts before non-dry-run: {sys.executable} -B "
            f"{acceptance.AGENT_ROOT / 'summarize.py'} --db {db_path}",
            result["next_actions"],
        )

    def test_acceptance_preflight_reports_worktree_root_create_command(self):
        from robert_agent import acceptance
        config_path = self._write_acceptance_config()
        worktree_root = self.root / "repo" / ".worktrees"
        worktree_root.rmdir()

        result = acceptance.acceptance_preflight(
            config_path,
            skip_external=True,
            command_exists=lambda command: f"/usr/bin/{command}",
        )

        gates = {gate["id"]: gate for gate in result["gates"]}
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(gates["worktree_root"]["status"], "failed")
        self.assertEqual(gates["worktree_root"]["evidence"], str(worktree_root))
        self.assertIn(
            f"mkdir -p {shlex.quote(str(worktree_root))}",
            result["next_actions"],
        )

    def test_acceptance_preflight_rejects_worktree_root_file(self):
        from robert_agent import acceptance
        config_path = self._write_acceptance_config()
        worktree_root = self.root / "repo" / ".worktrees"
        worktree_root.rmdir()
        worktree_root.write_text("not a directory", encoding="utf-8")

        result = acceptance.acceptance_preflight(
            config_path,
            skip_external=True,
            command_exists=lambda command: f"/usr/bin/{command}",
        )

        gates = {gate["id"]: gate for gate in result["gates"]}
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(gates["worktree_root"]["status"], "failed")
        self.assertEqual(gates["worktree_root"]["summary"], "worktree_root is not a directory")
        self.assertEqual(gates["worktree_root"]["evidence"], str(worktree_root))
        self.assertIn(
            f"Remove file or set repos[0].worktree_root to a directory: {worktree_root}",
            result["next_actions"],
        )

    def test_acceptance_preflight_checks_all_configured_worktree_roots(self):
        from robert_agent import acceptance
        config_path = self._write_acceptance_config()
        repo_b = self.root / "repo-b"
        repo_b.mkdir()
        (repo_b / ".git").mkdir()
        worktree_b = repo_b / ".worktrees"
        worktree_b.write_text("not a directory", encoding="utf-8")
        config_path.write_text(
            config_path.read_text(encoding="utf-8")
            + f"""
  - full_name: org/repo-b
    github_account: robot
    trusted_actors:
      - alice
    default_base_branch: main
    repo_root: {repo_b}
    worktree_root: {worktree_b}
""",
            encoding="utf-8",
        )

        result = acceptance.acceptance_preflight(
            config_path,
            skip_external=True,
            command_exists=lambda command: f"/usr/bin/{command}",
        )

        gates = {gate["id"]: gate for gate in result["gates"]}
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(gates["worktree_root:org/repo-b"]["status"], "failed")
        self.assertEqual(gates["worktree_root:org/repo-b"]["summary"], "worktree_root is not a directory")
        self.assertEqual(gates["worktree_root:org/repo-b"]["evidence"], str(worktree_b))
        self.assertIn(
            f"Remove file or set repo org/repo-b worktree_root to a directory: {worktree_b}",
            result["next_actions"],
        )

    def test_acceptance_preflight_reports_missing_config_without_traceback(self):
        from robert_agent import acceptance
        result = acceptance.acceptance_preflight(self.root / "missing-config.yml")

        gates = {gate["id"]: gate for gate in result["gates"]}
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "failed_config")
        self.assertEqual(gates["config_valid"]["status"], "failed")
        expected_command = (
            f"{shlex.quote(sys.executable)} -B "
            f"{shlex.quote(str(acceptance.AGENT_ROOT / 'init_config.py'))} "
            f"--config {shlex.quote(str((self.root / 'missing-config.yml').resolve()))}"
        )
        self.assertEqual(
            result["next_actions"],
            [expected_command],
        )

    def test_acceptance_preflight_reports_unconfigured_public_skeleton(self):
        from robert_agent import acceptance
        config_path = self.root / "config.yml"
        config_path.write_text(
            (PACKAGE_ROOT / "resources" / "config.example.yml").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        result = acceptance.acceptance_preflight(config_path)

        gates = {gate["id"]: gate for gate in result["gates"]}
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "failed_config")
        self.assertEqual(gates["config_valid"]["status"], "failed")
        self.assertIn(
            "repos must contain at least one repository",
            gates["config_valid"]["summary"],
        )
        self.assertEqual(
            result["next_actions"],
            ["Fix robert config"],
        )

    def test_acceptance_preflight_reports_data_dir_file(self):
        from robert_agent import acceptance
        config_path = self._write_acceptance_config()
        data_file = self.root / "data-file"
        data_file.write_text("not a directory", encoding="utf-8")
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                f"data_dir: {self.root / 'data'}",
                f"data_dir: {data_file}",
            ),
            encoding="utf-8",
        )

        result = acceptance.acceptance_preflight(config_path)

        gates = {gate["id"]: gate for gate in result["gates"]}
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "failed_config")
        self.assertEqual(gates["config_valid"]["status"], "failed")
        self.assertIn(str(data_file), gates["config_valid"]["summary"])
        self.assertEqual(
            result["next_actions"],
            [f"Set data_dir to a directory path in {config_path.resolve()}"],
        )

    def test_acceptance_preflight_reports_empty_database_name(self):
        from robert_agent import acceptance
        config_path = self._write_acceptance_config()
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "database: dd.sqlite3",
                "database:",
            ),
            encoding="utf-8",
        )

        result = acceptance.acceptance_preflight(config_path)

        gates = {gate["id"]: gate for gate in result["gates"]}
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "failed_config")
        self.assertEqual(gates["config_valid"]["status"], "failed")
        self.assertEqual(gates["config_valid"]["summary"], "database must not be empty")
        self.assertEqual(
            result["next_actions"],
            [f"Set database to a SQLite filename in {config_path.resolve()}"],
        )

    def test_acceptance_preflight_reports_database_path(self):
        from robert_agent import acceptance
        config_path = self._write_acceptance_config()
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "database: dd.sqlite3",
                "database: nested/dd.sqlite3",
            ),
            encoding="utf-8",
        )

        result = acceptance.acceptance_preflight(config_path)

        gates = {gate["id"]: gate for gate in result["gates"]}
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "failed_config")
        self.assertEqual(gates["config_valid"]["status"], "failed")
        self.assertEqual(
            gates["config_valid"]["summary"],
            "database must be a filename, not a path: nested/dd.sqlite3",
        )
        self.assertEqual(
            result["next_actions"],
            [f"Set database to a SQLite filename in {config_path.resolve()}"],
        )

    def test_acceptance_preflight_reports_nonpositive_max_concurrency(self):
        from robert_agent import acceptance
        config_path = self._write_acceptance_config()
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "database: dd.sqlite3",
                "database: dd.sqlite3\nmax_concurrency: 0",
            ),
            encoding="utf-8",
        )

        result = acceptance.acceptance_preflight(config_path)

        gates = {gate["id"]: gate for gate in result["gates"]}
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "failed_config")
        self.assertEqual(gates["config_valid"]["status"], "failed")
        self.assertEqual(gates["config_valid"]["summary"], "max_concurrency must be at least 1")
        self.assertEqual(
            result["next_actions"],
            [f"Set max_concurrency to a positive integer in {config_path.resolve()}"],
        )

    def test_acceptance_preflight_reports_nonpositive_runtime_threshold(self):
        from robert_agent import acceptance
        config_path = self._write_acceptance_config()
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "database: dd.sqlite3",
                "database: dd.sqlite3\nstale_after_minutes: 0",
            ),
            encoding="utf-8",
        )

        result = acceptance.acceptance_preflight(config_path)

        gates = {gate["id"]: gate for gate in result["gates"]}
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "failed_config")
        self.assertEqual(gates["config_valid"]["status"], "failed")
        self.assertEqual(gates["config_valid"]["summary"], "stale_after_minutes must be at least 1")
        self.assertEqual(
            result["next_actions"],
            [f"Set stale_after_minutes to a positive integer in {config_path.resolve()}"],
        )

    def test_acceptance_preflight_reports_invalid_repo_root_path(self):
        from robert_agent import acceptance
        config_path = self._write_acceptance_config()
        bad_repo_root = self.root / "missing-repo"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                f"repo_root: {self.root / 'repo'}",
                f"repo_root: {bad_repo_root}",
            ),
            encoding="utf-8",
        )

        result = acceptance.acceptance_preflight(config_path)

        gates = {gate["id"]: gate for gate in result["gates"]}
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "failed_config")
        self.assertEqual(gates["config_valid"]["status"], "failed")
        self.assertIn(str(bad_repo_root), gates["config_valid"]["summary"])
        self.assertEqual(
            result["next_actions"],
            [f"Set repos[0].repo_root to a real local git checkout in {config_path.resolve()}"],
        )

    def test_acceptance_preflight_reports_invalid_second_repo_root_path(self):
        from robert_agent import acceptance
        config_path = self._write_acceptance_config()
        bad_repo_root = self.root / "missing-repo-b"
        config_path.write_text(
            config_path.read_text(encoding="utf-8")
            + f"""
  - full_name: org/repo-b
    github_account: robot
    trusted_actors:
      - alice
    default_base_branch: main
    repo_root: {bad_repo_root}
    worktree_root: {bad_repo_root / ".worktrees"}
""",
            encoding="utf-8",
        )

        result = acceptance.acceptance_preflight(config_path)

        gates = {gate["id"]: gate for gate in result["gates"]}
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "failed_config")
        self.assertEqual(gates["config_valid"]["status"], "failed")
        self.assertIn("repos[1].repo_root is not a git checkout", gates["config_valid"]["summary"])
        self.assertEqual(
            result["next_actions"],
            [f"Set repos[1].repo_root to a real local git checkout in {config_path.resolve()}"],
        )

    def test_init_config_creates_public_config_and_refuses_overwrite(self):
        from robert_agent import init_config
        target = self.root / "nested" / "config.yml"
        repo = self.root / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        values = {
            "repo": "example/backend",
            "repo_path": str(repo),
            "worker": "codex",
            "github_account": "robert-bot",
            "trusted_actor": "maintainer",
        }

        created = init_config.init_config(
            target,
            values,
            non_interactive=True,
        )

        self.assertTrue(created["ok"], created)
        self.assertEqual(created["status"], "created")
        self.assertEqual(created["config_path"], str(target))
        self.assertIn("full_name: example/backend", target.read_text(encoding="utf-8"))

        second = init_config.init_config(
            target,
            values,
            non_interactive=True,
        )

        self.assertFalse(second["ok"], second)
        self.assertEqual(second["status"], "exists")
        self.assertIn("already exists", second["safe_error"])

    def test_prompt_uses_current_python_executable_for_worker_commands(self):
        from robert_agent import render_prompt
        prompt = render_prompt.render_prompt(
            task={
                "task_id": "task-1",
                "attempt_id": "attempt-1",
                "workstream_id": "github:example/backend!456",
            },
            route_result={
                "expected_output": "update_existing_pr",
                "allowed_github_actions": ["push_existing_pr", "comment"],
                "required_skills": [],
            },
            events=[{"event_fingerprint": "comment:1"}],
            runtime_context={
                "python_bin": "/usr/bin/python3.11",
                "db_path": "/tmp/dd.sqlite3",
                "result_script": "/agent/result.py",
                "snapshot_script": "/worker/snapshot.py",
                "heartbeat_script": "/worker/heartbeat.py",
                "status_script": "/agent/status.py",
            },
        )

        self.assertIn("/usr/bin/python3.11 /agent/status.py", prompt)
        self.assertIn("use the compact read-only status CLI", prompt)
        self.assertIn("run latest", prompt)
        self.assertIn("task task-1", prompt)
        self.assertIn("attempt attempt-1", prompt)
        self.assertIn("workstream github:example/backend!456", prompt)
        self.assertIn("event <event_fingerprint>", prompt)
        self.assertIn("events <query>", prompt)
        self.assertIn("source <source_key-or-number-or-url>", prompt)
        self.assertIn("artifact task-1 worker_stderr", prompt)
        self.assertIn("/usr/bin/python3.11 /worker/snapshot.py", prompt)
        self.assertIn("/usr/bin/python3.11 /worker/heartbeat.py", prompt)
        self.assertIn("/usr/bin/python3.11 /agent/result.py", prompt)

    def test_runtime_context_uses_configured_python_bin(self):
        from robert_agent import run_once
        context = run_once._runtime_context(
            self.root / "dd.sqlite3",
            {"default_base_branch": "master", "python_bin": "/opt/python/bin/python3"},
        )

        self.assertEqual(context["python_bin"], "/opt/python/bin/python3")
        self.assertTrue(context["status_script"].endswith("status.py"))

    def test_supervise_classifies_stale_and_timeout(self):
        from robert_agent import supervise
        now = datetime(2026, 6, 16, tzinfo=timezone.utc)
        stale = supervise.classify_attempt(
            heartbeat_at=now - timedelta(minutes=25),
            started_at=now - timedelta(minutes=30),
            now=now,
            stale_after_minutes=20,
            hard_timeout_minutes=90,
        )
        timed_out = supervise.classify_attempt(
            heartbeat_at=now - timedelta(minutes=95),
            started_at=now - timedelta(minutes=95),
            now=now,
            stale_after_minutes=20,
            hard_timeout_minutes=90,
        )

        self.assertEqual(stale["status"], "stale")
        self.assertFalse(stale["terminate"])
        self.assertEqual(timed_out["status"], "failed_timeout")
        self.assertTrue(timed_out["terminate"])

    def test_notify_records_local_notification(self):
        from robert_agent import notify
        db_path = self.root / "dd.sqlite3"
        self._init_notifications_db(db_path)

        result = notify.record_notification(
            db_path=db_path,
            notification_type="worker_stale",
            status="sent",
            metadata={"task_id": "task-1"},
        )

        self.assertTrue(result["ok"], result)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            row = conn.execute(
                "SELECT notification_type, status, task_id FROM notifications"
            ).fetchone()
        self.assertEqual(row, ("worker_stale", "sent", "task-1"))

    def test_notify_command_uses_argv_not_shell(self):
        from robert_agent import notify
        db_path = self.root / "dd.sqlite3"
        self._init_notifications_db(db_path)
        calls = []

        class Proc:
            returncode = 0

        original_run = notify.subprocess.run

        def fake_run(args, **kwargs):
            calls.append((args, kwargs))
            return Proc()

        try:
            notify.subprocess.run = fake_run
            result = notify.send_notification(
                db_path,
                {"type": "worker_stale", "task_id": "task-1"},
                command="notify-tool --channel local",
            )
        finally:
            notify.subprocess.run = original_run

        self.assertTrue(result["ok"], result)
        self.assertEqual(calls[0][0], ["notify-tool", "--channel", "local"])
        self.assertNotIn("shell", calls[0][1])

    def test_summarize_counts_control_plane_rows(self):
        from robert_agent import summarize
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        summary = summarize.summarize_database(db_path)

        self.assertEqual(summary["repos"], 1)
        self.assertEqual(summary["workstreams"], 1)
        self.assertEqual(summary["tasks"], 2)

    def test_web_builds_dashboard_data(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        data = web.build_dashboard_data(db_path)

        self.assertEqual(data["summary"]["tasks"], 2)
        self.assertEqual(
            data["repos"],
            [
                {
                    "repo_id": "repo-1",
                    "full_name": "example/backend",
                }
            ],
        )
        self.assertEqual(data["recent_tasks"][0]["task_id"], "task-2")
        self.assertIn("work_items", data)
        task_by_id = {task["task_id"]: task for task in data["recent_tasks"]}
        self.assertEqual(task_by_id["task-1"]["repo_id"], "repo-1")
        self.assertEqual(task_by_id["task-1"]["repo_full_name"], "example/backend")
        self.assertEqual(task_by_id["task-1"]["source_title"], "Question")
        self.assertEqual(data["recent_events"][0]["source_title"], "Question")
        self.assertEqual(data["wakeup_summary"]["pending"], 1)
        self.assertEqual(data["recent_wakeups"][0]["result_id"], "result-1")
        self.assertIn("acceptance_metrics", data)

    def test_web_groups_work_by_github_source(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO task_events(task_id, event_id, relationship, created_at)
                VALUES ('task-2', 'event-2', 'trigger', '2030-01-01T00:00:00+00:00')
                """
            )
            conn.execute(
                """
                UPDATE tasks
                SET updated_at = '2030-01-01T00:00:00+00:00'
                WHERE task_id = 'task-2'
                """
            )

        data = web.build_dashboard_data(db_path)
        work_items = {item["source_key"]: item for item in data["work_items"]}
        item = work_items["github:example/backend#1"]

        self.assertEqual(item["task_count"], 2)
        self.assertEqual(item["repo_id"], "repo-1")
        self.assertEqual(item["repo_full_name"], "example/backend")
        self.assertEqual(item["source_title"], "Question")
        self.assertEqual(item["tasks"][0]["task_id"], "task-2")
        self.assertEqual(item["latest_task_updated_at"], "2030-01-01T00:00:00+00:00")

    def test_web_dashboard_data_lists_all_repositories(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root)
                VALUES ('repo-2', 'Org/second-repo', 'robert-bot', 'main', '/repo2', '/repo2/.worktrees')
                """
            )

        data = web.build_dashboard_data(db_path)

        self.assertEqual(
            data["repos"],
            [
                {"repo_id": "repo-2", "full_name": "Org/second-repo"},
                {
                    "repo_id": "repo-1",
                    "full_name": "example/backend",
                },
            ],
        )

    def test_web_builds_notification_center(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        data = web.build_dashboard_data(db_path)

        self.assertIn("notification_center", data)
        self.assertTrue(
            any(item["kind"] == "notification" for item in data["notification_center"])
        )
        self.assertTrue(
            {
                "id",
                "kind",
                "severity",
                "title",
                "summary",
                "created_at",
                "metadata",
            }
            <= set(data["notification_center"][0])
        )

    def test_web_sorts_notifications_by_newest_time(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO notifications(
                  notification_id, task_id, notification_type, channel, status,
                  created_at, metadata_json
                )
                VALUES (
                  'notification-2', 'task-1', 'worker_stale',
                  'local', 'recorded', '2030-01-01T00:00:00+00:00', '{"summary": "latest"}'
                )
                """
            )

        data = web.build_dashboard_data(db_path)

        self.assertEqual(data["notification_center"][0]["id"], "notification:notification-2")

    def test_web_exposes_acceptance_metrics_with_usage(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        usage_payload = {
            "usage_available": True,
            "source": "cbc_stream_json",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_creation_input_tokens": 2,
                "cache_read_input_tokens": 3,
            },
            "total_cost_usd": 0.0123,
        }
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                "UPDATE worker_results SET metadata_json = ? WHERE result_id = 'result-1'",
                (
                    json.dumps(
                        {
                            "audit": {"status": "accepted"},
                            "usage": usage_payload,
                        },
                        sort_keys=True,
                    ),
                ),
            )
            conn.execute(
                """
                UPDATE github_actions
                SET publish_status = 'published',
                    metadata_json = ?
                WHERE action_id = 'action-1'
                """,
                (
                    json.dumps(
                        {
                            "body": self._dd_comment_body("ready"),
                            "publish": {"status": "published", "deduplicated": True},
                        },
                        sort_keys=True,
                    ),
                ),
            )

        data = web.build_dashboard_data(db_path)
        metrics = data["acceptance_metrics"]

        self.assertEqual(metrics["accepted_results"], 1)
        self.assertEqual(metrics["rejected_results"], 0)
        self.assertEqual(metrics["published_actions"], 1)
        self.assertEqual(metrics["deduplicated_actions"], 1)
        self.assertTrue(metrics["usage_available"])
        self.assertEqual(metrics["total_input_tokens"], 10)
        self.assertEqual(metrics["total_output_tokens"], 5)
        self.assertEqual(metrics["total_cache_creation_input_tokens"], 2)
        self.assertEqual(metrics["total_cache_read_input_tokens"], 3)
        self.assertEqual(metrics["total_cost_usd"], 0.0123)

    def test_web_status_and_chat_expose_latest_loop_summary(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        latest_loop = {
            "ok": True,
            "status": "completed",
            "cycles": 2,
            "stop_reason": "max_cycles",
        }
        (db_path.parent / "latest-loop.json").write_text(
            json.dumps(latest_loop, sort_keys=True),
            encoding="utf-8",
        )

        data = web.build_dashboard_data(db_path)
        chat_code, chat_payload = self._run_chat_status("--db", str(db_path), "status")
        status_code, status_payload = self._run_status("--db", str(db_path), "status")

        self.assertEqual(data["latest_loop"]["stop_reason"], "max_cycles")
        self.assertEqual(chat_code, 0)
        self.assertIn("Latest loop: max_cycles", chat_payload["message"])
        self.assertEqual(status_code, 0)
        self.assertEqual(status_payload["data"]["latest_loop"]["cycles"], 2)

    def test_status_surfaces_daemon_summary(self):
        from robert_agent import chat_status
        from robert_agent import status
        from robert_agent import web
        from robert_agent import daemon_state, storage

        db_path = self.root / "daemon-status.sqlite3"
        storage.init_database(db_path)
        with closing(daemon_state.connect(db_path)) as conn, conn:
            run = daemon_state.start_daemon_run(
                conn,
                config_path="/tmp/config.yml",
                owner_id="daemon-run-1",
                now="2026-07-03T00:00:00+00:00",
            )
            daemon_state.acquire_daemon_lease(
                conn,
                resource_key="daemon:main",
                owner_id="daemon-run-1",
                ttl_seconds=60,
                now="2026-07-03T00:00:00+00:00",
            )
            daemon_state.record_event(
                conn,
                run["daemon_run_id"],
                "run_once_triggered",
                "running",
                {"reason": "event_queue"},
                now="2026-07-03T00:00:00+00:00",
            )
            daemon_state.record_event(
                conn,
                run["daemon_run_id"],
                "live_poll_skipped_rate_limit",
                "skipped",
                {"reason": "core_rate_limit_floor"},
                now="2026-07-03T00:00:01+00:00",
            )

        dashboard = web.build_dashboard_data(db_path)
        status = status.build_status(db_path)
        message = chat_status.format_status(dashboard)

        self.assertEqual(
            dashboard["daemon"]["latest_event"]["event_type"],
            "live_poll_skipped_rate_limit",
        )
        self.assertEqual(dashboard["daemon"]["latest_lease"]["status"], "active")
        self.assertEqual(dashboard["daemon"]["recent_events"][0]["event_type"], "live_poll_skipped_rate_limit")
        self.assertEqual(dashboard["daemon"]["recent_events"][1]["event_type"], "run_once_triggered")
        self.assertEqual(status["daemon"]["latest_run"]["status"], "running")
        self.assertIn("Daemon:", message)
        self.assertIn("live_poll_skipped_rate_limit", message)

    def test_web_exposes_knowledge_review_data(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        self._record_knowledge_candidate(db_path)

        data = web.build_dashboard_data(db_path)

        review = data["knowledge_review"]
        self.assertEqual(review["counts"]["pending"], 1)
        self.assertEqual(review["counts"]["active"], 0)
        self.assertEqual(review["pending_candidates"][0]["candidate_id"], "kc-1")
        self.assertEqual(review["pending_candidates"][0]["title"], "Prefer update-existing-pr")
        self.assertEqual(review["submission_boundary"], "local_runtime_knowledge_only")

    def test_web_renders_operator_dashboard_html_shell(self):
        from robert_agent import web
        html = web.render_dashboard_html()

        self.assertIn('<main id="dashboard-app"', html)
        self.assertIn('rel="icon" type="image/svg+xml"', html)
        self.assertNotIn('href="data:,"', html)
        self.assertIn("mission-shell", html)
        self.assertIn("fetch('/data.json'", html)
        self.assertIn('data-view="command"', html)
        self.assertIn('id="view-work"', html)
        self.assertIn('data-view="daemon"', html)
        self.assertIn('id="view-daemon"', html)
        self.assertIn('data-view="timeline"', html)
        self.assertIn('id="view-knowledge"', html)
        self.assertIn('data-view="notifications"', html)
        self.assertLess(html.index('data-view="notifications"'), html.index('data-view="daemon"'))
        self.assertIn("theme-select", html)
        self.assertIn("language-select", html)
        self.assertIn("notification-center", html)
        self.assertIn("detail-drawer", html)
        self.assertIn("work-task-list", html)
        self.assertLess(html.index('id="work-task-list"'), html.index('id="detail-drawer"'))
        self.assertNotIn('id="command-search"', html)
        self.assertNotIn("searchPlaceholder", html)
        self.assertIn('id="work-search"', html)
        self.assertIn('id="work-repo-filter"', html)
        self.assertIn("workSearchQuery", html)
        self.assertIn("workRepoFilter", html)
        self.assertIn("function filteredWorkItems", html)
        self.assertIn("function workItemTitleMatches", html)
        self.assertIn("function taskIdMatchesWorkSearch", html)
        self.assertIn("renderWorkFilters(data)", html)
        self.assertIn("grid-template-columns: minmax(260px, 340px) minmax(260px, 340px) minmax(0, 1fr)", html)
        self.assertIn("timeline-filters", html)
        self.assertIn("timelineHiddenTypes: new Set(['daemon_heartbeat'])", html)
        self.assertIn("function fmtShort(value)", html)
        self.assertIn("badge(fmtShort(item.latest_task_updated_at)", html)
        self.assertIn("${esc(fmtShort(task.updated_at))}", html)
        self.assertIn("field('Updated', fmt(task.updated_at))", html)
        self.assertIn("${date.getFullYear()}-${pad2(date.getMonth() + 1)}", html)
        self.assertIn("brand-mark", html)

        self.assertIn("const translations", html)
        self.assertIn("setTheme(", html)
        self.assertIn("setLanguage(", html)
        self.assertIn("localStorage.getItem('robertWorkbenchTheme')", html)
        self.assertIn("localStorage.getItem('robertLanguage')", html)
        self.assertIn("function repoContext(data) { const repos = data.repos || []", html)
        self.assertNotIn("value.match(/github:", html)
        self.assertIn("operator-next-steps", html)
        self.assertIn("daemon-health", html)
        self.assertIn("daemon-events", html)
        self.assertIn("renderDaemon(data)", html)
        self.assertIn("renderNotifications(data)", html)
        self.assertIn("artifact-preview", html)
        self.assertIn("artifact-preview-drawer", html)
        self.assertIn("preview-scrim", html)
        self.assertIn("data-close-artifact-preview", html)
        self.assertIn("class=\"artifact-preview placeholder\"", html)
        self.assertIn("openArtifactDrawer", html)
        self.assertIn("closeArtifactDrawer", html)
        self.assertIn("renderMarkdown(content)", html)
        self.assertIn("highlightJson(content)", html)
        self.assertIn("gh-code", html)
        self.assertIn("json-key", html)
        self.assertIn("/knowledge/propose", html)
        self.assertIn("auto-refresh-toggle", html)
        self.assertIn("refresh-interval", html)
        self.assertIn("target=\"_blank\"", html)
        self.assertIn("rel=\"noopener noreferrer\"", html)
        self.assertIn("location.hash", html)
        self.assertIn("openTask(", html)
        self.assertIn("previewArtifact", html)
        self.assertIn("sourceTitle(item)", html)
        self.assertIn("sourceMeta(item)", html)

    def test_web_pages_share_primary_navigation_and_language_preference(self):
        from robert_agent import web
        from robert_agent import storage

        db_path = self.root / "shared-navigation.sqlite3"
        storage.init_database(db_path)

        operations_html = web.render_dashboard_html()
        self.assertIn('href="/assets/github-shell.css"', operations_html)
        self.assertIn('href="/assets/operations.css"', operations_html)
        self.assertIn('class="global-bar"', operations_html)
        self.assertIn('class="brand"', operations_html)
        self.assertIn('class="underline-nav"', operations_html)
        self.assertIn('class="underline-nav-item" href="/"', operations_html)
        self.assertIn('class="underline-nav-item" href="/board"', operations_html)
        self.assertIn('class="underline-nav-item selected" data-primary-view="operations" href="/operations"', operations_html)
        self.assertIn('data-i18n="primaryWork"', operations_html)
        self.assertIn('data-i18n="navHistory"', operations_html)
        self.assertIn('data-i18n="navKnowledge"', operations_html)
        self.assertIn("navBoard: 'Board'", operations_html)
        self.assertIn("navBoard: '任务看板'", operations_html)
        self.assertNotIn('class="nav-button" type="button" data-view="work"', operations_html)
        self.assertNotIn('class="nav-button" type="button" data-view="knowledge"', operations_html)
        self.assertIn('id="nav-count-command">0</span>', operations_html)
        self.assertIn('id="nav-count-timeline">0</span>', operations_html)
        self.assertIn('id="nav-count-notifications">0</span>', operations_html)
        self.assertIn('id="nav-count-daemon">0</span>', operations_html)
        self.assertNotIn('id="global-search"', operations_html)

        status, _headers, board_html = web.build_http_response("/board", db_path)
        self.assertEqual(status, 200)
        self.assertIn(b'class="global-bar"', board_html)
        self.assertIn(b'href="/assets/github-shell.css"', board_html)
        self.assertIn(b'class="brand"', board_html)
        self.assertIn(b'class="underline-nav"', board_html)
        self.assertIn(b'class="underline-nav-item" href="/"', board_html)
        self.assertIn(b'class="underline-nav-item selected" href="/board"', board_html)
        self.assertIn(b'class="underline-nav-item" href="/operations"', board_html)
        self.assertIn(b'data-i18n="navWork"', board_html)
        self.assertIn(b'data-i18n="navHistory"', board_html)
        self.assertIn(b'data-i18n="navKnowledge"', board_html)
        self.assertIn(b'id="language-select"', board_html)
        self.assertIn(b'id="theme-select"', board_html)
        self.assertIn(b'data-i18n="newTask"', board_html)
        self.assertNotIn(b'id="global-search"', board_html)

        status, _headers, workbench_html = web.build_http_response("/", db_path)
        self.assertEqual(status, 200)
        self.assertIn(b'id="language-select"', workbench_html)
        self.assertIn(b'id="theme-select"', workbench_html)
        self.assertIn(b'data-i18n="navWork"', workbench_html)
        self.assertIn(b'data-i18n="navHistory"', workbench_html)
        self.assertIn(b'data-i18n-placeholder="workSearchPlaceholder"', workbench_html)
        self.assertNotIn(b'id="global-search"', workbench_html)

        _status, _headers, board_css = web.build_http_response(
            "/assets/board.css", db_path
        )
        self.assertNotIn(b"Avenir Next Condensed", board_css)

        shell_status, shell_headers, shell_css = web.build_http_response(
            "/assets/github-shell.css", db_path
        )
        self.assertEqual(shell_status, 200)
        self.assertEqual(shell_headers["content-type"], "text/css; charset=utf-8")
        self.assertIn(b"--bgColor-default", shell_css)
        self.assertIn(b".global-bar", shell_css)
        self.assertIn(b".underline-nav", shell_css)
        self.assertIn(b"-apple-system", shell_css)

        operations_status, operations_headers, operations_css = web.build_http_response(
            "/assets/operations.css", db_path
        )
        self.assertEqual(operations_status, 200)
        self.assertEqual(
            operations_headers["content-type"], "text/css; charset=utf-8"
        )
        self.assertIn(b".mission-shell > .global-bar", operations_css)
        self.assertIn(b"var(--bgColor-default)", operations_css)

        _status, _headers, board_js = web.build_http_response(
            "/assets/board.js", db_path
        )
        self.assertIn(b'localStorage.getItem("robertLanguage")', board_js)
        self.assertIn(b'localStorage.setItem("robertLanguage"', board_js)
        self.assertIn(b'localStorage.getItem("robertWorkbenchTheme")', board_js)
        self.assertIn(b'localStorage.setItem("robertWorkbenchTheme"', board_js)
        self.assertIn(b"function applyTranslations()", board_js)

        _status, _headers, workbench_js = web.build_http_response(
            "/assets/workbench.js", db_path
        )
        self.assertIn(b'localStorage.getItem("robertLanguage")', workbench_js)
        self.assertIn(b'localStorage.setItem("robertLanguage"', workbench_js)
        self.assertIn(b'localStorage.getItem("robertWorkbenchTheme")', workbench_js)
        self.assertIn(b'localStorage.setItem("robertWorkbenchTheme"', workbench_js)
        self.assertIn(b"function applyTranslations()", workbench_js)

    def test_web_routes_root_to_html_and_data_json_to_payload(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        status, headers, body = web.build_http_response("/", db_path)
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/html; charset=utf-8")
        self.assertIn(b'<main id="workbench-app"', body)

        status, headers, body = web.build_http_response("/operations", db_path)
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/html; charset=utf-8")
        self.assertIn(b'<main id="dashboard-app"', body)

        status, headers, body = web.build_http_response("/data.json", db_path)
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(payload["summary"]["tasks"], 2)

        status, headers, body = web.build_http_response("/history", db_path)
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(payload["recent_tasks"][0]["task_id"], "task-2")

    def test_web_serves_board_shell_assets_and_rejects_asset_traversal(self):
        from robert_agent import web
        from robert_agent import storage

        db_path = self.root / "board-shell.sqlite3"
        storage.init_database(db_path)

        status, headers, body = web.build_http_response("/board", db_path)
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/html; charset=utf-8")
        for text in (
            b"Backlog",
            b"TODO",
            b"Doing",
            b"Waiting for you",
            b"Review",
            b"Done",
            b"New task",
            b"Operations",
        ):
            self.assertIn(text, body)
        self.assertIn(b'id="mobile-column-tabs"', body)
        self.assertIn(b'id="work-item-drawer"', body)
        self.assertIn(b'aria-live="polite"', body)

        css_status, css_headers, css_body = web.build_http_response(
            "/assets/board.css", db_path
        )
        js_status, js_headers, js_body = web.build_http_response(
            "/assets/board.js", db_path
        )
        self.assertEqual(css_status, 200)
        self.assertEqual(css_headers["content-type"], "text/css; charset=utf-8")
        self.assertIn(b"@media", css_body)
        self.assertIn(b"[hidden]", css_body)
        body_rules = css_body.decode("utf-8").split("body {", 1)[1].split("}", 1)[0]
        self.assertIn("height: 100dvh;", {line.strip() for line in body_rules.splitlines()})
        self.assertEqual(js_status, 200)
        self.assertEqual(js_headers["content-type"], "text/javascript; charset=utf-8")
        self.assertIn(b"/api/board", js_body)
        favicon_status, _headers, favicon_body = web.build_http_response(
            "/favicon.ico", db_path
        )
        self.assertEqual(favicon_status, 204)
        self.assertEqual(favicon_body, b"")

        traversal_status, _headers, _body = web.build_http_response(
            "/assets/../web.py", db_path
        )
        self.assertEqual(traversal_status, 404)

    def test_board_api_session_create_read_and_conflict_contract(self):
        from robert_agent import web
        from robert_agent import storage

        db_path = self.root / "board-api.sqlite3"
        storage.init_database(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root) VALUES ('repo-1', 'example/repo', 'robot', 'main', '/repo', '/worktrees')"
            )
        context = web.ControlContext(
            db_path=str(db_path),
            operator_identity="owner",
            allowed_repo_ids=frozenset({"repo-1"}),
            allowed_workers=frozenset({"default"}),
            allowed_origins=frozenset({"http://127.0.0.1:8765"}),
            csrf_token="csrf-test",
            writes_enabled=True,
            write_error=None,
        )
        session_status, _headers, session_body = web.build_http_response(
            "/api/session", db_path, control_context=context
        )
        self.assertEqual(session_status, 200)
        self.assertEqual(json.loads(session_body)["csrf_token"], "csrf-test")

        request_headers = {
            "host": "127.0.0.1:8765",
            "origin": "http://127.0.0.1:8765",
            "content-type": "application/json",
            "x-robert-csrf-token": "csrf-test",
            "x-idempotency-key": "create-api-1",
        }
        create_status, _headers, create_body = web.handle_dashboard_post(
            "/api/work-items",
            json.dumps(
                {
                    "repo_id": "repo-1",
                    "title": "API card",
                    "description": "Create from the local board.",
                    "priority": "P1",
                    "routing_mode": "auto",
                    "requested_worker": None,
                    "mode": "backlog",
                }
            ).encode(),
            db_path,
            control_context=context,
            request_headers=request_headers,
        )
        self.assertEqual(create_status, 201, create_body)
        created = json.loads(create_body)
        work_item_id = created["work_item_id"]

        board_status, _headers, board_body = web.build_http_response(
            "/api/board?repo=repo-1", db_path, control_context=context
        )
        detail_status, _headers, detail_body = web.build_http_response(
            f"/api/work-items/{work_item_id}", db_path, control_context=context
        )
        self.assertEqual(board_status, 200)
        self.assertEqual(detail_status, 200)
        self.assertEqual(json.loads(board_body)["items"][0]["title"], "API card")
        detail = json.loads(detail_body)

        missing_csrf = dict(request_headers)
        missing_csrf.pop("x-robert-csrf-token")
        denied_status, _headers, _body = web.handle_dashboard_post(
            f"/api/work-items/{work_item_id}/commands",
            json.dumps({"command": "start", "expected_version": detail["version"]}).encode(),
            db_path,
            control_context=context,
            request_headers=missing_csrf,
        )
        self.assertEqual(denied_status, 403)

        command_headers = {**request_headers, "x-idempotency-key": "start-api-1"}
        start_status, _headers, start_body = web.handle_dashboard_post(
            f"/api/work-items/{work_item_id}/commands",
            json.dumps({"command": "start", "expected_version": detail["version"]}).encode(),
            db_path,
            control_context=context,
            request_headers=command_headers,
        )
        self.assertEqual(start_status, 200, start_body)
        stale_headers = {**request_headers, "x-idempotency-key": "edit-stale-api"}
        stale_status, _headers, stale_body = web.handle_dashboard_post(
            f"/api/work-items/{work_item_id}/commands",
            json.dumps(
                {
                    "command": "edit",
                    "expected_version": detail["version"],
                    "title": "Stale edit",
                }
            ).encode(),
            db_path,
            control_context=context,
            request_headers=stale_headers,
        )
        self.assertEqual(stale_status, 409)
        self.assertEqual(json.loads(stale_body)["current"]["work_item_id"], work_item_id)

    def test_board_api_rejects_bad_filters_and_read_only_writes(self):
        from robert_agent import web
        from robert_agent import storage

        db_path = self.root / "board-readonly.sqlite3"
        storage.init_database(db_path)
        context = web.ControlContext(
            db_path=str(db_path),
            operator_identity="owner",
            allowed_repo_ids=frozenset(),
            allowed_workers=frozenset(),
            allowed_origins=frozenset({"http://127.0.0.1:8765"}),
            csrf_token="csrf-test",
            writes_enabled=False,
            write_error="Start with --config to enable writes.",
        )
        status, _headers, _body = web.build_http_response(
            "/api/board?column=unknown", db_path, control_context=context
        )
        self.assertEqual(status, 422)
        write_status, _headers, _body = web.handle_dashboard_post(
            "/api/work-items",
            b"{}",
            db_path,
            control_context=context,
            request_headers={
                "host": "127.0.0.1:8765",
                "origin": "http://127.0.0.1:8765",
                "content-type": "application/json",
                "x-robert-csrf-token": "csrf-test",
                "x-idempotency-key": "readonly-1",
            },
        )
        self.assertEqual(write_status, 503)
        knowledge_status, _headers, _body = web.handle_dashboard_post(
            "/knowledge/propose",
            b"repo_id=repo-1",
            db_path,
            control_context=context,
            request_headers={
                "host": "127.0.0.1:8765",
                "origin": "http://127.0.0.1:8765",
                "content-type": "application/x-www-form-urlencoded",
                "x-robert-csrf-token": "csrf-test",
            },
        )
        self.assertEqual(knowledge_status, 503)

    def test_web_config_mode_initializes_control_context_and_repos(self):
        from robert_agent import web
        config_path = self._write_acceptance_config()
        context = web.build_control_context_from_config(
            config_path,
            host="127.0.0.1",
            port=9876,
            operator_identity="local-owner",
            csrf_token="fixed-token",
        )

        self.assertTrue(context.writes_enabled)
        self.assertEqual(context.csrf_token, "fixed-token")
        self.assertEqual(context.allowed_repo_ids, frozenset({"repo:x/y"}))
        self.assertEqual(context.allowed_workers, frozenset({"default"}))
        self.assertEqual(context.allowed_origins, frozenset({"http://127.0.0.1:9876"}))
        with closing(sqlite3.connect(context.db_path)) as conn:
            repo = conn.execute(
                "SELECT repo_id, full_name FROM repos WHERE repo_id = 'repo:x/y'"
            ).fetchone()
        self.assertEqual(repo, ("repo:x/y", "x/y"))

    def test_web_http_handler_passes_control_context_and_limits_body(self):
        import http.client
        import threading

        from robert_agent import web
        from robert_agent import storage

        db_path = self.root / "handler.sqlite3"
        storage.init_database(db_path)
        server = web.ThreadingHTTPServer(("127.0.0.1", 0), None)
        port = server.server_address[1]
        context = web.ControlContext(
            db_path=str(db_path),
            operator_identity="owner",
            allowed_repo_ids=frozenset(),
            allowed_workers=frozenset(),
            allowed_origins=frozenset({f"http://127.0.0.1:{port}"}),
            csrf_token="handler-token",
            writes_enabled=False,
            write_error="read only",
        )
        server.RequestHandlerClass = web.make_handler(db_path, context)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        connection.request("GET", "/api/session")
        response = connection.getresponse()
        session = json.loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertEqual(session["csrf_token"], "handler-token")

        connection.request(
            "POST",
            "/api/work-items",
            body=b"x" * (64 * 1024 + 1),
            headers={"content-type": "application/json"},
        )
        response = connection.getresponse()
        response.read()
        self.assertEqual(response.status, 413)
        connection.close()

    def test_web_serves_github_native_workbench_assets(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        status, headers, body = web.build_http_response("/", db_path)
        html = body.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/html; charset=utf-8")
        self.assertIn('id="workbench-app"', html)
        self.assertIn('class="underline-nav"', html)
        self.assertIn('id="work-list"', html)
        self.assertIn('id="detail-panel"', html)
        self.assertIn('/assets/github-shell.css', html)
        self.assertIn('/assets/workbench.css', html)
        self.assertIn('/assets/workbench.js', html)
        self.assertIn('id="language-select"', html)
        self.assertNotIn('id="global-search"', html)
        self.assertNotIn("bootstrap", html.lower())

        status, headers, body = web.build_http_response(
            "/assets/workbench.css", db_path
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/css; charset=utf-8")
        self.assertIn(b".workbench-shell", body)

        status, headers, body = web.build_http_response(
            "/assets/github-shell.css", db_path
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/css; charset=utf-8")
        self.assertIn(b"--bgColor-default", body)

        status, headers, body = web.build_http_response(
            "/assets/workbench.js", db_path
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            headers["content-type"], "application/javascript; charset=utf-8"
        )
        self.assertIn(b"function loadWorkItems", body)
        self.assertIn(b"const translations", body)
        self.assertIn(b"function setLanguage", body)
        self.assertNotIn(b"/data.json", body)

    def test_web_serves_registered_text_artifact_preview(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        log_path = self.root / "worker.stderr.log"
        log_path.write_text("line 1\nworker failed clearly\n", encoding="utf-8")
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE artifacts
                SET path = ?, bytes = ?
                WHERE task_id = 'task-1' AND artifact_type = 'worker_stderr'
                """,
                (str(log_path), log_path.stat().st_size),
            )

        status, headers, body = web.build_http_response(
            "/artifact.txt?task_id=task-1&artifact_type=worker_stderr",
            db_path,
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/plain; charset=utf-8")
        self.assertIn(b"worker failed clearly", body)
        self.assertIn(b"artifact_type=worker_stderr", body)

    def test_web_rejects_unregistered_artifact_preview(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        status, headers, body = web.build_http_response(
            "/artifact.txt?task_id=task-2&artifact_type=worker_stderr",
            db_path,
        )

        self.assertEqual(status, 404)
        self.assertEqual(headers["content-type"], "text/plain; charset=utf-8")
        self.assertIn(b"artifact not found", body)

    def test_chat_status_formats_overview(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        code, payload = self._run_chat_status("--db", str(db_path), "status")

        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["format"], "markdown")
        self.assertIn("Robert status", payload["message"])
        self.assertIn("Tasks: 2", payload["message"])
        self.assertIn("Wakeups pending: 1", payload["message"])
        self.assertIn("Results accepted:", payload["message"])
        self.assertIn("publish_accepted_github_actions", payload["message"])
        self.assertIn("task-1", payload["message"])

    def test_chat_status_formats_task_detail(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        code, payload = self._run_chat_status("--db", str(db_path), "task", "task-1")

        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertIn("Task task-1", payload["message"])
        self.assertIn("Lifecycle: completed", payload["message"])
        self.assertIn("Next action: publish_accepted_github_actions", payload["message"])
        self.assertIn("worker_stderr", payload["message"])

    def test_chat_status_formats_run_steps(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        code, payload = self._run_chat_status("--db", str(db_path), "run", "run-1")

        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertIn("Run run-1", payload["message"])
        self.assertIn("validate_config: succeeded", payload["message"])
        self.assertIn("dispatch: failed", payload["message"])

    def test_chat_status_formats_partial_run_repo_summaries(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE agent_runs
                SET summary_json = ?
                WHERE run_id = 'run-1'
                """,
                (
                    json.dumps(
                        {
                            "overall_status": "partial_failure",
                            "repo_summaries": [
                                {
                                    "repo_id": "repo-1",
                                    "full_name": "example/backend",
                                    "status": "failed",
                                }
                            ],
                        },
                        sort_keys=True,
                    ),
                ),
            )

        code, payload = self._run_chat_status("--db", str(db_path), "run", "run-1")

        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertIn("Status: partial_failure", payload["message"])
        self.assertIn("Repos:", payload["message"])
        self.assertIn("- example/backend: failed", payload["message"])

    def test_chat_status_previews_registered_artifact(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        log_path = self.root / "worker.stderr.log"
        log_path.write_text("line 1\nworker failed clearly\n", encoding="utf-8")
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE artifacts
                SET path = ?, bytes = ?
                WHERE task_id = 'task-1' AND artifact_type = 'worker_stderr'
                """,
                (str(log_path), log_path.stat().st_size),
            )

        code, payload = self._run_chat_status(
            "--db",
            str(db_path),
            "artifact",
            "task-1",
            "worker_stderr",
        )

        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertIn("Artifact worker_stderr for task-1", payload["message"])
        self.assertIn("worker failed clearly", payload["message"])

    def test_chat_status_reports_missing_task(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        code, payload = self._run_chat_status("--db", str(db_path), "task", "missing-task")

        self.assertEqual(code, 3)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "not_found")
        self.assertIn("missing-task", payload["safe_error"])

    def test_status_reports_compact_overview_json(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        code, payload = self._run_status("--db", str(db_path), "status")

        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["format"], "json")
        self.assertTrue(payload["checked_at_utc"].endswith("Z"))
        self.assertEqual(payload["data"]["summary"]["tasks"], 2)
        self.assertEqual(payload["data"]["wakeup_summary"]["pending"], 1)
        self.assertIn("acceptance_metrics", payload["data"])
        self.assertEqual(payload["data"]["latest_run"]["run_id"], "run-1")
        self.assertEqual(payload["data"]["recent_tasks"][0]["task_id"], "task-2")

    def test_status_reports_task_by_id_without_history_markdown(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        code, payload = self._run_status("--db", str(db_path), "task", "task-1")

        self.assertEqual(code, 0)
        data = payload["data"]
        self.assertEqual(data["task_id"], "task-1")
        self.assertEqual(data["workstream_id"], "ws-1")
        self.assertEqual(data["latest_attempt"]["attempt_id"], "attempt-1")
        self.assertEqual(data["latest_phase"]["phase"], "analyze")
        self.assertEqual(data["latest_result"]["result_id"], "result-1")
        self.assertEqual(data["results"][0]["result_id"], "result-1")
        self.assertEqual(data["results"][0]["handoff"], "ready")
        self.assertEqual(data["action_counts"]["total"], 1)
        self.assertEqual(data["latest_notification"]["notification_id"], "notification-1")
        self.assertEqual(data["actions"][0]["action_id"], "action-1")
        self.assertIn("prompt", {artifact["artifact_type"] for artifact in data["artifacts"]})
        self.assertEqual(data["events"][0]["event_fingerprint"], "comment:1")

    def test_status_reports_run_attempt_and_workstream_by_id(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        run_code, run_payload = self._run_status("--db", str(db_path), "run", "run-1")
        attempt_code, attempt_payload = self._run_status(
            "--db", str(db_path), "attempt", "attempt-1"
        )
        stream_code, stream_payload = self._run_status(
            "--db", str(db_path), "workstream", "ws-1"
        )

        self.assertEqual(run_code, 0)
        self.assertEqual(run_payload["data"]["steps"][1]["step_key"], "dispatch")
        self.assertEqual(attempt_code, 0)
        self.assertEqual(attempt_payload["data"]["task_id"], "task-1")
        self.assertEqual(attempt_payload["data"]["dispatch"]["command"]["argv0"], "cbc")
        self.assertEqual(attempt_payload["data"]["dispatch"]["command"]["argc"], 4)
        self.assertNotIn("very long prompt", json.dumps(attempt_payload["data"]["dispatch"]))
        self.assertEqual(attempt_payload["data"]["process"]["pid"], os.getpid())
        self.assertEqual(attempt_payload["data"]["process"]["status"], "running")
        self.assertEqual(attempt_payload["data"]["phases"][0]["phase"], "analyze")
        self.assertEqual(stream_code, 0)
        self.assertEqual(stream_payload["data"]["workstream_id"], "ws-1")
        self.assertEqual(stream_payload["data"]["tasks"][0]["task_id"], "task-2")

    def test_status_reports_latest_recent_event_and_artifact(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        latest_code, latest_payload = self._run_status("--db", str(db_path), "run", "latest")
        runs_code, runs_payload = self._run_status("--db", str(db_path), "runs", "--limit", "3")
        event_code, event_payload = self._run_status("--db", str(db_path), "event", "1")
        events_code, events_payload = self._run_status(
            "--db", str(db_path), "events", "analysis", "--limit", "5"
        )
        source_code, source_payload = self._run_status("--db", str(db_path), "source", "1")
        artifact_code, artifact_payload = self._run_status(
            "--db",
            str(db_path),
            "artifact",
            "task-1",
            "worker_stderr",
            "--max-bytes",
            "64",
        )

        self.assertEqual(latest_code, 0)
        self.assertEqual(latest_payload["data"]["run_id"], "run-1")
        self.assertEqual(runs_code, 0)
        self.assertEqual(runs_payload["data"][0]["run_id"], "run-1")
        self.assertEqual(runs_payload["data"][0]["step_status_counts"]["failed"], 1)
        self.assertEqual(event_code, 0)
        self.assertEqual(event_payload["data"]["event_fingerprint"], "comment:1")
        self.assertEqual(event_payload["data"]["tasks"][0]["task_id"], "task-1")
        self.assertEqual(event_payload["data"]["tasks"][0]["workstream_id"], "ws-1")
        self.assertIsNone(event_payload["data"]["tasks"][0]["parent_task_id"])
        self.assertEqual(event_payload["data"]["tasks"][0]["worktree_path"], "/repo/.worktrees/task-1")
        self.assertEqual(event_payload["data"]["tasks"][0]["branch_name"], "codex/dd-1-task")
        self.assertEqual(events_code, 0)
        self.assertEqual(events_payload["data"][0]["event_fingerprint"], "comment:1")
        self.assertEqual(events_payload["data"][0]["linked_task_count"], 1)
        self.assertEqual(source_code, 0)
        self.assertEqual(source_payload["data"]["sources"][0]["source_key"], "github:example/backend#1")
        self.assertEqual(source_payload["data"]["workstreams"][0]["workstream_id"], "ws-1")
        self.assertEqual(source_payload["data"]["tasks"][0]["task_id"], "task-1")
        self.assertEqual(source_payload["data"]["tasks"][0]["worktree_path"], "/repo/.worktrees/task-1")
        self.assertEqual(artifact_code, 0)
        self.assertIn("stderr tail", artifact_payload["data"]["content"])
        self.assertEqual(artifact_payload["data"]["artifact_type"], "worker_stderr")

    def test_status_reports_current_state_closed_source(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        payload = {
            "status": "current_state_closed",
            "source_key": "github:example/backend#1",
            "source_type": "issue",
            "number": 1,
            "terminal_reason": "remote_source_closed",
            "skip_reason": "source_already_closed_before_dispatch",
        }
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE github_sources
                SET state = 'closed', metadata_json = ?
                WHERE source_id = 'source-1'
                """,
                (json.dumps({"current_state_reconciliation": payload}, sort_keys=True),),
            )
            conn.execute(
                """
                INSERT INTO audit_events(
                  audit_id, repo_id, workstream_id, task_id, event_type, created_at, payload_json
                )
                VALUES ('audit-current-state-closed', 'repo-1', NULL, NULL, 'current_state_closed', ?, ?)
                """,
                (datetime.now(timezone.utc).isoformat(), json.dumps(payload, sort_keys=True)),
            )

        status_code, status_payload = self._run_status("--db", str(db_path), "status")
        source_code, source_payload = self._run_status("--db", str(db_path), "source", "1")

        self.assertEqual(status_code, 0)
        self.assertEqual(status_payload["data"]["summary"]["skipped_closed_sources"], 1)
        self.assertEqual(source_code, 0)
        source_metadata = source_payload["data"]["sources"][0]["metadata"]
        self.assertEqual(
            source_metadata["current_state_reconciliation"]["status"],
            "current_state_closed",
        )
        self.assertEqual(
            source_metadata["current_state_reconciliation"]["skip_reason"],
            "source_already_closed_before_dispatch",
        )

    def test_status_reports_missing_ids(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        code, payload = self._run_status("--db", str(db_path), "attempt", "missing-attempt")

        self.assertEqual(code, 3)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "not_found")
        self.assertIn("missing-attempt", payload["safe_error"])

    def test_web_post_approves_knowledge_candidate_locally(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        self._record_knowledge_candidate(db_path)

        status, headers, body = web.handle_dashboard_post(
            "/knowledge/approve",
            (
                "candidate_id=kc-1&scope_type=route&scope_value=update-existing-pr"
                "&approved_by=wklken"
            ).encode("utf-8"),
            db_path,
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
        result = json.loads(body.decode("utf-8"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "approved")
        with closing(sqlite3.connect(db_path)) as conn, conn:
            candidate = conn.execute(
                "SELECT status, reviewer FROM knowledge_candidates WHERE candidate_id = 'kc-1'"
            ).fetchone()
            runtime_count = conn.execute(
                "SELECT COUNT(*) FROM runtime_knowledge WHERE candidate_id = 'kc-1'"
            ).fetchone()[0]
        self.assertEqual(candidate, ("approved", "wklken"))
        self.assertEqual(runtime_count, 1)

    def test_web_post_proposes_knowledge_candidates_locally(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        self._record_project_memory_entry(db_path)

        status, headers, body = web.handle_dashboard_post(
            "/knowledge/propose",
            "repo_id=repo-1".encode("utf-8"),
            db_path,
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
        result = json.loads(body.decode("utf-8"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "proposed")
        self.assertEqual(result["candidate_count"], 1)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            row = conn.execute(
                """
                SELECT title, status
                FROM knowledge_candidates
                WHERE repo_id = 'repo-1'
                """
            ).fetchone()
        self.assertEqual(row, ("Prefer update existing PR", "pending"))

    def test_web_post_rejects_knowledge_candidate_locally(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        self._record_knowledge_candidate(db_path)

        status, _headers, body = web.handle_dashboard_post(
            "/knowledge/reject",
            "candidate_id=kc-1&reviewer=wklken&review_note=too+narrow".encode("utf-8"),
            db_path,
        )

        self.assertEqual(status, 200)
        result = json.loads(body.decode("utf-8"))
        self.assertEqual(result["status"], "rejected")
        with closing(sqlite3.connect(db_path)) as conn, conn:
            candidate = conn.execute(
                "SELECT status, reviewer, review_note FROM knowledge_candidates WHERE candidate_id = 'kc-1'"
            ).fetchone()
        self.assertEqual(candidate, ("rejected", "wklken", "too narrow"))

    def test_web_post_refuses_unknown_write_action(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        status, _headers, body = web.handle_dashboard_post(
            "/github/publish",
            b"action_id=action-1",
            db_path,
        )

        self.assertEqual(status, 404)
        self.assertIn(b"unsupported dashboard action", body)

    def test_web_post_refuses_knowledge_write_when_tables_are_missing(self):
        from robert_agent import web
        db_path = self.root / "old.sqlite3"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute("CREATE TABLE repos(repo_id TEXT PRIMARY KEY, full_name TEXT)")

        status, _headers, body = web.handle_dashboard_post(
            "/knowledge/propose",
            b"repo_id=repo-1",
            db_path,
        )

        self.assertEqual(status, 400)
        result = json.loads(body.decode("utf-8"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("Knowledge tables are not available", result["safe_error"])

    def test_web_exposes_task_route_decision_evidence(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        data = web.build_dashboard_data(db_path)

        task = {task["task_id"]: task for task in data["recent_tasks"]}["task-2"]
        self.assertEqual(task["route_decision_id"], "route-2")
        self.assertEqual(task["route_confidence"], "high")
        self.assertEqual(task["allowed_github_actions"], ["push_existing_pr", "open_pr", "comment"])
        self.assertEqual(
            task["recommended_skills"],
            [
                "fast-small-pr",
                "fast-code-path",
                "fast-add-tests",
                "fast-test-fix",
                "fast-preflight",
            ],
        )

    def test_web_exposes_recent_event_flow_evidence(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        data = web.build_dashboard_data(db_path)

        event = {
            event["event_fingerprint"]: event
            for event in data["recent_events"]
        }["comment:1"]
        self.assertEqual(event["event_fingerprint"], "comment:1")
        self.assertEqual(event["source_key"], "github:example/backend#1")
        self.assertEqual(event["source_type"], "issue")
        self.assertEqual(event["number"], 1)
        self.assertEqual(event["authorization_status"], "authorized")
        self.assertEqual(event["task_id"], "task-1")
        self.assertEqual(event["task_relationship"], "trigger")
        self.assertEqual(event["workstream_id"], "ws-1")
        self.assertEqual(event["payload"]["intent"], "analysis")

    def test_web_counts_event_flow_risk_states(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        data = web.build_dashboard_data(db_path)

        self.assertEqual(
            data["event_flow_counts"],
            {
                "authorization_status": {
                    "authorized": 2,
                    "pending_actor_permission": 1,
                },
                "task_relationship": {
                    "trigger": 1,
                    "unattached": 2,
                },
                "authorized_unattached": 1,
                "ignored_events": 0,
            },
        )

    def test_web_counts_ignored_events_for_operator_context(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        self._record_ignored_untrusted_event(db_path)

        data = web.build_dashboard_data(db_path)

        self.assertEqual(data["event_flow_counts"]["ignored_events"], 1)

    def test_web_counts_real_authorized_trigger_unattached_events(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        self._record_authorized_trigger_unattached_event(db_path)

        data = web.build_dashboard_data(db_path)

        self.assertEqual(data["event_flow_counts"]["authorized_unattached"], 2)
        steps = {
            (step["target_kind"], step["target_id"]): step
            for step in data["operator_next_steps"]
        }
        self.assertEqual(steps[("event", "event-authorized-trigger")]["next_action"], "inspect_recent_events")
        self.assertEqual(steps[("event", "event-authorized-trigger")]["reason"], "authorized_unattached")

    def test_web_surfaces_pending_authorization_events(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        self._record_pending_authorization_event(db_path)

        data = web.build_dashboard_data(db_path)

        alerts = {
            alert["alert_type"]: alert
            for alert in data["operator_alerts"]
        }
        self.assertEqual(
            alerts["pending_authorization_events"],
            {
                "alert_type": "pending_authorization_events",
                "count": 1,
                "severity": "warning",
                "next_action": "resolve_authorization_lookup",
            },
        )
        steps = {
            (step["target_kind"], step["target_id"]): step
            for step in data["operator_next_steps"]
        }
        self.assertEqual(steps[("event", "event-pending-authorization")]["next_action"], "resolve_authorization_lookup")
        self.assertEqual(steps[("event", "event-pending-authorization")]["reason"], "pending_authorization")

    def test_web_builds_operator_alerts_from_risk_counts(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        data = web.build_dashboard_data(db_path)

        alerts = {
            alert["alert_type"]: alert
            for alert in data["operator_alerts"]
        }
        self.assertEqual(
            alerts["authorized_unattached_events"],
            {
                "alert_type": "authorized_unattached_events",
                "count": 1,
                "severity": "warning",
                "next_action": "inspect_recent_events",
            },
        )
        self.assertEqual(
            alerts["pending_actor_permission_events"],
            {
                "alert_type": "pending_actor_permission_events",
                "count": 1,
                "severity": "warning",
                "next_action": "resolve_actor_permission",
            },
        )
        self.assertEqual(
            alerts["waiting_publish_tasks"],
            {
                "alert_type": "waiting_publish_tasks",
                "count": 1,
                "severity": "info",
                "next_action": "publish_accepted_github_actions",
            },
        )

    def test_web_operator_alerts_include_pending_events_outside_recent_window(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        self._record_pending_authorization_event(db_path)
        self._record_ignored_untrusted_event(db_path)

        data = web.build_dashboard_data(db_path, history_limit=1)

        self.assertNotEqual(
            data["recent_events"][0]["event_id"],
            "event-pending-authorization",
        )
        alerts = {
            alert["alert_type"]: alert
            for alert in data["operator_alerts"]
        }
        self.assertEqual(alerts["pending_actor_permission_events"]["count"], 1)
        self.assertEqual(alerts["pending_authorization_events"]["count"], 1)

    def test_web_operator_next_steps_include_pending_events_outside_recent_window(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        self._record_pending_authorization_event(db_path)
        self._record_ignored_untrusted_event(db_path)

        data = web.build_dashboard_data(db_path, history_limit=1)

        steps = {
            (step["target_kind"], step["target_id"]): step
            for step in data["operator_next_steps"]
        }
        self.assertEqual(
            steps[("event", "event-3")]["next_action"],
            "resolve_actor_permission",
        )
        self.assertEqual(
            steps[("event", "event-pending-authorization")]["next_action"],
            "resolve_authorization_lookup",
        )

    def test_web_builds_operator_next_steps_with_target_ids(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        data = web.build_dashboard_data(db_path)

        steps = {
            (step["target_kind"], step["target_id"]): step
            for step in data["operator_next_steps"]
        }
        self.assertEqual(
            steps[("task", "task-1")],
            {
                "target_kind": "task",
                "target_id": "task-1",
                "github_url": "https://github.com/x/y/issues/1",
                "source_key": "github:example/backend#1",
                "source_title": "Question",
                "workstream_id": "ws-1",
                "route_id": "comment-analysis",
                "severity": "info",
                "next_action": "publish_accepted_github_actions",
                "reason": "waiting_publish",
                "action_id": "action-1",
                "action_type": "comment",
                "target_url": "https://github.com/x/y/issues/1",
            },
        )
        self.assertEqual(
            steps[("event", "event-2")],
            {
                "target_kind": "event",
                "target_id": "event-2",
                "github_url": "https://github.com/x/y/issues/1",
                "source_key": "github:example/backend#1",
                "source_title": "Question",
                "event_fingerprint": "comment:2",
                "actor_login": "wklken",
                "severity": "warning",
                "next_action": "inspect_recent_events",
                "reason": "authorized_unattached",
            },
        )
        self.assertEqual(
            steps[("event", "event-3")],
            {
                "target_kind": "event",
                "target_id": "event-3",
                "github_url": "https://github.com/x/y/issues/1",
                "source_key": "github:example/backend#1",
                "source_title": "Question",
                "event_fingerprint": "comment:3",
                "actor_login": "external-user",
                "severity": "warning",
                "next_action": "resolve_actor_permission",
                "reason": "pending_actor_permission",
            },
        )

    def test_web_marks_publish_failures_as_attention_steps_with_command(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        self._record_publish_failure(db_path)

        data = web.build_dashboard_data(db_path)

        task = next(task for task in data["recent_tasks"] if task["task_id"] == "task-1")
        self.assertEqual(task["operator_state"], "needs_attention")
        self.assertEqual(task["next_action"], "inspect_github_publish_failure")
        steps = {
            (step["target_kind"], step["target_id"]): step
            for step in data["operator_next_steps"]
        }
        self.assertEqual(
            steps[("task", "task-1")],
            {
                "target_kind": "task",
                "target_id": "task-1",
                "github_url": "https://github.com/x/y/issues/1",
                "source_key": "github:example/backend#1",
                "source_title": "Question",
                "workstream_id": "ws-1",
                "route_id": "comment-analysis",
                "severity": "critical",
                "next_action": "inspect_github_publish_failure",
                "reason": "needs_attention",
                "notification_type": "github_publish_failed",
                "safe_error": "gh: authentication failed",
                "command": ["gh", "api", "repos/x/y/issues/1/comments", "-f", "body=ready"],
            },
        )

    def test_web_publish_failure_step_ignores_unrelated_latest_notification(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        self._record_publish_failure(db_path)
        self._record_unrelated_latest_notification(db_path)

        data = web.build_dashboard_data(db_path)

        steps = {
            (step["target_kind"], step["target_id"]): step
            for step in data["operator_next_steps"]
        }
        self.assertEqual(steps[("task", "task-1")]["notification_type"], "github_publish_failed")
        self.assertEqual(steps[("task", "task-1")]["safe_error"], "gh: authentication failed")
        self.assertEqual(
            steps[("task", "task-1")]["command"],
            ["gh", "api", "repos/x/y/issues/1/comments", "-f", "body=ready"],
        )

    def test_web_marks_skipped_publish_actions_as_attention(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        self._record_skipped_publish_action(db_path)

        data = web.build_dashboard_data(db_path)

        task = next(task for task in data["recent_tasks"] if task["task_id"] == "task-1")
        self.assertEqual(task["operator_state"], "needs_attention")
        self.assertEqual(task["next_action"], "inspect_github_publish_skipped")
        steps = {
            (step["target_kind"], step["target_id"]): step
            for step in data["operator_next_steps"]
        }
        self.assertEqual(steps[("task", "task-1")]["notification_type"], "github_publish_skipped")
        self.assertEqual(steps[("task", "task-1")]["safe_error"], "publisher does not support action_type close_issue")

    def test_web_waiting_for_user_with_unpublished_question_waits_for_publish(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        self._mark_summary_task_waiting_for_user(db_path)

        data = web.build_dashboard_data(db_path)

        task = next(task for task in data["recent_tasks"] if task["task_id"] == "task-1")
        self.assertEqual(task["operator_state"], "waiting_publish")
        self.assertEqual(task["next_action"], "publish_accepted_github_actions")

    def test_web_does_not_report_recovered_publish_failure_as_attention(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        self._record_recovered_publish_failure(db_path)

        data = web.build_dashboard_data(db_path)

        task = next(task for task in data["recent_tasks"] if task["task_id"] == "task-1")
        self.assertEqual(task["operator_state"], "completed")
        self.assertEqual(task["next_action"], "none")
        self.assertNotIn(
            ("task", "task-1"),
            {
                (step["target_kind"], step["target_id"])
                for step in data["operator_next_steps"]
            },
        )

    def test_web_exposes_recent_task_artifacts_for_debugging(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        data = web.build_dashboard_data(db_path)

        task = next(task for task in data["recent_tasks"] if task["task_id"] == "task-1")
        artifact_dir = self.root / "task-artifacts"
        prompt_path = artifact_dir / "prompt.md"
        stdout_path = artifact_dir / "worker.stdout.log"
        stderr_path = artifact_dir / "worker.stderr.log"
        self.assertEqual(
            task["artifacts"],
            {
                "prompt": {
                    "path": str(prompt_path),
                    "bytes": prompt_path.stat().st_size,
                },
                "worker_stderr": {
                    "path": str(stderr_path),
                    "bytes": stderr_path.stat().st_size,
                },
                "worker_stdout": {
                    "path": str(stdout_path),
                    "bytes": stdout_path.stat().st_size,
                },
            },
        )

    def test_web_exposes_recent_run_steps_for_debugging(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        data = web.build_dashboard_data(db_path)

        self.assertEqual(data["recent_runs"][0]["run_id"], "run-1")
        self.assertEqual(data["recent_runs"][0]["status"], "failed")
        steps = {
            step["step_key"]: step["status"]
            for step in data["recent_runs"][0]["steps"]
        }
        self.assertEqual(steps["validate_config"], "succeeded")
        self.assertEqual(steps["dispatch"], "failed")
        self.assertEqual(steps["summarize"], "skipped")

    def test_web_exposes_recent_run_repo_steps(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO run_repo_steps(
                  step_id, run_id, repo_id, step_key, status,
                  started_at, finished_at, output_json, error_json
                )
                VALUES ('repo-step-1', 'run-1', 'repo-1', 'discover', 'failed', ?, ?, ?, ?)
                """,
                (
                    now,
                    now,
                    json.dumps({"raw_event_count": 0}, sort_keys=True),
                    json.dumps({"status": "api_failed"}, sort_keys=True),
                ),
            )

        data = web.build_dashboard_data(db_path)
        repo_steps = data["recent_runs"][0]["repo_steps"]

        self.assertEqual(repo_steps[0]["repo_id"], "repo-1")
        self.assertEqual(repo_steps[0]["repo_full_name"], "example/backend")
        self.assertEqual(repo_steps[0]["output"]["raw_event_count"], 0)
        self.assertEqual(repo_steps[0]["error"]["status"], "api_failed")

    def test_status_run_includes_repo_steps(self):
        from robert_agent import status
        from robert_agent import storage

        data_dir = self.root / "data"
        repo_root = self.root / "repo-a"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        worktree_root = repo_root / ".worktrees"
        worktree_root.mkdir()
        config_path = self.root / "config.yml"
        db_path = data_dir / "dd.sqlite3"
        storage.init_database(db_path)
        now = "2026-07-04T00:00:00+00:00"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(
                "INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root) VALUES ('repo:a', 'Org/a', 'robot', 'main', ?, ?)",
                (str(repo_root), str(worktree_root)),
            )
            conn.execute(
                "INSERT INTO agent_runs(run_id, status, started_at, config_path, dry_run, summary_json) VALUES ('run-a', 'failed', ?, ?, 1, ?)",
                (
                    now,
                    str(config_path),
                    json.dumps(
                        {
                            "overall_status": "partial_failure",
                            "repo_summaries": [{"repo_id": "repo:a", "status": "failed"}],
                        },
                        sort_keys=True,
                    ),
                ),
            )
            conn.execute(
                "INSERT INTO run_repo_steps(step_id, run_id, repo_id, step_key, status, started_at, finished_at, output_json) VALUES ('step-a', 'run-a', 'repo:a', 'discover', 'failed', ?, ?, ?)",
                (now, now, json.dumps({"raw_event_count": 0}, sort_keys=True)),
            )

            run = status.build_run(conn, "run-a")

        self.assertEqual(run["summary"]["overall_status"], "partial_failure")
        self.assertEqual(run["repo_steps"][0]["repo_id"], "repo:a")
        self.assertEqual(run["repo_steps"][0]["repo_full_name"], "Org/a")

    def test_web_orders_recent_run_steps_by_runtime_flow(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.executemany(
                """
                INSERT INTO run_steps(step_id, run_id, step_key, status, started_at, finished_at)
                VALUES (?, 'run-1', ?, 'succeeded', ?, ?)
                """,
                [
                    ("step-audit", "audit_results", now, now),
                    ("step-publish", "publish_actions", now, now),
                    ("step-discover", "discover", now, now),
                ],
            )

        data = web.build_dashboard_data(db_path)
        step_keys = [step["step_key"] for step in data["recent_runs"][0]["steps"]]

        self.assertLess(step_keys.index("audit_results"), step_keys.index("publish_actions"))
        self.assertLess(step_keys.index("publish_actions"), step_keys.index("discover"))

    def test_web_orders_multi_repo_run_steps_by_runtime_flow(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.executemany(
                """
                INSERT INTO run_steps(step_id, run_id, step_key, status, started_at, finished_at)
                VALUES (?, 'run-1', ?, 'succeeded', ?, ?)
                """,
                [
                    ("step-acquire-lease", "acquire_lease", now, now),
                    ("step-publish", "publish_actions", now, now),
                    ("step-reconcile", "reconcile", now, now),
                    ("step-discover", "discover", now, now),
                    ("step-route", "route", now, now),
                    ("step-repo-pipelines", "repo_pipelines", now, now),
                    ("step-discover-notifications", "discover_notifications", now, now),
                ],
            )

        data = web.build_dashboard_data(db_path)
        step_keys = [step["step_key"] for step in data["recent_runs"][0]["steps"]]

        self.assertLess(step_keys.index("discover_notifications"), step_keys.index("acquire_lease"))
        self.assertLess(step_keys.index("publish_actions"), step_keys.index("reconcile"))
        self.assertLess(step_keys.index("reconcile"), step_keys.index("discover"))
        self.assertLess(step_keys.index("route"), step_keys.index("repo_pipelines"))
        self.assertLess(step_keys.index("repo_pipelines"), step_keys.index("dispatch"))

    def test_status_orders_multi_repo_run_steps_by_runtime_flow(self):
        from robert_agent import status
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.executemany(
                """
                INSERT INTO run_steps(step_id, run_id, step_key, status, started_at, finished_at)
                VALUES (?, 'run-1', ?, 'succeeded', ?, ?)
                """,
                [
                    ("step-acquire-lease", "acquire_lease", now, now),
                    ("step-publish", "publish_actions", now, now),
                    ("step-reconcile", "reconcile", now, now),
                    ("step-discover", "discover", now, now),
                    ("step-route", "route", now, now),
                    ("step-repo-pipelines", "repo_pipelines", now, now),
                    ("step-discover-notifications", "discover_notifications", now, now),
                ],
            )
            run = status.build_run(conn, "run-1")

        step_keys = [step["step_key"] for step in run["steps"]]

        self.assertLess(step_keys.index("discover_notifications"), step_keys.index("acquire_lease"))
        self.assertLess(step_keys.index("publish_actions"), step_keys.index("reconcile"))
        self.assertLess(step_keys.index("reconcile"), step_keys.index("discover"))
        self.assertLess(step_keys.index("route"), step_keys.index("repo_pipelines"))
        self.assertLess(step_keys.index("repo_pipelines"), step_keys.index("dispatch"))

    def test_web_orders_repo_steps_by_pipeline_flow(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.executemany(
                """
                INSERT INTO run_repo_steps(
                  step_id, run_id, repo_id, step_key, status, started_at, finished_at
                )
                VALUES (?, 'run-1', 'repo-1', ?, 'succeeded', ?, ?)
                """,
                [
                    ("repo-step-route", "route", now, now),
                    ("repo-step-acquire", "acquire_lease", now, now),
                    ("repo-step-reconcile", "reconcile", now, now),
                    ("repo-step-supervise", "supervise", now, now),
                ],
            )

        data = web.build_dashboard_data(db_path)
        step_keys = [step["step_key"] for step in data["recent_runs"][0]["repo_steps"]]

        self.assertEqual(step_keys, ["acquire_lease", "supervise", "reconcile", "route"])

    def test_status_orders_repo_steps_by_pipeline_flow(self):
        from robert_agent import status
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.executemany(
                """
                INSERT INTO run_repo_steps(
                  step_id, run_id, repo_id, step_key, status, started_at, finished_at
                )
                VALUES (?, 'run-1', 'repo-1', ?, 'succeeded', ?, ?)
                """,
                [
                    ("repo-step-route", "route", now, now),
                    ("repo-step-acquire", "acquire_lease", now, now),
                    ("repo-step-reconcile", "reconcile", now, now),
                    ("repo-step-supervise", "supervise", now, now),
                ],
            )
            run = status.build_run(conn, "run-1")

        step_keys = [step["step_key"] for step in run["repo_steps"]]

        self.assertEqual(step_keys, ["acquire_lease", "supervise", "reconcile", "route"])

    def test_web_exposes_recent_github_action_audit_and_publish_status(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        data = web.build_dashboard_data(db_path)

        self.assertEqual(data["recent_actions"][0]["action_id"], "action-1")
        self.assertEqual(data["recent_actions"][0]["audit_status"], "accepted")
        self.assertEqual(data["recent_actions"][0]["publish_status"], "not_published")

    def test_web_exposes_recent_notifications_for_rejected_worker_results(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        self._record_rejected_result(db_path)

        data = web.build_dashboard_data(db_path)

        notification = data["recent_notifications"][0]
        self.assertEqual(notification["notification_type"], "worker_result_rejected")
        self.assertEqual(notification["task_id"], "task-1")
        self.assertEqual(notification["metadata"]["result_id"], "result-1")
        self.assertEqual(
            notification["metadata"]["audit"]["safe_error"],
            "open_pr action missing fields: ['base']",
        )

    def test_web_exposes_worker_result_skill_evidence(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        self._record_worker_result_evidence(db_path)

        data = web.build_dashboard_data(db_path)

        result = data["recent_worker_results"][0]
        self.assertEqual(result["result_id"], "result-1")
        self.assertEqual(result["task_id"], "task-1")
        self.assertEqual(result["consumed_event_fingerprints"], ["comment:1"])
        self.assertEqual(result["verification"], [{"command": "python -m unittest", "status": "passed"}])
        self.assertEqual(result["metadata"]["used_skills"], ["fast-code-path"])
        self.assertEqual(result["metadata"]["audit"]["status"], "accepted")
        self.assertEqual(data["used_skill_counts"], {"fast-code-path": 1})

    def test_web_annotates_recent_tasks_with_operator_state(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        self._insert_operator_state_examples(db_path)

        data = web.build_dashboard_data(db_path)

        tasks = {task["task_id"]: task for task in data["recent_tasks"]}
        self.assertEqual(tasks["task-worker"]["operator_state"], "worker_running")
        self.assertEqual(tasks["task-worker"]["next_action"], "wait_for_worker_completion")
        self.assertEqual(tasks["task-worker"]["latest_attempt_status"], "running")
        self.assertEqual(tasks["task-stale"]["operator_state"], "worker_stale")
        self.assertEqual(tasks["task-stale"]["next_action"], "inspect_worker_heartbeat")
        self.assertEqual(tasks["task-stale"]["latest_attempt_status"], "stale")
        self.assertEqual(tasks["task-timeout"]["operator_state"], "needs_attention")
        self.assertEqual(tasks["task-timeout"]["next_action"], "inspect_worker_timeout")
        self.assertEqual(tasks["task-timeout"]["latest_attempt_status"], "failed")
        self.assertEqual(tasks["task-publish"]["operator_state"], "waiting_publish")
        self.assertEqual(tasks["task-publish"]["next_action"], "publish_accepted_github_actions")
        self.assertEqual(tasks["task-publish"]["pending_publish_actions"], 1)
        self.assertEqual(tasks["task-user"]["operator_state"], "waiting_user")
        self.assertEqual(tasks["task-user"]["next_action"], "wait_for_trusted_user_reply")
        self.assertEqual(tasks["task-failed"]["operator_state"], "needs_attention")
        self.assertEqual(tasks["task-failed"]["next_action"], "inspect_failure_notification")
        self.assertEqual(
            tasks["task-failed"]["latest_notification"]["metadata"]["audit"]["safe_error"],
            "open_pr action missing fields: ['base']",
        )

    def test_web_flags_repeated_worker_resume_notifications(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_operator_overview_db(db_path)
        self._record_repeated_worker_resume_notifications(db_path)

        data = web.build_dashboard_data(db_path)

        tasks = {task["task_id"]: task for task in data["recent_tasks"]}
        self.assertEqual(tasks["task-worker"]["operator_state"], "worker_retrying")
        self.assertEqual(tasks["task-worker"]["next_action"], "inspect_worker_resume")
        self.assertEqual(tasks["task-worker"]["worker_resume_count"], 2)
        self.assertEqual(data["operator_state_counts"]["worker_retrying"], 1)
        alerts = {alert["alert_type"]: alert for alert in data["operator_alerts"]}
        self.assertEqual(alerts["worker_retrying_tasks"]["severity"], "warning")

    def test_web_flags_worker_result_pending_audit(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_operator_overview_db(db_path)
        self._record_result_pending_audit_task(db_path)

        data = web.build_dashboard_data(db_path)

        tasks = {task["task_id"]: task for task in data["recent_tasks"]}
        self.assertEqual(tasks["task-result-pending"]["operator_state"], "result_pending_audit")
        self.assertEqual(tasks["task-result-pending"]["next_action"], "wait_next_cycle_or_run_once")
        self.assertEqual(tasks["task-result-pending"]["latest_attempt_status"], "completed")
        alerts = {alert["alert_type"]: alert for alert in data["operator_alerts"]}
        self.assertEqual(alerts["result_pending_audit_tasks"]["severity"], "warning")

    def test_web_counts_operator_states_for_dashboard_overview(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_operator_overview_db(db_path)

        data = web.build_dashboard_data(db_path)

        self.assertEqual(
            data["operator_state_counts"],
            {
                "needs_attention": 2,
                "waiting_publish": 1,
                "waiting_user": 1,
                "worker_running": 1,
                "worker_stale": 1,
            },
        )

    def test_web_worker_next_steps_include_attempt_evidence(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_operator_overview_db(db_path)

        data = web.build_dashboard_data(db_path)

        steps = {
            (step["target_kind"], step["target_id"]): step
            for step in data["operator_next_steps"]
        }
        self.assertEqual(steps[("task", "task-stale")]["next_action"], "inspect_worker_heartbeat")
        self.assertEqual(steps[("task", "task-stale")]["attempt_id"], "attempt-stale")
        self.assertEqual(steps[("task", "task-stale")]["attempt_status"], "stale")
        self.assertIsNotNone(steps[("task", "task-stale")]["heartbeat_at"])
        self.assertIsNotNone(steps[("task", "task-stale")]["started_at"])
        self.assertEqual(steps[("task", "task-timeout")]["next_action"], "inspect_worker_timeout")
        self.assertEqual(steps[("task", "task-timeout")]["attempt_id"], "attempt-timeout")
        self.assertEqual(steps[("task", "task-timeout")]["attempt_status"], "failed")
        self.assertIsNotNone(steps[("task", "task-timeout")]["finished_at"])

    def test_web_failure_next_steps_include_notification_evidence(self):
        from robert_agent import web
        db_path = self.root / "dd.sqlite3"
        self._init_operator_overview_db(db_path)

        data = web.build_dashboard_data(db_path)

        steps = {
            (step["target_kind"], step["target_id"]): step
            for step in data["operator_next_steps"]
        }
        self.assertEqual(steps[("task", "task-failed")]["next_action"], "inspect_failure_notification")
        self.assertEqual(steps[("task", "task-failed")]["notification_type"], "worker_result_rejected")
        self.assertEqual(steps[("task", "task-failed")]["result_id"], "result-failed")
        self.assertEqual(steps[("task", "task-failed")]["safe_error"], "open_pr action missing fields: ['base']")

    def test_summarize_counts_rejected_results_and_action_backlog(self):
        from robert_agent import summarize
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        self._record_rejected_result(db_path)
        self._record_additional_publish_failure_action(db_path)
        self._record_additional_skipped_publish_action(db_path)
        self._record_pending_authorization_event(db_path)

        summary = summarize.summarize_database(db_path)

        self.assertEqual(summary["rejected_worker_results"], 1)
        self.assertEqual(summary["failed_github_actions"], 1)
        self.assertEqual(summary["pending_publish_actions"], 1)
        self.assertEqual(summary["publish_failed_actions"], 1)
        self.assertEqual(summary["skipped_publish_actions"], 1)
        self.assertEqual(summary["pending_actor_permission_events"], 1)
        self.assertEqual(summary["pending_authorization_events"], 1)

    def test_publish_ready_comment_action_marks_published_with_external_id(self):
        from robert_agent import publish
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        calls = []

        class Completed:
            def __init__(self, stdout):
                self.returncode = 0
                self.stdout = stdout
                self.stderr = ""

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            if command[:3] == ["gh", "api", "repos/x/y/issues/1/comments?per_page=100"]:
                return Completed("[]")
            return Completed(
                json.dumps(
                    {
                        "id": 987,
                        "html_url": "https://github.com/x/y/issues/1#issuecomment-987",
                    }
                )
            )

        result = publish.publish_ready_actions(db_path, dry_run=False, run_command=fake_run)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["published_count"], 1)
        self.assertEqual(calls[0][0], ["gh", "api", "repos/x/y/issues/1/comments?per_page=100", "--paginate", "--slurp"])
        self.assertEqual(calls[1][0][:3], ["gh", "api", "repos/x/y/issues/1/comments"])
        self.assertIn(f"body={self._dd_comment_body('ready')}", calls[1][0])
        self.assertNotIn("shell", calls[1][1])
        with closing(sqlite3.connect(db_path)) as conn, conn:
            row = conn.execute(
                """
                SELECT publish_status, external_id, target_url
                FROM github_actions
                WHERE action_id = 'action-1'
                """
            ).fetchone()
        self.assertEqual(
            row,
            (
                "published",
                "987",
                "https://github.com/x/y/issues/1#issuecomment-987",
            ),
        )

    def test_publish_ready_comment_reuses_existing_comment_with_same_marker(self):
        from robert_agent import publish
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        body = self._dd_comment_body("ready")
        calls = []

        class Completed:
            returncode = 0
            stdout = json.dumps(
                [
                    [
                        {
                            "id": 987,
                            "html_url": "https://github.com/x/y/issues/1#issuecomment-987",
                            "body": body,
                        }
                    ]
                ]
            )
            stderr = ""

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            return Completed()

        result = publish.publish_ready_actions(db_path, dry_run=False, run_command=fake_run)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["published_count"], 1)
        self.assertEqual(result["deduplicated_count"], 1)
        self.assertEqual(calls, [(["gh", "api", "repos/x/y/issues/1/comments?per_page=100", "--paginate", "--slurp"], {"capture_output": True, "text": True})])
        with closing(sqlite3.connect(db_path)) as conn, conn:
            action = conn.execute(
                """
                SELECT publish_status, external_id, target_url, metadata_json
                FROM github_actions
                WHERE action_id = 'action-1'
                """
            ).fetchone()
        self.assertEqual(action[:3], ("published", "987", "https://github.com/x/y/issues/1#issuecomment-987"))
        self.assertTrue(json.loads(action[3])["publish"]["deduplicated"])

    def test_publish_ready_comment_without_marker_fails_without_posting(self):
        from robert_agent import publish
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE github_actions
                SET metadata_json = '{"body": "ready without marker"}'
                WHERE action_id = 'action-1'
                """
            )
        calls = []

        result = publish.publish_ready_actions(
            db_path,
            dry_run=False,
            run_command=lambda command, **kwargs: calls.append((command, kwargs)),
        )

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(calls, [])
        with closing(sqlite3.connect(db_path)) as conn, conn:
            action = conn.execute(
                """
                SELECT publish_status, metadata_json
                FROM github_actions
                WHERE action_id = 'action-1'
                """
            ).fetchone()
        self.assertEqual(action[0], "not_published")
        self.assertIn("robert-comment", json.loads(action[1])["publish"]["safe_error"])

    def test_publish_ready_open_pr_action_creates_pr_and_records_url(self):
        from robert_agent import publish
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE github_actions
                SET action_type = 'open_pr',
                    target_url = NULL,
                    metadata_json = ?
                WHERE action_id = 'action-1'
                """,
                (
                    json.dumps(
                        {
                            "repo": "x/y",
                            "head": "codex/dd-123",
                            "base": "master",
                            "title": "Fix timeout",
                            "body": self._dd_pr_body("Implements the fix"),
                            "draft": True,
                        },
                        sort_keys=True,
                    ),
                ),
            )
        calls = []

        class Completed:
            def __init__(self, stdout):
                self.returncode = 0
                self.stdout = stdout
                self.stderr = ""

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            if command[:3] == ["gh", "pr", "list"]:
                return Completed("[]")
            return Completed("https://github.com/x/y/pull/42\n")

        result = publish.publish_ready_actions(db_path, dry_run=False, run_command=fake_run)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["published_count"], 1)
        self.assertEqual(
            calls[0][0],
            [
                "gh",
                "pr",
                "list",
                "--repo",
                "x/y",
                "--head",
                "codex/dd-123",
                "--state",
                "open",
                "--json",
                "number,url,baseRefName,headRefName,headRepositoryOwner",
            ],
        )
        self.assertEqual(
            calls[1][0],
            [
                "gh",
                "pr",
                "create",
                "--repo",
                "x/y",
                "--head",
                "codex/dd-123",
                "--base",
                "master",
                "--title",
                "Fix timeout",
                "--body",
                self._dd_pr_body("Implements the fix"),
                "--draft",
            ],
        )
        self.assertNotIn("shell", calls[1][1])
        with closing(sqlite3.connect(db_path)) as conn, conn:
            row = conn.execute(
                """
                SELECT publish_status, external_id, target_url
                FROM github_actions
                WHERE action_id = 'action-1'
                """
            ).fetchone()
        self.assertEqual(row, ("published", "42", "https://github.com/x/y/pull/42"))

    def test_publish_ready_new_pr_pushes_branch_before_opening_pr(self):
        from robert_agent import publish
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        worktree = self.root / "worktree"
        worktree.mkdir()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE github_actions
                SET action_type = 'push_existing_pr',
                    target_url = NULL,
                    created_at = '2026-06-18T08:00:00+00:00',
                    metadata_json = ?
                WHERE action_id = 'action-1'
                """,
                (
                    json.dumps(
                        {
                            "worktree_path": str(worktree),
                            "branch": "codex/dd-123",
                            "remote": "origin",
                        },
                        sort_keys=True,
                    ),
                ),
            )
            conn.execute(
                """
                INSERT INTO github_actions(
                  action_id, result_id, task_id, action_type, target_url, external_id,
                  audit_status, publish_status, created_at, metadata_json
                )
                VALUES(
                  'action-2', 'result-1', 'task-1', 'open_pr', NULL, NULL,
                  'accepted', 'not_published', '2026-06-18T08:00:01+00:00', ?
                )
                """,
                (
                    json.dumps(
                        {
                            "repo": "x/y",
                            "head": "codex/dd-123",
                            "base": "master",
                            "title": "Fix timeout",
                            "body": self._dd_pr_body("Implements the fix"),
                        },
                        sort_keys=True,
                    ),
                ),
            )
        calls = []

        class Completed:
            def __init__(self, stdout=""):
                self.returncode = 0
                self.stdout = stdout
                self.stderr = ""

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            if command[:2] == ["git", "push"]:
                return Completed("pushed\n")
            if command[:3] == ["gh", "pr", "list"]:
                return Completed("[]")
            if command[:4] == ["git", "remote", "get-url", "origin"]:
                self.assertEqual(kwargs["cwd"], str(worktree))
                return Completed("https://github.com/x/y.git\n")
            if command[:3] == ["gh", "pr", "create"]:
                return Completed("https://github.com/x/y/pull/42\n")
            raise AssertionError(command)

        result = publish.publish_ready_actions(db_path, dry_run=False, run_command=fake_run)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["published_count"], 2)
        self.assertEqual(calls[0][0], ["git", "push", "origin", "HEAD:codex/dd-123"])
        self.assertEqual(calls[0][1]["cwd"], str(worktree))
        self.assertEqual(calls[1][0], ["git", "remote", "get-url", "origin"])
        self.assertEqual(calls[2][0][:3], ["gh", "pr", "list"])
        self.assertEqual(calls[3][0][:3], ["gh", "pr", "create"])
        self.assertEqual(calls[3][0][calls[3][0].index("--head") + 1], "codex/dd-123")
        with closing(sqlite3.connect(db_path)) as conn, conn:
            rows = conn.execute(
                """
                SELECT action_id, publish_status, external_id, target_url
                FROM github_actions
                ORDER BY created_at, action_id
                """
            ).fetchall()
        self.assertEqual(
            rows,
            [
                ("action-1", "published", "codex/dd-123", None),
                ("action-2", "published", "42", "https://github.com/x/y/pull/42"),
            ],
        )

    def test_publish_ready_open_pr_uses_fork_owner_from_published_push_action(self):
        from robert_agent import publish
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        worktree = self.root / "worktree"
        worktree.mkdir()
        branch = "codex/dd-2901-bug-mcp-proxy-arraystring-value-bug"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE github_actions
                SET action_type = 'push_existing_pr',
                    target_url = NULL,
                    external_id = ?,
                    publish_status = 'published',
                    created_at = '2026-06-18T08:00:00+00:00',
                    metadata_json = ?
                WHERE action_id = 'action-1'
                """,
                (
                    branch,
                    json.dumps(
                        {
                            "worktree_path": str(worktree),
                            "branch": branch,
                            "remote": "origin",
                        },
                        sort_keys=True,
                    ),
                ),
            )
            conn.execute(
                """
                INSERT INTO github_actions(
                  action_id, result_id, task_id, action_type, target_url, external_id,
                  audit_status, publish_status, created_at, metadata_json
                )
                VALUES(
                  'action-2', 'result-1', 'task-1', 'open_pr', NULL, NULL,
                  'accepted', 'not_published', '2026-06-18T08:00:01+00:00', ?
                )
                """,
                (
                    json.dumps(
                        {
                            "repo": "example/backend",
                            "head": branch,
                            "base": "master",
                            "title": "Fix array string value bug",
                            "body": self._dd_pr_body("Implements the fix"),
                        },
                        sort_keys=True,
                    ),
                ),
            )
        calls = []

        class Completed:
            def __init__(self, stdout=""):
                self.returncode = 0
                self.stdout = stdout
                self.stderr = ""

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            if command[:4] == ["git", "remote", "get-url", "origin"]:
                self.assertEqual(kwargs["cwd"], str(worktree))
                return Completed("https://github.com/robert-bot/blueking-apigateway.git\n")
            if command[:3] == ["gh", "pr", "list"]:
                return Completed("[]")
            if command[:3] == ["gh", "pr", "create"]:
                return Completed("https://github.com/example/backend/pull/42\n")
            raise AssertionError(command)

        result = publish.publish_ready_actions(db_path, dry_run=False, run_command=fake_run)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["published_count"], 1)
        self.assertEqual(calls[0][0], ["git", "remote", "get-url", "origin"])
        self.assertEqual(calls[1][0][:3], ["gh", "pr", "list"])
        self.assertEqual(calls[1][0][calls[1][0].index("--head") + 1], branch)
        self.assertEqual(calls[2][0][:3], ["gh", "pr", "create"])
        self.assertEqual(calls[2][0][calls[2][0].index("--head") + 1], f"robert-bot:{branch}")

    def test_publish_ready_new_pr_stops_open_pr_when_branch_push_fails(self):
        from robert_agent import publish
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        worktree = self.root / "worktree"
        worktree.mkdir()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE github_actions
                SET action_type = 'push_existing_pr',
                    target_url = NULL,
                    created_at = '2026-06-18T08:00:00+00:00',
                    metadata_json = ?
                WHERE action_id = 'action-1'
                """,
                (
                    json.dumps(
                        {
                            "worktree_path": str(worktree),
                            "branch": "codex/dd-123",
                            "remote": "origin",
                        },
                        sort_keys=True,
                    ),
                ),
            )
            conn.execute(
                """
                INSERT INTO github_actions(
                  action_id, result_id, task_id, action_type, target_url, external_id,
                  audit_status, publish_status, created_at, metadata_json
                )
                VALUES(
                  'action-2', 'result-1', 'task-1', 'open_pr', NULL, NULL,
                  'accepted', 'not_published', '2026-06-18T08:00:01+00:00', ?
                )
                """,
                (
                    json.dumps(
                        {
                            "repo": "x/y",
                            "head": "codex/dd-123",
                            "base": "master",
                            "title": "Fix timeout",
                            "body": self._dd_pr_body("Implements the fix"),
                        },
                        sort_keys=True,
                    ),
                ),
            )
        calls = []

        class Completed:
            def __init__(self, returncode=0, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            if command[:2] == ["git", "push"]:
                return Completed(returncode=1, stderr="push rejected")
            raise AssertionError(command)

        result = publish.publish_ready_actions(db_path, dry_run=False, run_command=fake_run)

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["published_count"], 0)
        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], ["git", "push", "origin", "HEAD:codex/dd-123"])
        with closing(sqlite3.connect(db_path)) as conn, conn:
            rows = conn.execute(
                """
                SELECT action_id, publish_status, external_id, target_url
                FROM github_actions
                ORDER BY created_at, action_id
                """
            ).fetchall()
        self.assertEqual(
            rows,
            [
                ("action-1", "not_published", None, None),
                ("action-2", "not_published", None, None),
            ],
        )

    def test_publish_ready_open_pr_reuses_existing_head_pr(self):
        from robert_agent import publish
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE github_actions
                SET action_type = 'open_pr',
                    target_url = NULL,
                    metadata_json = ?
                WHERE action_id = 'action-1'
                """,
                (
                    json.dumps(
                        {
                            "repo": "x/y",
                            "head": "codex/dd-123",
                            "base": "master",
                            "title": "Fix timeout",
                            "body": self._dd_pr_body("Implements the fix"),
                        },
                        sort_keys=True,
                    ),
                ),
            )
        calls = []

        class Completed:
            def __init__(self, stdout):
                self.returncode = 0
                self.stdout = stdout
                self.stderr = ""

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            if command[:3] == ["gh", "pr", "list"]:
                return Completed(
                    json.dumps(
                        [
                            {
                                "number": 42,
                                "url": "https://github.com/x/y/pull/42",
                                "baseRefName": "master",
                                "headRefName": "codex/dd-123",
                            }
                        ]
                    )
                )
            self.assertEqual(command[:4], ["gh", "pr", "view", "42"])
            return Completed(
                json.dumps(
                    {
                        "number": 42,
                        "url": "https://github.com/x/y/pull/42",
                        "baseRefName": "master",
                        "headRefName": "codex/dd-123",
                        "body": self._dd_pr_body("Existing DD PR"),
                    },
                    sort_keys=True,
                )
            )

        result = publish.publish_ready_actions(db_path, dry_run=False, run_command=fake_run)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["published_count"], 1)
        self.assertEqual(result["deduplicated_count"], 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0][:3], ["gh", "pr", "list"])
        self.assertEqual(
            calls[1][0],
            [
                "gh",
                "pr",
                "view",
                "42",
                "--repo",
                "x/y",
                "--json",
                "number,url,body,baseRefName,headRefName,headRepositoryOwner",
            ],
        )
        with closing(sqlite3.connect(db_path)) as conn, conn:
            action = conn.execute(
                """
                SELECT publish_status, external_id, target_url, metadata_json
                FROM github_actions
                WHERE action_id = 'action-1'
                """
            ).fetchone()
        self.assertEqual(action[:3], ("published", "42", "https://github.com/x/y/pull/42"))
        self.assertTrue(json.loads(action[3])["publish"]["deduplicated"])

    def test_publish_ready_open_pr_ignores_same_head_pr_without_marker(self):
        from robert_agent import publish
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE github_actions
                SET action_type = 'open_pr',
                    target_url = NULL,
                    metadata_json = ?
                WHERE action_id = 'action-1'
                """,
                (
                    json.dumps(
                        {
                            "repo": "x/y",
                            "head": "codex/dd-123",
                            "base": "master",
                            "title": "Fix timeout",
                            "body": self._dd_pr_body("Implements the fix"),
                        },
                        sort_keys=True,
                    ),
                ),
            )
        calls = []

        class Completed:
            def __init__(self, stdout):
                self.returncode = 0
                self.stdout = stdout
                self.stderr = ""

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            if command[:3] == ["gh", "pr", "list"]:
                return Completed(
                    json.dumps(
                        [
                            {
                                "number": 42,
                                "url": "https://github.com/x/y/pull/42",
                                "baseRefName": "master",
                                "headRefName": "codex/dd-123",
                            }
                        ]
                    )
                )
            if command[:3] == ["gh", "pr", "view"]:
                return Completed(
                    json.dumps(
                        {
                            "number": 42,
                            "url": "https://github.com/x/y/pull/42",
                            "baseRefName": "master",
                            "headRefName": "codex/dd-123",
                            "body": "Manual PR on the same head branch",
                        },
                        sort_keys=True,
                    )
                )
            self.assertEqual(command[:3], ["gh", "pr", "create"])
            return Completed("https://github.com/x/y/pull/43\n")

        result = publish.publish_ready_actions(db_path, dry_run=False, run_command=fake_run)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["published_count"], 1)
        self.assertEqual(result["deduplicated_count"], 0)
        self.assertEqual([call[0][:3] for call in calls], [["gh", "pr", "list"], ["gh", "pr", "view"], ["gh", "pr", "create"]])
        with closing(sqlite3.connect(db_path)) as conn, conn:
            action = conn.execute(
                """
                SELECT publish_status, external_id, target_url, metadata_json
                FROM github_actions
                WHERE action_id = 'action-1'
                """
            ).fetchone()
        self.assertEqual(action[:3], ("published", "43", "https://github.com/x/y/pull/43"))
        self.assertFalse(json.loads(action[3])["publish"]["deduplicated"])

    def test_publish_ready_open_pr_reuses_same_owner_head_pr_with_different_marker(self):
        from robert_agent import publish
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE github_actions
                SET action_type = 'open_pr',
                    target_url = NULL,
                    metadata_json = ?
                WHERE action_id = 'action-1'
                """,
                (
                    json.dumps(
                        {
                            "repo": "x/y",
                            "head": "codex/dd-123",
                            "head_owner": "robert-bot",
                            "base": "master",
                            "title": "Fix timeout",
                            "body": self._dd_pr_body("Implements the fix"),
                        },
                        sort_keys=True,
                    ),
                ),
            )
        calls = []

        class Completed:
            def __init__(self, stdout):
                self.returncode = 0
                self.stdout = stdout
                self.stderr = ""

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            if command[:3] == ["gh", "pr", "list"]:
                return Completed(
                    json.dumps(
                        [
                            {
                                "number": 42,
                                "url": "https://github.com/x/y/pull/42",
                                "baseRefName": "master",
                                "headRefName": "codex/dd-123",
                                "headRepositoryOwner": {"login": "robert-bot"},
                            }
                        ]
                    )
                )
            if command[:3] == ["gh", "pr", "view"]:
                return Completed(
                    json.dumps(
                        {
                            "number": 42,
                            "url": "https://github.com/x/y/pull/42",
                            "baseRefName": "master",
                            "headRefName": "codex/dd-123",
                            "headRepositoryOwner": {"login": "robert-bot"},
                            "body": self._dd_pr_body("Existing DD PR", task_id="task-old"),
                        },
                        sort_keys=True,
                    )
                )
            raise AssertionError(command)

        result = publish.publish_ready_actions(db_path, dry_run=False, run_command=fake_run)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["published_count"], 1)
        self.assertEqual(result["deduplicated_count"], 1)
        self.assertEqual([call[0][:3] for call in calls], [["gh", "pr", "list"], ["gh", "pr", "view"]])
        with closing(sqlite3.connect(db_path)) as conn, conn:
            metadata = json.loads(
                conn.execute(
                    "SELECT metadata_json FROM github_actions WHERE action_id = 'action-1'"
                ).fetchone()[0]
            )
        self.assertEqual(metadata["publish"]["response"]["dedupe_reason"], "matching_head")

    def test_publish_ready_open_pr_does_not_reuse_different_owner_head_pr(self):
        from robert_agent import publish
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE github_actions
                SET action_type = 'open_pr',
                    target_url = NULL,
                    metadata_json = ?
                WHERE action_id = 'action-1'
                """,
                (
                    json.dumps(
                        {
                            "repo": "x/y",
                            "head": "codex/dd-123",
                            "head_owner": "robert-bot",
                            "base": "master",
                            "title": "Fix timeout",
                            "body": self._dd_pr_body("Implements the fix"),
                        },
                        sort_keys=True,
                    ),
                ),
            )
        calls = []

        class Completed:
            def __init__(self, stdout):
                self.returncode = 0
                self.stdout = stdout
                self.stderr = ""

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            if command[:3] == ["gh", "pr", "list"]:
                return Completed(
                    json.dumps(
                        [
                            {
                                "number": 42,
                                "url": "https://github.com/x/y/pull/42",
                                "baseRefName": "master",
                                "headRefName": "codex/dd-123",
                                "headRepositoryOwner": {"login": "someone-else"},
                            }
                        ]
                    )
                )
            if command[:3] == ["gh", "pr", "view"]:
                return Completed(
                    json.dumps(
                        {
                            "number": 42,
                            "url": "https://github.com/x/y/pull/42",
                            "baseRefName": "master",
                            "headRefName": "codex/dd-123",
                            "headRepositoryOwner": {"login": "someone-else"},
                            "body": "Manual PR on the same branch name",
                        },
                        sort_keys=True,
                    )
                )
            self.assertEqual(command[:3], ["gh", "pr", "create"])
            return Completed("https://github.com/x/y/pull/43\n")

        result = publish.publish_ready_actions(db_path, dry_run=False, run_command=fake_run)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["published_count"], 1)
        self.assertEqual(result["deduplicated_count"], 0)
        self.assertEqual([call[0][:3] for call in calls], [["gh", "pr", "list"], ["gh", "pr", "view"], ["gh", "pr", "create"]])
        self.assertEqual(calls[2][0][calls[2][0].index("--head") + 1], "robert-bot:codex/dd-123")

    def test_publish_ready_open_pr_empty_stdout_does_not_crash(self):
        from robert_agent import publish
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE github_actions
                SET action_type = 'open_pr',
                    target_url = NULL,
                    metadata_json = ?
                WHERE action_id = 'action-1'
                """,
                (
                    json.dumps(
                        {
                            "repo": "x/y",
                            "head": "codex/dd-123",
                            "base": "master",
                            "title": "Fix timeout",
                            "body": self._dd_pr_body("Implements the fix"),
                        },
                        sort_keys=True,
                    ),
                ),
            )

        class Completed:
            def __init__(self, stdout):
                self.returncode = 0
                self.stdout = stdout
                self.stderr = ""

        def fake_run(command, **_kwargs):
            if command[:3] == ["gh", "pr", "list"]:
                return Completed("[]")
            return Completed("")

        result = publish.publish_ready_actions(db_path, dry_run=False, run_command=fake_run)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["published_count"], 1)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            publish_status = conn.execute(
                "SELECT publish_status FROM github_actions WHERE action_id = 'action-1'"
            ).fetchone()[0]
        self.assertEqual(publish_status, "published")

    def test_publish_ready_push_existing_pr_pushes_branch(self):
        from robert_agent import publish
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        worktree = self.root / "worktree"
        worktree.mkdir()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE github_actions
                SET action_type = 'push_existing_pr',
                    target_url = 'https://github.com/x/y/pull/42',
                    metadata_json = ?
                WHERE action_id = 'action-1'
                """,
                (
                    json.dumps(
                        {
                            "worktree_path": str(worktree),
                            "remote": "origin",
                            "branch": "codex/dd-123",
                        },
                        sort_keys=True,
                    ),
                ),
            )
        calls = []

        class Completed:
            returncode = 0
            stdout = "pushed\n"
            stderr = ""

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            return Completed()

        result = publish.publish_ready_actions(db_path, dry_run=False, run_command=fake_run)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["published_count"], 1)
        self.assertEqual(calls[0][0], ["git", "push", "origin", "HEAD:codex/dd-123"])
        self.assertEqual(calls[0][1]["cwd"], str(worktree))
        self.assertNotIn("shell", calls[0][1])
        with closing(sqlite3.connect(db_path)) as conn, conn:
            row = conn.execute(
                """
                SELECT publish_status, external_id, target_url
                FROM github_actions
                WHERE action_id = 'action-1'
                """
            ).fetchone()
        self.assertEqual(row, ("published", "codex/dd-123", "https://github.com/x/y/pull/42"))

    def test_publish_ignores_actions_not_accepted_or_already_published(self):
        from robert_agent import publish
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                "UPDATE github_actions SET audit_status = 'pending' WHERE action_id = 'action-1'"
            )
        calls = []

        result = publish.publish_ready_actions(
            db_path,
            dry_run=False,
            run_command=lambda command, **kwargs: calls.append((command, kwargs)),
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["published_count"], 0)
        self.assertEqual(calls, [])

    def test_publish_failure_keeps_action_retryable_and_records_notification(self):
        from robert_agent import publish
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        class EmptyComments:
            returncode = 0
            stdout = "[]"
            stderr = ""

        class Failed:
            returncode = 1
            stdout = ""
            stderr = "network unavailable"

        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            if command[:3] == ["gh", "api", "repos/x/y/issues/1/comments?per_page=100"]:
                return EmptyComments()
            return Failed()

        result = publish.publish_ready_actions(
            db_path,
            dry_run=False,
            run_command=fake_run,
        )

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "publish_failed")
        self.assertEqual(
            result["failures"],
            [
                {
                    "action_id": "action-1",
                    "safe_error": "network unavailable",
                    "command": ["gh", "api", "repos/x/y/issues/1/comments", "-f", f"body={self._dd_comment_body('ready')}"],
                }
            ],
        )
        with closing(sqlite3.connect(db_path)) as conn, conn:
            action = conn.execute(
                """
                SELECT publish_status, metadata_json
                FROM github_actions
                WHERE action_id = 'action-1'
                """
            ).fetchone()
            notification = conn.execute(
                """
                SELECT notification_type, status, metadata_json
                FROM notifications
                ORDER BY created_at DESC
                LIMIT 1
                """
            ).fetchone()
        self.assertEqual(action[0], "not_published")
        publish_metadata = json.loads(action[1])["publish"]
        self.assertEqual(publish_metadata["safe_error"], "network unavailable")
        self.assertEqual(
            publish_metadata["command"],
            ["gh", "api", "repos/x/y/issues/1/comments", "-f", f"body={self._dd_comment_body('ready')}"],
        )
        self.assertEqual(notification[:2], ("github_publish_failed", "recorded"))
        notification_metadata = json.loads(notification[2])
        self.assertEqual(notification_metadata["action_id"], "action-1")
        self.assertEqual(
            notification_metadata["command"],
            ["gh", "api", "repos/x/y/issues/1/comments", "-f", f"body={self._dd_comment_body('ready')}"],
        )

    def test_repeated_publish_failure_updates_existing_notification(self):
        from robert_agent import publish
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        class EmptyComments:
            returncode = 0
            stdout = "[]"
            stderr = ""

        class Failed:
            returncode = 1
            stdout = ""

            def __init__(self, stderr):
                self.stderr = stderr

        errors = iter(["network unavailable", "gh auth expired"])

        def fake_run(command, **_kwargs):
            if command[:3] == ["gh", "api", "repos/x/y/issues/1/comments?per_page=100"]:
                return EmptyComments()
            return Failed(next(errors))

        first = publish.publish_ready_actions(db_path, dry_run=False, run_command=fake_run)
        second = publish.publish_ready_actions(db_path, dry_run=False, run_command=fake_run)

        self.assertEqual(first["status"], "publish_failed")
        self.assertEqual(second["status"], "publish_failed")
        with closing(sqlite3.connect(db_path)) as conn, conn:
            count, metadata_json = conn.execute(
                """
                SELECT COUNT(*), MAX(metadata_json)
                FROM notifications
                WHERE notification_type = 'github_publish_failed'
                """
            ).fetchone()

        metadata = json.loads(metadata_json)
        self.assertEqual(count, 1)
        self.assertEqual(metadata["action_id"], "action-1")
        self.assertEqual(metadata["safe_error"], "gh auth expired")
        self.assertEqual(
            metadata["command"],
            ["gh", "api", "repos/x/y/issues/1/comments", "-f", f"body={self._dd_comment_body('ready')}"],
        )

    def test_publish_success_resolves_previous_failure_notification(self):
        from robert_agent import publish
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        class EmptyComments:
            returncode = 0
            stdout = "[]"
            stderr = ""

        class Failed:
            returncode = 1
            stdout = ""
            stderr = "network unavailable"

        class Created:
            returncode = 0
            stdout = json.dumps(
                {
                    "id": 987,
                    "html_url": "https://github.com/x/y/issues/1#issuecomment-987",
                }
            )
            stderr = ""

        create_results = iter([Failed(), Created()])

        def fake_run(command, **_kwargs):
            if command[:3] == ["gh", "api", "repos/x/y/issues/1/comments?per_page=100"]:
                return EmptyComments()
            return next(create_results)

        failed = publish.publish_ready_actions(db_path, dry_run=False, run_command=fake_run)
        published = publish.publish_ready_actions(db_path, dry_run=False, run_command=fake_run)

        self.assertEqual(failed["status"], "publish_failed")
        self.assertEqual(published["status"], "published")
        with closing(sqlite3.connect(db_path)) as conn, conn:
            notification = conn.execute(
                """
                SELECT status, metadata_json
                FROM notifications
                WHERE notification_type = 'github_publish_failed'
                """
            ).fetchone()

        metadata = json.loads(notification[1])
        self.assertEqual(notification[0], "resolved")
        self.assertEqual(metadata["action_id"], "action-1")
        self.assertEqual(metadata["safe_error"], "network unavailable")
        self.assertIn("resolved_at", metadata)
        self.assertEqual(metadata["resolved_publish_status"], "published")

    def test_no_skeleton_markers_remain(self):
        hits = []
        for path in (PACKAGE_ROOT).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "stage_2_skeleton" in text or "skeleton_main" in text:
                hits.append(str(path.relative_to(REPO_ROOT)))
        self.assertEqual(hits, [])

    def _init_notifications_db(self, db_path):
        from robert_agent import storage

        storage.init_database(db_path)

    def _run_chat_status(self, *args):
        from robert_agent import chat_status
        output = io.StringIO()
        with redirect_stdout(output):
            code = chat_status.main(list(args))
        return code, json.loads(output.getvalue())

    def _run_status(self, *args):
        from robert_agent import status
        output = io.StringIO()
        with redirect_stdout(output):
            code = status.main(list(args))
        return code, json.loads(output.getvalue())

    def _init_summary_db(self, db_path):
        from robert_agent import storage

        storage.init_database(db_path)
        now = datetime.now(timezone.utc).isoformat()
        artifact_dir = self.root / "task-artifacts"
        artifact_dir.mkdir()
        prompt_path = artifact_dir / "prompt.md"
        stdout_path = artifact_dir / "worker.stdout.log"
        stderr_path = artifact_dir / "worker.stderr.log"
        prompt_path.write_text("compact prompt path only\n", encoding="utf-8")
        stdout_path.write_text("stdout tail\n", encoding="utf-8")
        stderr_path.write_text("stderr tail\n", encoding="utf-8")
        dispatch_metadata = {
            "dispatch": {
                "ok": True,
                "status": "running",
                "task_id": "task-1",
                "attempt_id": "attempt-1",
                "pid": os.getpid(),
                "command": ["cbc", "-p", "--input-format", "text"],
                "prompt_path": str(prompt_path),
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
            }
        }
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root)
                VALUES ('repo-1', 'example/backend', 'robert-bot', 'master', '/repo', '/repo/.worktrees')
                """
            )
            conn.execute(
                """
                INSERT INTO github_sources(
                  source_id, repo_id, source_key, source_type, number, html_url,
                  title, state, author_login
                )
                VALUES (
                  'source-1', 'repo-1',
                  'github:example/backend#1',
                  'issue', 1, 'https://github.com/x/y/issues/1',
                  'Question', 'open', 'wklken'
                )
                """
            )
            conn.execute(
                """
                INSERT INTO github_events(
                  event_id, repo_id, source_id, event_fingerprint, event_type,
                  actor_login, author_association, authorization_status,
                  event_at, payload_json
                )
                VALUES (
                  'event-1', 'repo-1', 'source-1', 'comment:1', 'comment',
                  'wklken', 'OWNER', 'authorized', ?, ?
                )
                """,
                (now, json.dumps({"intent": "analysis"}, sort_keys=True)),
            )
            conn.executemany(
                """
                INSERT INTO github_events(
                  event_id, repo_id, source_id, event_fingerprint, event_type,
                  actor_login, author_association, authorization_status,
                  event_at, payload_json
                )
                VALUES (?, 'repo-1', 'source-1', ?, 'comment', ?, ?, ?, ?, ?)
                """,
                [
                    (
                        "event-2",
                        "comment:2",
                        "wklken",
                        "OWNER",
                        "authorized",
                        now,
                        json.dumps({"intent": "bug_fix"}, sort_keys=True),
                    ),
                    (
                        "event-3",
                        "comment:3",
                        "external-user",
                        None,
                        "pending_actor_permission",
                        now,
                        json.dumps({"intent": "unclear"}, sort_keys=True),
                    ),
                ],
            )
            conn.execute(
                """
                INSERT INTO workstreams(
                  workstream_id, repo_id, primary_source_id, lifecycle,
                  created_at, updated_at
                )
                VALUES ('ws-1', 'repo-1', 'source-1', 'active', ?, ?)
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO workstream_sources(workstream_id, source_id, relationship, created_at)
                VALUES ('ws-1', 'source-1', 'primary', ?)
                """,
                (now,),
            )
            conn.execute(
                """
                INSERT INTO tasks(task_id, workstream_id, lifecycle, priority, created_at, updated_at)
                VALUES
                  ('task-1', 'ws-1', 'completed', 'P1', ?, ?),
                  ('task-2', 'ws-1', 'running', 'P0', ?, ?)
                """,
                (now, now, now, now),
            )
            conn.execute(
                """
                INSERT INTO attempts(
                  attempt_id, task_id, attempt_no, status, started_at, heartbeat_at,
                  finished_at, worktree_path, branch_name, metadata_json
                )
                VALUES ('attempt-1', 'task-1', 1, 'completed', ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    now,
                    now,
                    "/repo/.worktrees/task-1",
                    "codex/dd-1-task",
                    json.dumps(dispatch_metadata, sort_keys=True),
                ),
            )
            conn.execute(
                """
                INSERT INTO worker_phases(
                  phase_id, attempt_id, phase, status, summary, next_step, created_at
                )
                VALUES ('phase-1', 'attempt-1', 'analyze', 'completed', 'Read context', 'Verify result', ?)
                """,
                (now,),
            )
            conn.executemany(
                """
                INSERT INTO route_decisions(
                  route_decision_id, task_id, route_id, expected_output,
                  allowed_github_actions_json, required_skills_json,
                  recommended_skills_json, confidence, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        "route-1",
                        "task-1",
                        "comment-analysis",
                        "comment_analysis",
                        '["comment"]',
                        '["fast-code-path", "fast-zoom-out", "fast-verify-review-point"]',
                        '["fast-code-path", "fast-zoom-out", "fast-verify-review-point"]',
                        "high",
                        now,
                    ),
                    (
                        "route-2",
                        "task-2",
                        "new-pr",
                        "new_pr",
                        '["push_existing_pr", "open_pr", "comment"]',
                        '["fast-small-pr", "fast-code-path", "fast-add-tests", "fast-test-fix", "fast-preflight"]',
                        '["fast-small-pr", "fast-code-path", "fast-add-tests", "fast-test-fix", "fast-preflight"]',
                        "high",
                        now,
                    ),
                ],
            )
            conn.execute(
                """
                INSERT INTO task_events(task_id, event_id, relationship, created_at)
                VALUES ('task-1', 'event-1', 'trigger', ?)
                """,
                (now,),
            )
            conn.execute(
                """
                INSERT INTO agent_runs(run_id, status, started_at, finished_at, config_path, dry_run, error_json)
                VALUES ('run-1', 'failed', ?, ?, '/tmp/config.yml', 0, '{"status": "failed_dispatch"}')
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO run_steps(step_id, run_id, step_key, status, started_at, finished_at)
                VALUES
                  ('step-1', 'run-1', 'validate_config', 'succeeded', ?, ?),
                  ('step-2', 'run-1', 'dispatch', 'failed', ?, ?),
                  ('step-3', 'run-1', 'summarize', 'skipped', ?, ?)
                """,
                (now, now, now, now, now, now),
            )
            conn.execute(
                """
                INSERT INTO worker_results(
                  result_id, task_id, attempt_id, output_type,
                  consumed_event_fingerprints_json, verification_json, handoff, created_at
                )
                VALUES ('result-1', 'task-1', 'attempt-1', 'comment_analysis', '["comment:1"]', '[]', 'ready', ?)
                """,
                (now,),
            )
            conn.execute(
                """
                INSERT INTO github_actions(
                  action_id, result_id, task_id, action_type, target_url, audit_status,
                  publish_status, created_at, metadata_json
                )
                VALUES (
                  'action-1', 'result-1', 'task-1', 'comment',
                  'https://github.com/x/y/issues/1', 'accepted',
                  'not_published', ?, ?
                )
                """,
                (now, json.dumps({"body": self._dd_comment_body("ready")}, sort_keys=True)),
            )
            conn.execute(
                """
                INSERT INTO notifications(
                  notification_id, task_id, notification_type, channel, status,
                  created_at, metadata_json
                )
                VALUES (
                  'notification-1', 'task-1', 'task_completed',
                  'local', 'recorded', ?, '{"summary": "ready"}'
                )
                """,
                (now,),
            )
            conn.execute(
                """
                INSERT INTO wakeups(
                  wakeup_id, repo_id, reason, dedupe_key, task_id, attempt_id,
                  result_id, status, not_before_at, created_at, updated_at, metadata_json
                )
                VALUES (
                  'wakeup-1', 'repo-1', 'worker_result_ready', 'result-1',
                  'task-1', 'attempt-1', 'result-1', 'pending', ?, ?, ?,
                  '{"recorded_by": "test"}'
                )
                """,
                (now, now, now),
            )
            conn.executemany(
                """
                INSERT INTO artifacts(
                  artifact_id, task_id, attempt_id, artifact_type, path, bytes, created_at
                )
                VALUES (?, 'task-1', 'attempt-1', ?, ?, ?, ?)
                """,
                [
                    ("artifact-prompt", "prompt", str(prompt_path), prompt_path.stat().st_size, now),
                    ("artifact-stdout", "worker_stdout", str(stdout_path), stdout_path.stat().st_size, now),
                    ("artifact-stderr", "worker_stderr", str(stderr_path), stderr_path.stat().st_size, now),
                ],
            )

    def _record_knowledge_candidate(self, db_path):
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO knowledge_candidates(
                  candidate_id, repo_id, title, summary, prompt_text,
                  candidate_type, source_memory_ids_json, evidence_json,
                  confidence, status, created_at, metadata_json
                )
                VALUES (
                  'kc-1', 'repo-1', 'Prefer update-existing-pr',
                  'DD PR follow-up should update the existing branch.',
                  'When review comments arrive on a DD PR, use update-existing-pr.',
                  'rule', '["memory-1"]', '[{"type": "test", "value": "unit"}]',
                  'medium', 'pending', ?, '{"retrieval_boost": {"keywords": ["dd-pr"]}}'
                )
                """,
                (now,),
            )

    def _record_project_memory_entry(self, db_path):
        from robert_agent import project_memory

        now = datetime.now(timezone.utc).isoformat()
        payload = {
            "memory_delta": {
                "status": "has_memory",
                "entries": [
                    {
                        "kind": "rule",
                        "title": "Prefer update existing PR",
                        "short_summary": "Review follow-up should update the current DD PR branch.",
                        "long_summary": "When review comments arrive on an existing DD PR, keep work on the same PR unless the route says otherwise.",
                        "keywords": ["dd-pr", "review-follow-up"],
                        "paths": ["src/robert_agent/run_once.py"],
                    }
                ],
            }
        }
        with closing(sqlite3.connect(db_path)) as conn, conn:
            status = project_memory.record_memory_delta(
                conn,
                result_payload=payload,
                workstream_id="ws-1",
                repo_id="repo-1",
                run_now=now,
            )
        self.assertEqual(status["status"], "recorded")

    def _insert_operator_state_examples(self, db_path):
        now = datetime.now(timezone.utc).isoformat()
        audit = {
            "ok": False,
            "status": "failed",
            "safe_error": "open_pr action missing fields: ['base']",
        }
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.executemany(
                """
                INSERT INTO workstreams(
                  workstream_id, repo_id, lifecycle, active_task_id, created_at, updated_at
                )
                VALUES (?, 'repo-1', ?, ?, ?, ?)
                """,
                [
                    ("ws-worker", "active", "task-worker", now, now),
                    ("ws-stale", "active", "task-stale", now, now),
                    ("ws-timeout", "failed", None, now, now),
                    ("ws-publish", "active", "task-publish", now, now),
                    ("ws-user", "waiting_for_user", "task-user", now, now),
                    ("ws-failed", "failed", None, now, now),
                ],
            )
            conn.executemany(
                """
                INSERT INTO tasks(
                  task_id, workstream_id, lifecycle, priority, created_at, updated_at
                )
                VALUES (?, ?, ?, 'P1', ?, ?)
                """,
                [
                    ("task-worker", "ws-worker", "running", now, now),
                    ("task-stale", "ws-stale", "running", now, now),
                    ("task-timeout", "ws-timeout", "failed", now, now),
                    ("task-publish", "ws-publish", "running", now, now),
                    ("task-user", "ws-user", "waiting_for_user", now, now),
                    ("task-failed", "ws-failed", "failed", now, now),
                ],
            )
            conn.execute(
                """
                INSERT INTO attempts(
                  attempt_id, task_id, attempt_no, status, started_at, heartbeat_at
                )
                VALUES ('attempt-worker', 'task-worker', 1, 'running', ?, ?)
                """,
                (now, now),
            )
            conn.executemany(
                """
                INSERT INTO attempts(
                  attempt_id, task_id, attempt_no, status, started_at, heartbeat_at, finished_at
                )
                VALUES (?, ?, 1, ?, ?, ?, ?)
                """,
                [
                    ("attempt-stale", "task-stale", "stale", now, now, None),
                    ("attempt-timeout", "task-timeout", "failed", now, now, now),
                ],
            )
            conn.execute(
                """
                INSERT INTO worker_results(
                  result_id, task_id, attempt_id, output_type,
                  consumed_event_fingerprints_json, verification_json, handoff,
                  created_at, metadata_json
                )
                VALUES (
                  'result-publish', 'task-publish', 'attempt-publish',
                  'comment_analysis', '["comment:1"]', '[]', 'ready', ?, '{}'
                )
                """,
                (now,),
            )
            conn.execute(
                """
                INSERT INTO github_actions(
                  action_id, result_id, task_id, action_type, target_url,
                  audit_status, publish_status, created_at, metadata_json
                )
                VALUES (
                  'action-publish', 'result-publish', 'task-publish',
                  'comment', 'https://github.com/x/y/issues/1',
                  'accepted', 'not_published', ?, '{}'
                )
                """,
                (now,),
            )
            conn.execute(
                """
                INSERT INTO notifications(
                  notification_id, task_id, notification_type, channel, status,
                  created_at, metadata_json
                )
                VALUES (
                  'notification-failed', 'task-failed', 'worker_result_rejected',
                  'local', 'recorded', ?, ?
                )
                """,
                (now, json.dumps({"audit": audit, "result_id": "result-failed"}, sort_keys=True)),
            )
            conn.execute(
                """
                INSERT INTO notifications(
                  notification_id, task_id, notification_type, channel, status,
                  created_at, metadata_json
                )
                VALUES (
                  'notification-timeout', 'task-timeout', 'worker_timeout',
                  'local', 'recorded', ?, '{"attempt_id": "attempt-timeout"}'
                )
                """,
                (now,),
            )

    def _init_operator_overview_db(self, db_path):
        from robert_agent import storage

        storage.init_database(db_path)
        now = datetime.now(timezone.utc).isoformat()
        audit = {
            "ok": False,
            "status": "failed",
            "safe_error": "open_pr action missing fields: ['base']",
        }
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root)
                VALUES ('repo-1', 'example/backend', 'robert-bot', 'master', '/repo', '/repo/.worktrees')
                """
            )
            conn.executemany(
                """
                INSERT INTO workstreams(
                  workstream_id, repo_id, lifecycle, active_task_id, created_at, updated_at
                )
                VALUES (?, 'repo-1', ?, ?, ?, ?)
                """,
                [
                    ("ws-worker", "active", "task-worker", now, now),
                    ("ws-stale", "active", "task-stale", now, now),
                    ("ws-timeout", "failed", None, now, now),
                    ("ws-publish", "active", "task-publish", now, now),
                    ("ws-user", "waiting_for_user", "task-user", now, now),
                    ("ws-failed", "failed", None, now, now),
                ],
            )
            conn.executemany(
                """
                INSERT INTO tasks(
                  task_id, workstream_id, lifecycle, priority, created_at, updated_at
                )
                VALUES (?, ?, ?, 'P1', ?, ?)
                """,
                [
                    ("task-worker", "ws-worker", "running", now, now),
                    ("task-stale", "ws-stale", "running", now, now),
                    ("task-timeout", "ws-timeout", "failed", now, now),
                    ("task-publish", "ws-publish", "running", now, now),
                    ("task-user", "ws-user", "waiting_for_user", now, now),
                    ("task-failed", "ws-failed", "failed", now, now),
                ],
            )
            conn.executemany(
                """
                INSERT INTO attempts(
                  attempt_id, task_id, attempt_no, status, started_at, heartbeat_at, finished_at
                )
                VALUES (?, ?, 1, ?, ?, ?, ?)
                """,
                [
                    ("attempt-worker", "task-worker", "running", now, now, None),
                    ("attempt-stale", "task-stale", "stale", now, now, None),
                    ("attempt-timeout", "task-timeout", "failed", now, now, now),
                ],
            )
            conn.execute(
                """
                INSERT INTO worker_results(
                  result_id, task_id, attempt_id, output_type,
                  consumed_event_fingerprints_json, verification_json, handoff,
                  created_at, metadata_json
                )
                VALUES (
                  'result-publish', 'task-publish', 'attempt-publish',
                  'comment_analysis', '["comment:1"]', '[]', 'ready', ?, '{}'
                )
                """,
                (now,),
            )
            conn.execute(
                """
                INSERT INTO github_actions(
                  action_id, result_id, task_id, action_type, target_url,
                  audit_status, publish_status, created_at, metadata_json
                )
                VALUES (
                  'action-publish', 'result-publish', 'task-publish',
                  'comment', 'https://github.com/x/y/issues/1',
                  'accepted', 'not_published', ?, '{}'
                )
                """,
                (now,),
            )
            conn.executemany(
                """
                INSERT INTO notifications(
                  notification_id, task_id, notification_type, channel, status,
                  created_at, metadata_json
                )
                VALUES (?, ?, ?, 'local', 'recorded', ?, ?)
                """,
                [
                    (
                        "notification-timeout",
                        "task-timeout",
                        "worker_timeout",
                        now,
                        '{"attempt_id": "attempt-timeout"}',
                    ),
                    (
                        "notification-failed",
                        "task-failed",
                        "worker_result_rejected",
                        now,
                        json.dumps({"audit": audit, "result_id": "result-failed"}, sort_keys=True),
                    ),
                ],
            )

    def _record_repeated_worker_resume_notifications(self, db_path):
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.executemany(
                """
                INSERT INTO notifications(
                  notification_id, task_id, notification_type, channel, status,
                  created_at, metadata_json
                )
                VALUES (?, 'task-worker', 'worker_resume_prepared', 'local', 'recorded', ?, ?)
                """,
                [
                    (
                        "notification-resume-1",
                        now,
                        json.dumps(
                            {
                                "attempt_id": "attempt-worker-old-1",
                                "resume_attempt_id": "attempt-worker",
                                "recovery_artifact_path": "/tmp/recovery-1.json",
                            },
                            sort_keys=True,
                        ),
                    ),
                    (
                        "notification-resume-2",
                        now,
                        json.dumps(
                            {
                                "attempt_id": "attempt-worker-old-2",
                                "resume_attempt_id": "attempt-worker",
                                "recovery_artifact_path": "/tmp/recovery-2.json",
                            },
                            sort_keys=True,
                        ),
                    ),
                ],
            )

    def _record_result_pending_audit_task(self, db_path):
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO workstreams(
                  workstream_id, repo_id, lifecycle, active_task_id, created_at, updated_at
                )
                VALUES ('ws-result-pending', 'repo-1', 'active', 'task-result-pending', ?, ?)
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO tasks(
                  task_id, workstream_id, lifecycle, priority, created_at, updated_at
                )
                VALUES ('task-result-pending', 'ws-result-pending', 'running', 'P1', ?, ?)
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO attempts(
                  attempt_id, task_id, attempt_no, status, started_at, heartbeat_at, finished_at
                )
                VALUES ('attempt-result-pending', 'task-result-pending', 1, 'completed', ?, ?, ?)
                """,
                (now, now, now),
            )
            conn.execute(
                """
                INSERT INTO worker_results(
                  result_id, task_id, attempt_id, output_type,
                  consumed_event_fingerprints_json, verification_json, handoff,
                  created_at, metadata_json
                )
                VALUES (
                  'result-pending-audit', 'task-result-pending', 'attempt-result-pending',
                  'classification_result', '["comment:1"]', '[]', 'ready', ?, '{}'
                )
                """,
                (now,),
            )

    def _record_rejected_result(self, db_path):
        audit = {
            "ok": False,
            "status": "failed",
            "safe_error": "open_pr action missing fields: ['base']",
        }
        metadata = {
            "audit": audit,
            "recorded_by": "robert.worker",
            "used_skills": [],
        }
        notification_metadata = {
            "task_id": "task-1",
            "result_id": "result-1",
            "audit": audit,
        }
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                "UPDATE worker_results SET metadata_json = ? WHERE result_id = 'result-1'",
                (json.dumps(metadata, sort_keys=True),),
            )
            conn.execute(
                """
                UPDATE github_actions
                SET audit_status = 'failed',
                    publish_status = 'not_published'
                WHERE action_id = 'action-1'
                """,
            )
            conn.execute(
                """
                INSERT INTO notifications(
                  notification_id, task_id, notification_type, channel, status,
                  created_at, metadata_json
                )
                VALUES (
                  'notification-rejected-1', 'task-1', 'worker_result_rejected',
                  'local', 'recorded', ?, ?
                )
                """,
                (now, json.dumps(notification_metadata, sort_keys=True)),
            )

    def _record_ignored_untrusted_event(self, db_path):
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO github_events(
                  event_id, repo_id, source_id, event_fingerprint, event_type,
                  actor_login, author_association, authorization_status,
                  event_at, payload_json
                )
                VALUES (
                  'event-ignored-1', 'repo-1', 'source-1', 'comment:ignored-1',
                  'comment', 'external-user', NULL, 'ignored_untrusted_trigger',
                  ?, ?
                )
                """,
                (now, json.dumps({"intent": "bug_fix"}, sort_keys=True)),
            )

    def _record_authorized_trigger_unattached_event(self, db_path):
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO github_events(
                  event_id, repo_id, source_id, event_fingerprint, event_type,
                  actor_login, author_association, authorization_status,
                  event_at, payload_json
                )
                VALUES (
                  'event-authorized-trigger', 'repo-1', 'source-1',
                  'comment:authorized-trigger', 'comment', 'wklken', 'OWNER',
                  'authorized_trigger', ?, ?
                )
                """,
                (now, json.dumps({"intent": "bug_fix"}, sort_keys=True)),
            )

    def _record_pending_authorization_event(self, db_path):
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO github_events(
                  event_id, repo_id, source_id, event_fingerprint, event_type,
                  actor_login, author_association, authorization_status,
                  event_at, payload_json
                )
                VALUES (
                  'event-pending-authorization', 'repo-1', 'source-1',
                  'comment:pending-authorization', 'comment', 'external-user',
                  NULL, 'pending_authorization', ?, ?
                )
                """,
                (
                    now,
                    json.dumps(
                        {"authorization_lookup_complete": False, "intent": "bug_fix"},
                        sort_keys=True,
                    ),
                ),
            )

    def _record_publish_failure(self, db_path):
        metadata = {
            "body": self._dd_comment_body("ready"),
            "publish": {
                "status": "publish_failed",
                "failed_at": "2026-06-17T04:00:00+00:00",
                "safe_error": "gh: authentication failed",
                "command": ["gh", "api", "repos/x/y/issues/1/comments", "-f", "body=ready"],
            },
        }
        notification_metadata = {
            "action_id": "action-1",
            "action_type": "comment",
            "safe_error": "gh: authentication failed",
            "command": ["gh", "api", "repos/x/y/issues/1/comments", "-f", "body=ready"],
        }
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE github_actions
                SET audit_status = 'accepted',
                    publish_status = 'not_published',
                    metadata_json = ?
                WHERE action_id = 'action-1'
                """,
                (json.dumps(metadata, sort_keys=True),),
            )
            conn.execute(
                """
                INSERT INTO notifications(
                  notification_id, task_id, notification_type, channel, status,
                  created_at, metadata_json
                )
                VALUES (
                  'notification-publish-failed-1', 'task-1', 'github_publish_failed',
                  'local', 'recorded', ?, ?
                )
                """,
                (now, json.dumps(notification_metadata, sort_keys=True)),
            )

    def _record_unrelated_latest_notification(self, db_path):
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO notifications(
                  notification_id, task_id, notification_type, channel, status,
                  created_at, metadata_json
                )
                VALUES (
                  'notification-unrelated-latest', 'task-1', 'worker_stale',
                  'local', 'recorded', '2099-01-01T00:00:00+00:00',
                  '{"attempt_id": "attempt-1"}'
                )
                """
            )

    def _record_skipped_publish_action(self, db_path):
        metadata = {
            "publish": {
                "status": "skipped",
                "skipped_at": "2026-06-17T04:00:00+00:00",
                "safe_error": "publisher does not support action_type close_issue",
            },
        }
        notification_metadata = {
            "action_id": "action-1",
            "action_type": "close_issue",
            "safe_error": "publisher does not support action_type close_issue",
            "command": [],
        }
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE github_actions
                SET action_type = 'close_issue',
                    audit_status = 'accepted',
                    publish_status = 'skipped',
                    metadata_json = ?
                WHERE action_id = 'action-1'
                """,
                (json.dumps(metadata, sort_keys=True),),
            )
            conn.execute(
                """
                INSERT INTO notifications(
                  notification_id, task_id, notification_type, channel, status,
                  created_at, metadata_json
                )
                VALUES (
                  'notification-publish-skipped-1', 'task-1', 'github_publish_skipped',
                  'local', 'recorded', ?, ?
                )
                """,
                (now, json.dumps(notification_metadata, sort_keys=True)),
            )

    def _mark_summary_task_waiting_for_user(self, db_path):
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE tasks
                SET lifecycle = 'waiting_for_user',
                    updated_at = ?
                WHERE task_id = 'task-1'
                """,
                (now,),
            )
            conn.execute(
                """
                UPDATE workstreams
                SET lifecycle = 'waiting_for_user',
                    active_task_id = 'task-1',
                    updated_at = ?
                WHERE workstream_id = 'ws-1'
                """,
                (now,),
            )

    def _record_additional_skipped_publish_action(self, db_path):
        metadata = {
            "publish": {
                "status": "skipped",
                "skipped_at": "2026-06-17T04:00:00+00:00",
                "safe_error": "publisher does not support action_type close_issue",
            },
        }
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO github_actions(
                  action_id, result_id, task_id, action_type, target_url,
                  audit_status, publish_status, created_at, metadata_json
                )
                VALUES (
                  'action-skipped-1', 'result-1', 'task-1', 'close_issue',
                  'https://github.com/x/y/issues/1', 'accepted',
                  'skipped', ?, ?
                )
                """,
                (now, json.dumps(metadata, sort_keys=True)),
            )

    def _record_additional_publish_failure_action(self, db_path):
        metadata = {
            "publish": {
                "status": "publish_failed",
                "failed_at": "2026-06-17T04:00:00+00:00",
                "safe_error": "gh: authentication failed",
                "command": ["gh", "api", "repos/x/y/issues/1/comments", "-f", "body=ready"],
            },
        }
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO github_actions(
                  action_id, result_id, task_id, action_type, target_url,
                  audit_status, publish_status, created_at, metadata_json
                )
                VALUES (
                  'action-publish-failed-1', 'result-1', 'task-1', 'comment',
                  'https://github.com/x/y/issues/1', 'accepted',
                  'not_published', ?, ?
                )
                """,
                (now, json.dumps(metadata, sort_keys=True)),
            )

    def _record_recovered_publish_failure(self, db_path):
        metadata = {
            "body": self._dd_comment_body("ready"),
            "publish": {
                "status": "published",
                "published_at": "2026-06-17T04:10:00+00:00",
                "response": {"id": 987},
                "deduplicated": False,
            },
        }
        notification_metadata = {
            "action_id": "action-1",
            "action_type": "comment",
            "safe_error": "gh: authentication failed",
            "command": ["gh", "api", "repos/x/y/issues/1/comments", "-f", "body=ready"],
        }
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE github_actions
                SET audit_status = 'accepted',
                    publish_status = 'published',
                    metadata_json = ?
                WHERE action_id = 'action-1'
                """,
                (json.dumps(metadata, sort_keys=True),),
            )
            conn.execute(
                """
                INSERT INTO notifications(
                  notification_id, task_id, notification_type, channel, status,
                  created_at, metadata_json
                )
                VALUES (
                  'notification-publish-recovered-1', 'task-1', 'github_publish_failed',
                  'local', 'recorded', ?, ?
                )
                """,
                (now, json.dumps(notification_metadata, sort_keys=True)),
            )

    def _record_worker_result_evidence(self, db_path):
        metadata = {
            "audit": {
                "ok": True,
                "status": "accepted",
                "planned_github_actions": ["comment"],
            },
            "used_skills": ["fast-code-path"],
        }
        verification = [{"command": "python -m unittest", "status": "passed"}]
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE worker_results
                SET metadata_json = ?,
                    verification_json = ?
                WHERE result_id = 'result-1'
                """,
                (
                    json.dumps(metadata, sort_keys=True),
                    json.dumps(verification, sort_keys=True),
                ),
            )

    def _write_acceptance_config(self):
        repo_root = self.root / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        worktree_root = repo_root / ".worktrees"
        worktree_root.mkdir()
        config_path = self.root / "config.yml"
        config_path.write_text(
            f"""data_dir: {self.root / "data"}
database: dd.sqlite3
repos:
  - full_name: x/y
    github_account: robot
    trusted_actors:
      - wklken
    default_base_branch: master
    repo_root: {repo_root}
    worktree_root: {worktree_root}
""",
            encoding="utf-8",
        )
        return config_path

    def _dd_comment_body(self, text):
        return "<!-- robert-comment task_id=task-1 attempt_id=attempt-1 event_fingerprints=comment:1 -->\n" + text

    def _dd_pr_body(self, text, task_id="task-1"):
        return (
            "<!-- robert-workstream\n"
            "origin_workstream_id: github:x/y#1\n"
            "source_issue: 1\n"
            f"task_id: {task_id}\n"
            "created_by: robert\n"
            "-->\n"
            + text
        )


if __name__ == "__main__":
    unittest.main()
