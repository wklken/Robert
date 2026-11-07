from contextlib import closing
import shlex
import sqlite3
import sys
import yaml
from tests.support import PACKAGE_ROOT
from tests.support.acceptance import AcceptanceFixture


class ReadinessTests(AcceptanceFixture):
    def test_acceptance_preflight_reports_live_gate_statuses(self):
        from robert_agent.acceptance import readiness as acceptance
        config_path = self._write_acceptance_config()

        def command_exists(command):
            return {
                sys.executable: sys.executable,
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
        from robert_agent.acceptance import readiness as acceptance
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
        from robert_agent.acceptance import readiness as acceptance
        config_path = self._write_acceptance_config()
        document = yaml.safe_load(config_path.read_text())
        document['workers']['default']['command'] = 'custom-cbc'
        config_path.write_text(yaml.safe_dump(document))
        probed = []

        def command_exists(command):
            probed.append(command)
            return {
                sys.executable: sys.executable,
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
        self.assertIn(sys.executable, probed)
        self.assertIn("custom-cbc", probed)
        self.assertEqual(gates["python_bin"]["status"], "passed")
        self.assertEqual(gates["worker_command"]["status"], "passed")
        self.assertEqual(gates["worker_command"]["evidence"], "/opt/bin/custom-cbc")

    def test_acceptance_preflight_checks_codex_worker_agent_command(self):
        from robert_agent.acceptance import readiness as acceptance
        config_path = self._write_acceptance_config()
        document = yaml.safe_load(config_path.read_text())
        document['workers']['default'].update(adapter='codex', command='/opt/homebrew/bin/codex')
        config_path.write_text(yaml.safe_dump(document))
        probed = []

        def command_exists(command):
            probed.append(command)
            return {
                sys.executable: sys.executable,
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
        from robert_agent.acceptance import readiness as acceptance
        config_path = self._write_acceptance_config()
        document = yaml.safe_load(config_path.read_text())
        document['workers']['default']['command'] = '/opt/bin/cbc'
        document['workers']['reviewer'] = {'adapter': 'codex', 'command': '/opt/bin/review-codex',
                                           'model': 'gpt-5.6-sol', 'effort': 'xhigh'}
        config_path.write_text(yaml.safe_dump(document))
        probed = []

        def command_exists(command):
            probed.append(command)
            return {
                sys.executable: sys.executable,
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
        from robert_agent.acceptance import readiness as acceptance
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
        from robert_agent.acceptance import readiness as acceptance
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
        from robert_agent.acceptance import readiness as acceptance
        from robert_agent.adapters.sqlite import database as storage

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
            f"{acceptance.AGENT_ROOT / 'readmodels/summarize.py'} --db {db_path}",
            result["next_actions"],
        )

    def test_acceptance_preflight_reports_worktree_root_create_command(self):
        from robert_agent.acceptance import readiness as acceptance
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
        from robert_agent.acceptance import readiness as acceptance
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
            f"Remove file or set repos[0].worktrees to a directory: {worktree_root}",
            result["next_actions"],
        )

    def test_acceptance_preflight_checks_all_configured_worktree_roots(self):
        from robert_agent.acceptance import readiness as acceptance
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
    default_branch: main
    checkout: {repo_b}
    worktrees: {worktree_b}
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
            f"Remove file or set repo org/repo-b worktrees to a directory: {worktree_b}",
            result["next_actions"],
        )

    def test_acceptance_preflight_reports_missing_config_without_traceback(self):
        from robert_agent.acceptance import readiness as acceptance
        result = acceptance.acceptance_preflight(self.root / "missing-config.yml")

        gates = {gate["id"]: gate for gate in result["gates"]}
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "failed_config")
        self.assertEqual(gates["config_valid"]["status"], "failed")
        expected_command = (
            f"{shlex.quote(sys.executable)} -B "
            f"{shlex.quote(str(acceptance.AGENT_ROOT / 'operations/initialize.py'))} "
            f"--config {shlex.quote(str((self.root / 'missing-config.yml').resolve()))}"
        )
        self.assertEqual(
            result["next_actions"],
            [expected_command],
        )

    def test_acceptance_preflight_reports_unconfigured_public_skeleton(self):
        from robert_agent.acceptance import readiness as acceptance
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
            "config.repos must contain at least 1 items",
            gates["config_valid"]["summary"],
        )
        self.assertEqual(
            result["next_actions"],
            ["Fix robert config"],
        )

    def test_acceptance_preflight_reports_data_dir_file(self):
        from robert_agent.acceptance import readiness as acceptance
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
        from robert_agent.acceptance import readiness as acceptance
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
        self.assertEqual(gates["config_valid"]["summary"], "invalid config: config.database must be string")
        self.assertEqual(
            result["next_actions"],
            [f"Set database to a SQLite filename in {config_path.resolve()}"],
        )

    def test_acceptance_preflight_reports_database_path(self):
        from robert_agent.acceptance import readiness as acceptance
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
            "invalid config: database must be a filename, not a path: nested/dd.sqlite3",
        )
        self.assertEqual(
            result["next_actions"],
            [f"Set database to a SQLite filename in {config_path.resolve()}"],
        )

    def test_acceptance_preflight_reports_nonpositive_max_concurrency(self):
        from robert_agent.acceptance import readiness as acceptance
        config_path = self._write_acceptance_config()
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "database: dd.sqlite3",
                "database: dd.sqlite3\nscheduler:\n  max_concurrency: 0",
            ),
            encoding="utf-8",
        )

        result = acceptance.acceptance_preflight(config_path)

        gates = {gate["id"]: gate for gate in result["gates"]}
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "failed_config")
        self.assertEqual(gates["config_valid"]["status"], "failed")
        self.assertEqual(gates["config_valid"]["summary"], "invalid config: config.scheduler.max_concurrency violates minimum 1")
        self.assertEqual(
            result["next_actions"],
            [f"Set scheduler.max_concurrency to a positive integer in {config_path.resolve()}"],
        )

    def test_acceptance_preflight_reports_nonpositive_runtime_threshold(self):
        from robert_agent.acceptance import readiness as acceptance
        config_path = self._write_acceptance_config()
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "database: dd.sqlite3",
                "database: dd.sqlite3\nsupervision:\n  heartbeat_stale_seconds: 0",
            ),
            encoding="utf-8",
        )

        result = acceptance.acceptance_preflight(config_path)

        gates = {gate["id"]: gate for gate in result["gates"]}
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "failed_config")
        self.assertEqual(gates["config_valid"]["status"], "failed")
        self.assertEqual(gates["config_valid"]["summary"], "invalid config: config.supervision.heartbeat_stale_seconds violates minimum 1")
        self.assertEqual(
            result["next_actions"],
            [f"Set supervision.heartbeat_stale_seconds to a positive integer in {config_path.resolve()}"],
        )

    def test_acceptance_preflight_reports_invalid_repo_root_path(self):
        from robert_agent.acceptance import readiness as acceptance
        config_path = self._write_acceptance_config()
        bad_repo_root = self.root / "missing-repo"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                f"checkout: {self.root / 'repo'}",
                f"checkout: {bad_repo_root}",
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
            [f"Set repos[0].checkout to a real local git checkout in {config_path.resolve()}"],
        )

    def test_acceptance_preflight_reports_invalid_second_repo_root_path(self):
        from robert_agent.acceptance import readiness as acceptance
        config_path = self._write_acceptance_config()
        bad_repo_root = self.root / "missing-repo-b"
        config_path.write_text(
            config_path.read_text(encoding="utf-8")
            + f"""
  - full_name: org/repo-b
    github_account: robot
    trusted_actors:
      - alice
    default_branch: main
    checkout: {bad_repo_root}
    worktrees: {bad_repo_root / ".worktrees"}
""",
            encoding="utf-8",
        )

        result = acceptance.acceptance_preflight(config_path)

        gates = {gate["id"]: gate for gate in result["gates"]}
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "failed_config")
        self.assertEqual(gates["config_valid"]["status"], "failed")
        self.assertIn("repos[1].checkout is not a git checkout", gates["config_valid"]["summary"])
        self.assertEqual(
            result["next_actions"],
            [f"Set repos[1].checkout to a real local git checkout in {config_path.resolve()}"],
        )
