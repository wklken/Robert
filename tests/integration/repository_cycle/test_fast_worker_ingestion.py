from robert_agent.adapters.processes import attempt as attempt_process
from robert_agent.delivery.application import publish
from contextlib import closing
import json
import sqlite3
import time
import yaml
from pathlib import Path
from tests.support import PACKAGE_ROOT
from robert_agent.adapters.processes import worker as cycle_worker
from robert_agent.execution.application import monitor
from robert_agent.work.application import preparation
from robert_agent.foundation import values
from tests.support.repository_cycle import (
    RepositoryCycleFixture,
    _dd_comment_body,
    _verification_evidence,
)


class FastWorkerIngestionTests(RepositoryCycleFixture):
    def test_live_dispatch_commits_attempt_before_worker_can_record_progress(self):
        from robert_agent.runtime import cycle as run_once

        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "comment-analysis",
                            "number": 123,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "body": "@robert-bot please analyze this",
                            "intent": "analysis",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        db_path = self.data_dir / "dd.sqlite3"
        visibility = []
        original_dispatch = cycle_worker.dispatch_worker

        def fake_dispatch_worker(**kwargs):
            with closing(sqlite3.connect(db_path)) as external:
                row = external.execute(
                    "SELECT status FROM attempts WHERE attempt_id = ?",
                    (kwargs["attempt_id"],),
                ).fetchone()
            visibility.append(row[0] if row else None)
            return {
                "ok": True,
                "status": "running",
                "task_id": kwargs["task_id"],
                "attempt_id": kwargs["attempt_id"],
                "pid": 12345,
                "command": ["cbc", "-p"],
            }

        try:
            cycle_worker.dispatch_worker = fake_dispatch_worker
            result = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                fixture_path=self.fixture_path,
                dry_run=False,
                skip_external=True,
            )
        finally:
            cycle_worker.dispatch_worker = original_dispatch

        self.assertTrue(result["ok"], result)
        self.assertEqual(visibility, ["prepared"])

    def test_live_dispatch_does_not_overwrite_fast_worker_result(self):
        from robert_agent.delivery.application import submission as worker_result
        from robert_agent.runtime import cycle as run_once

        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "comment-analysis",
                            "number": 123,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "body": "@robert-bot please analyze this",
                            "intent": "analysis",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        db_path = self.data_dir / "dd.sqlite3"
        original_dispatch = cycle_worker.dispatch_worker

        def fake_dispatch_worker(**kwargs):
            record = worker_result.record_result(
                db_path,
                {
                    "task_id": kwargs["task_id"],
                    "attempt_id": kwargs["attempt_id"],
                    "output_type": "comment_analysis",
                    "submission": {
                        "planned_github_actions": [
                            {
                                "type": "comment",
                                "target_url": "https://github.com/example/backend/issues/123",
                                "body": _dd_comment_body(
                                    "Analysis is ready",
                                    task_id=kwargs["task_id"],
                                    attempt_id=kwargs["attempt_id"],
                                    fingerprints="comment:comment-analysis",
                                ),
                            }
                        ],
                        "consumed_event_fingerprints": ["comment:comment-analysis"],
                        "verification": [_verification_evidence("python -B -m unittest")],
                        "handoff": "ready",
                        "used_skills": ["fast-code-path"],
                    },
                },
            )
            self.assertTrue(record["ok"], record)
            return {
                "ok": True,
                "status": "running",
                "task_id": kwargs["task_id"],
                "attempt_id": kwargs["attempt_id"],
                "pid": 12345,
                "command": ["cbc", "-p"],
            }

        try:
            cycle_worker.dispatch_worker = fake_dispatch_worker
            first = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                fixture_path=self.fixture_path,
                dry_run=False,
                skip_external=True,
            )
        finally:
            cycle_worker.dispatch_worker = original_dispatch

        self.assertTrue(first["ok"], first)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_id = conn.execute("SELECT task_id FROM tasks").fetchone()[0]
            attempt_status = conn.execute("SELECT status FROM attempts").fetchone()[0]
            result_count = conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0]
        self.assertEqual(attempt_status, "completed")
        self.assertEqual(result_count, 1)

        original_publish = publish.publish_ready_actions

        def fake_publish_ready_actions(db_path, dry_run=False, repo_id=None):
            with closing(sqlite3.connect(db_path)) as conn, conn:
                conn.execute(
                    """
                    UPDATE github_actions
                    SET publish_status = 'published',
                        external_id = '987',
                        target_url = 'https://github.com/example/backend/issues/123#issuecomment-987'
                    WHERE task_id = ?
                    """,
                    (task_id,),
                )
            return {
                "ok": True,
                "status": "published",
                "published_count": 1,
                "skipped_count": 0,
                "failed_count": 0,
            }

        original_process = attempt_process.dispatch_status
        try:
            # The stub worker has exited; only the controller can confirm that fact.
            attempt_process.dispatch_status = lambda _metadata: {"status": "dead"}
            publish.publish_ready_actions = fake_publish_ready_actions
            second = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=False,
                skip_external=True,
            )
        finally:
            attempt_process.dispatch_status = original_process
            publish.publish_ready_actions = original_publish

        self.assertTrue(second["ok"], second)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_lifecycle = conn.execute(
                "SELECT lifecycle FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            workstream = conn.execute("SELECT lifecycle, active_task_id FROM workstreams").fetchone()
            action_status = conn.execute(
                "SELECT audit_status, publish_status FROM github_actions WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            relationship = conn.execute(
                "SELECT relationship FROM task_events WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
        self.assertEqual(task_lifecycle, "completed")
        self.assertEqual(workstream, ("completed", None))
        self.assertEqual(action_status, ("accepted", "published"))
        self.assertEqual(relationship, "consumed")

    def test_live_dispatch_accepts_fast_successful_worker_subprocess_result(self):
        import os
        from robert_agent.runtime import cycle as run_once

        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "comment-analysis",
                            "number": 123,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "body": "@robert-bot please analyze this",
                            "intent": "analysis",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        worker_command = self.root / "mock-worker.py"
        worker_command.write_text(
            f"""#!/usr/bin/env python3
import json
import sys

sys.path.insert(0, {str(PACKAGE_ROOT.parent)!r})
from robert_agent.delivery.application import submission as result

prompt = sys.stdin.read()
fields = {{}}
for line in prompt.splitlines():
    if ": " in line:
        key, value = line.split(": ", 1)
        fields[key] = value
fingerprint_line = fields["event_fingerprints"].strip()
fingerprints = json.loads(fingerprint_line)
payload = {{
    "task_id": fields["task_id"],
    "attempt_id": fields["attempt_id"],
    "output_type": "comment_analysis",
    "submission": {{
    "planned_github_actions": [
        {{
            "type": "comment",
            "target_url": "https://github.com/example/backend/issues/123",
            "body": "Analysis is ready",
        }}
    ],
    "consumed_event_fingerprints": fingerprints,
    "verification": [{_verification_evidence("mock-worker")}],
    "handoff": "ready",
    "used_skills": [],
    }},
}}
record = result.record_result(fields["db_path"], payload)
print(json.dumps(record, sort_keys=True))
raise SystemExit(0 if record["ok"] else 1)
""",
            encoding="utf-8",
        )
        os.chmod(worker_command, 0o755)
        config = yaml.safe_load(self.config_path.read_text())
        config["workers"]["default"].update(adapter="cbc", command=str(worker_command))
        self.config_path.write_text(yaml.safe_dump(config))

        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=False,
            skip_external=True,
        )

        self.assertTrue(first["ok"], first)
        db_path = self.data_dir / "dd.sqlite3"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            with closing(sqlite3.connect(db_path)) as conn, conn:
                attempt_status = conn.execute("SELECT status FROM attempts").fetchone()[0]
                result_count = conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0]
                if result_count:
                    monitor.supervise_running_attempts(
                        conn,
                        "repo:example/backend",
                        {},
                        values.utc_now(),
                        resume_attempt=preparation.create_resume_attempt_and_prompt,
                    )
                    attempt_status = conn.execute("SELECT status FROM attempts").fetchone()[0]
            if attempt_status == "completed" and result_count == 1:
                break
            time.sleep(0.1)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_id = conn.execute("SELECT task_id FROM tasks").fetchone()[0]
            task_lifecycle = conn.execute("SELECT lifecycle FROM tasks").fetchone()[0]
            attempt_status, metadata_json = conn.execute("SELECT status, metadata_json FROM attempts").fetchone()
            result_count = conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0]
            notification_count = conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
        metadata = json.loads(metadata_json or "{}")
        dispatch_metadata = metadata.get("dispatch") or {}
        worker_logs = {}
        for key in ["stdout_path", "stderr_path"]:
            path = dispatch_metadata.get(key)
            if path:
                worker_logs[key] = Path(path).read_text(encoding="utf-8", errors="replace")
        self.assertEqual(task_lifecycle, "queued")
        self.assertEqual(
            attempt_status,
            "completed",
            {
                "result_count": result_count,
                "dispatch": dispatch_metadata,
                "worker_logs": worker_logs,
            },
        )
        self.assertEqual(result_count, 1, {"dispatch": dispatch_metadata, "worker_logs": worker_logs})
        self.assertEqual(notification_count, 0)

        original_publish = publish.publish_ready_actions

        def fake_publish_ready_actions(db_path, dry_run=False, repo_id=None):
            with closing(sqlite3.connect(db_path)) as conn, conn:
                conn.execute(
                    """
                    UPDATE github_actions
                    SET publish_status = 'published',
                        external_id = '987',
                        target_url = 'https://github.com/example/backend/issues/123#issuecomment-987'
                    WHERE task_id = ?
                    """,
                    (task_id,),
                )
            return {
                "ok": True,
                "status": "published",
                "published_count": 1,
                "skipped_count": 0,
                "failed_count": 0,
            }

        try:
            publish.publish_ready_actions = fake_publish_ready_actions
            second = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=False,
                skip_external=True,
            )
        finally:
            publish.publish_ready_actions = original_publish

        self.assertTrue(second["ok"], second)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_lifecycle = conn.execute("SELECT lifecycle FROM tasks").fetchone()[0]
            workstream = conn.execute("SELECT lifecycle, active_task_id FROM workstreams").fetchone()
        self.assertEqual(task_lifecycle, "completed")
        self.assertEqual(workstream, ("completed", None))
