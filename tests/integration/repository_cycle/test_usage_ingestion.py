from contextlib import closing
import json
import sqlite3
from tests.support import PACKAGE_ROOT
from tests.support.repository_cycle import (
    RepositoryCycleFixture,
    _verification_evidence,
)


class CycleUsageIngestionTests(RepositoryCycleFixture):
    def test_run_once_copies_worker_usage_from_stdout_log(self):
        from robert_agent.delivery.application import submission as result
        from robert_agent.runtime import cycle as run_once

        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(first["ok"], first)
        db_path = self.data_dir / "dd.sqlite3"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_id, attempt_id = conn.execute(
                "SELECT task_id, attempt_id FROM attempts"
            ).fetchone()
        stdout_path = self.root / "worker.stdout.log"
        stdout_path.write_text(
            "\n".join(
                [
                    json.dumps({"type": "message", "usage": {"input_tokens": 1}}),
                    json.dumps(
                        {
                            "type": "result",
                            "subtype": "success",
                            "duration_ms": 1234,
                            "num_turns": 1,
                            "total_cost_usd": 0.045,
                            "usage": {
                                "input_tokens": 20,
                                "output_tokens": 8,
                            },
                        },
                        sort_keys=True,
                    ),
                ]
            ),
            encoding="utf-8",
        )
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                "UPDATE attempts SET metadata_json = json_set(?, '$.result_contract', 'compact-v1') WHERE attempt_id = ?",
                (
                    json.dumps(
                        {"dispatch": {"stdout_path": str(stdout_path)}},
                        sort_keys=True,
                    ),
                    attempt_id,
                ),
            )
        record = result.record_result(
            db_path,
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "output_type": "new_pr",
                "submission": {
                    "planned_github_actions": self._new_pr_actions_for_attempt(
                        db_path,
                        task_id,
                    ),
                    "consumed_event_fingerprints": ["comment:comment-1"],
                    "verification": [_verification_evidence()],
                    "handoff": "opened PR",
                    "used_skills": ["fast-small-pr"],
                },
            },
        )
        self.assertTrue(record["ok"], record)

        audited = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(audited["ok"], audited)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            attempt_metadata = json.loads(
                conn.execute(
                    "SELECT metadata_json FROM attempts WHERE attempt_id = ?",
                    (attempt_id,),
                ).fetchone()[0]
            )
            result_metadata = json.loads(
                conn.execute(
                    "SELECT metadata_json FROM worker_results WHERE result_id = ?",
                    (record["result_id"],),
                ).fetchone()[0]
            )
        self.assertTrue(attempt_metadata["usage"]["usage_available"])
        self.assertTrue(result_metadata["usage"]["usage_available"])
        self.assertEqual(result_metadata["usage"]["usage"]["input_tokens"], 20)
        self.assertEqual(result_metadata["usage"]["total_cost_usd"], 0.045)
