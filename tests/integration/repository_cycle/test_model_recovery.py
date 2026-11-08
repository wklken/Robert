from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import sys
from unittest.mock import patch

import yaml

from tests.support.repository_cycle import RepositoryCycleFixture
from robert_agent.adapters.processes import attempt as attempt_process
from robert_agent.configuration.command import validate_config
from robert_agent.execution.application import monitor
from robert_agent.supervision import decisions
from robert_agent.work.application import preparation


class ModelWorkerRecoveryTests(RepositoryCycleFixture):
    def test_worker_retry_waits_for_model_and_preserves_the_domain_budget(self):
        db_path, task_id, attempt_id = self._interrupted_attempt(phase=False, transport=True)
        raw = yaml.safe_load(self.config_path.read_text())
        raw.setdefault("supervision", {}).update(mode="decide", decision={"command": [sys.executable]})
        self.config_path.write_text(yaml.safe_dump(raw))
        config = validate_config(self.config_path, skip_external=True)
        self.assertTrue(config["ok"], config)
        now = datetime.now(timezone.utc).isoformat()
        repo = config["repos"][0]

        def observe(conn):
            with patch.object(attempt_process, "dispatch_status", return_value={"status": "dead"}):
                monitor.supervise_running_attempts(
                    conn,
                    "repo:example/backend",
                    config,
                    now,
                    data_dir=self.data_dir,
                    db_path=db_path,
                    repo=repo,
                    resume_attempt=preparation.create_resume_attempt_and_prompt,
                )
                monitor.recover_failed_timeout_attempts(
                    conn,
                    "repo:example/backend",
                    now,
                    data_dir=self.data_dir,
                    db_path=db_path,
                    repo=repo,
                    resume_attempt=preparation.create_resume_attempt_and_prompt,
                    config_result=config,
                )

        with closing(sqlite3.connect(db_path)) as conn, conn:
            observe(conn)
            self.assertEqual(conn.execute("SELECT count(*) FROM attempts").fetchone()[0], 1)
            from robert_agent.persistence.queries import attempts

            self.assertEqual(list(attempts.find_for_running_attempt_count(conn, "repo:example/backend"))[0][0], 0)
            self.assertEqual(
                conn.execute("SELECT status FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()[0], "failed"
            )
            self.assertEqual(
                conn.execute(
                    "SELECT recovery_attempts FROM execution_subjects WHERE subject_id=?", (task_id,)
                ).fetchone()[0],
                0,
            )
            failure = json.loads(
                conn.execute("SELECT failure_json FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()[0]
            )
            self.assertEqual(failure["status"], "waiting_for_model_decision")
            workstream = conn.execute("SELECT workstream_id FROM tasks WHERE task_id=?", (task_id,)).fetchone()[0]
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            packet = decisions.packet(decisions.snapshot(conn, workstream, config, now), {})
            candidate = next(c for c in packet["candidates"] if c["action"] == "retry_worker")
            output = {
                "bundle_id": packet["bundle_id"],
                "bundle_sha256": packet["bundle_sha256"],
                "action": "retry_worker",
                "candidate_id": candidate["id"],
                "reason": "Transient transport interruption",
                "evidence_ids": ["attempts"],
                "expected_progress": "Resume the original request",
                "recheck_seconds": 300,
                "message": "",
            }
            self.assertTrue(
                decisions.approve(conn, config, packet, decisions.validate(json.dumps(output), packet), now)
            )
            conn.commit()
            now = (datetime.fromisoformat(now) + timedelta(seconds=1)).isoformat()
            observe(conn)
            self.assertEqual(conn.execute("SELECT count(*) FROM attempts").fetchone()[0], 2)
            self.assertEqual(
                conn.execute(
                    "SELECT recovery_attempts FROM execution_subjects WHERE subject_id=?", (task_id,)
                ).fetchone()[0],
                1,
            )
            self.assertEqual(decisions.read(conn, workstream)["status"], "verifying")
