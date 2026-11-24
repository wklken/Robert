from contextlib import closing
import sqlite3

from tests.support.summary_database import SummaryFixture


class TaskInspectionFixture(SummaryFixture):
    def setUp(self):
        super().setUp()
        self.db = self.root / "robert.sqlite3"
        self._init_summary_db(self.db)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE tasks SET created_at='2026-11-24T10:00:00Z', updated_at=created_at")
            conn.execute("UPDATE tasks SET created_at='2026-11-24T12:40:00Z' WHERE task_id='task-2'")
            conn.execute("UPDATE workstreams SET public_ref='wi-073'")
            conn.execute("UPDATE github_sources SET title='073 verifier failure'")
            conn.execute("UPDATE attempts SET started_at='2026-11-24T10:01:00Z', finished_at='2026-11-24T10:03:00Z'")
            conn.execute(
                "INSERT INTO attempts(attempt_id,task_id,attempt_no,status,started_at,heartbeat_at,metadata_json) "
                "VALUES ('attempt-2','task-2',1,'running','2026-11-24T12:45:00Z','2026-11-24T12:59:00Z','{}')"
            )
            conn.execute(
                "INSERT INTO worker_phases(phase_id,attempt_id,phase,status,summary,created_at) "
                "VALUES ('phase-2','attempt-2','verify','running','checking','2026-11-24T12:50:00Z')"
            )
            conn.execute(
                "INSERT INTO pre_pr_review_sessions(round_id,task_id,status,request_json,created_at,updated_at) "
                "VALUES ('round-2','task-2','reviewing','{}','2026-11-24T10:00:00Z','2026-11-24T12:55:00Z')"
            )
            conn.executemany(
                "INSERT INTO pre_pr_review_runs(run_id,round_id,stage,worker_id,attempt_no,owner_attempt_id,status,"
                "output_dir,prompt_sha256,started_at,created_at,updated_at,error) "
                "VALUES (?,'round-2','verify_final','reviewer',?,?,?,?,'hash',?,?,?,?)",
                [
                    (
                        "old-run",
                        1,
                        "attempt-1",
                        "failed",
                        "/old",
                        "2026-11-24T10:01:00Z",
                        "2026-11-24T10:01:00Z",
                        "2026-11-24T12:58:00Z",
                        "password=leaked-secret-value",
                    ),
                    (
                        "new-run",
                        2,
                        "attempt-2",
                        "running",
                        "/new",
                        "2026-11-24T12:55:00Z",
                        "2026-11-24T12:54:00Z",
                        "2026-11-24T12:59:00Z",
                        None,
                    ),
                ],
            )
