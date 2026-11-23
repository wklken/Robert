"""Compact no-action detail while retaining summaries and recovery evidence."""

import json
import sqlite3
from unittest.mock import patch

from tests.support.retention import RetentionFixture, NOW
from robert_agent.execution import journal
from robert_agent.operations import retention
from robert_agent.readmodels import dashboard, status


RECENT = "2026-10-22T00:00:00+00:00"
COUNTERS = (
    "supervised_count",
    "failed_timeout_recovered_count",
    "raw_event_count",
    "decision_count",
    "prompt_count",
    "dispatch_queue_count",
    "audited_result_count",
    "publish_pending_count",
    "published_count",
    "publish_skipped_count",
    "publish_failed_count",
    "finalized_task_count",
    "reconciled_source_count",
    "current_state_closed_skip_count",
)


def no_action_summary():
    repo = {key: 0 for key in COUNTERS}
    repo.update(repo_id="repo", full_name="example/repo", status="completed", failure_status=None)
    # Reading already-known or non-driving input does not make a new task.
    repo.update(raw_event_count=1, decision_count=1)
    return {
        "overall_status": "completed",
        "dispatch_count": 0,
        "prompt_count": 0,
        "audited_result_count": 0,
        "supervised_count": 0,
        "reconciled_source_count": 0,
        "current_state_closed_skip_count": 0,
        "repo_summaries": [repo],
        "publish_result": {
            "ok": True,
            "pending_count": 0,
            "published_count": 0,
            "skipped_count": 0,
            "failed_count": 0,
            "finalized_task_count": 0,
        },
    }


class DiagnosticCompactionTests(RetentionFixture):
    def seed_history(self):
        with self.connect() as conn:
            for i in range(200):
                conn.execute(
                    "INSERT INTO agent_runs(run_id,status,started_at,finished_at) VALUES (?,'completed',?,?)",
                    (f"recent-{i:03}", NOW, NOW),
                )

    def detail_run(self, run, *, summary=None, state="completed", route_at=RECENT):
        self.add_run(run, state, RECENT)
        summary = no_action_summary() if summary is None else summary
        with self.connect() as conn:
            conn.execute("UPDATE agent_runs SET summary_json=? WHERE run_id=?", (json.dumps(summary), run))
            conn.execute("UPDATE run_repo_steps SET finished_at=? WHERE run_id=?", (route_at, run))
        self.add_lease("lease-" + run, owner=run, when=RECENT)
        return summary

    def test_no_action_details_compact_but_summary_and_business_rows_remain(self):
        self.seed_history()
        original = self.detail_run("empty")
        self.detail_run("route-owner", route_at=NOW)
        before = self.core_rows()
        preview = retention.prune_database(self.db, now=NOW)
        self.assertGreater(preview["counts"]["run_repo_steps"], 0, preview)
        with self.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM run_repo_steps WHERE run_id='empty'").fetchone()[0], 1)
        result = retention.prune_database(self.db, now=NOW, apply=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.core_rows(), before)
        with self.connect() as conn:
            run = status.build_run(conn, "empty")
            self.assertEqual(
                json.loads(conn.execute("SELECT summary_json FROM agent_runs WHERE run_id='empty'").fetchone()[0])[
                    "repo_summaries"
                ],
                original["repo_summaries"],
            )
            self.assertEqual(run["diagnostics"]["detail_status"], "compacted_no_actions")
            self.assertIn("明细已压缩", run["diagnostics"]["summary"])
            self.assertEqual(run["steps"], [])
            self.assertEqual(run["repo_steps"], [])
            self.assertEqual(conn.execute("SELECT count(*) FROM leases WHERE owner_id='empty'").fetchone()[0], 0)
            self.assertEqual(
                conn.execute("SELECT count(*) FROM run_repo_steps WHERE run_id='route-owner'").fetchone()[0], 1
            )
        again = retention.prune_database(self.db, now=NOW, apply=True)
        self.assertEqual(sum(again["counts"].values()), 0)

    def test_active_referenced_failed_nonzero_and_unknown_details_remain(self):
        self.seed_history()
        for run in ("referenced", "leased", "failure", "error-step", "pending-step", "business", "missing", "invalid"):
            summary = no_action_summary()
            if run == "business":
                summary["repo_summaries"][0]["prompt_count"] = 1
            elif run == "missing":
                del summary["repo_summaries"][0]["publish_pending_count"]
            self.detail_run(run, summary=summary, state="failed" if run == "failure" else "completed")
        with self.connect() as conn:
            conn.execute("UPDATE leases SET status='active' WHERE owner_id='leased'")
            conn.execute("UPDATE run_repo_steps SET error_json='{}' WHERE run_id='error-step'")
            conn.execute("UPDATE run_repo_steps SET status='pending' WHERE run_id='pending-step'")
            conn.execute("UPDATE agent_runs SET summary_json='invalid' WHERE run_id='invalid'")
            conn.execute(
                "INSERT INTO wakeups(wakeup_id,repo_id,reason,dedupe_key,source_run_id,status,not_before_at,created_at,updated_at) "
                "VALUES ('reference','repo','manual_operator_request','reference','referenced','consumed',?,?,?)",
                (RECENT, RECENT, RECENT),
            )
            before = conn.execute("SELECT * FROM run_repo_steps ORDER BY step_id").fetchall()
        result = retention.prune_database(self.db, now=NOW, apply=True)
        self.assertTrue(result["ok"], result)
        with self.connect() as conn:
            self.assertEqual(conn.execute("SELECT * FROM run_repo_steps ORDER BY step_id").fetchall(), before)

    def test_failed_compaction_rolls_back_details_and_marker(self):
        self.seed_history()
        self.detail_run("empty")
        self.detail_run("route-owner", route_at=NOW)
        from robert_agent.persistence.stores import diagnostics

        real = diagnostics.compact_run_details

        def fail(*args, **kwargs):
            real(*args, **kwargs)
            raise sqlite3.OperationalError("database is locked")

        with patch.object(diagnostics, "compact_run_details", side_effect=fail):
            result = retention.prune_database(self.db, now=NOW, apply=True)
        self.assertFalse(result["ok"], result)
        with self.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM run_repo_steps WHERE run_id='empty'").fetchone()[0], 1)
            summary = json.loads(conn.execute("SELECT summary_json FROM agent_runs WHERE run_id='empty'").fetchone()[0])
            self.assertNotIn("diagnostics", summary)

    def test_pipeline_output_is_referenced_only_after_summary_is_available(self):
        self.detail_run("output")
        summary = no_action_summary()
        with self.connect() as conn:
            journal.insert_run_step(conn, "output", "repo_pipelines")
            journal.mark_run_step(
                conn,
                "output",
                "repo_pipelines",
                "succeeded",
                NOW,
                output={"overall_status": "completed", "repo_summaries": summary["repo_summaries"]},
            )
            provisional = json.loads(
                conn.execute(
                    "SELECT output_json FROM run_steps WHERE run_id='output' AND step_key='repo_pipelines'"
                ).fetchone()[0]
            )
            self.assertEqual(provisional["repo_summaries"], summary["repo_summaries"])
            provisional["duration_seconds"] = 2.5
            conn.execute(
                "UPDATE run_steps SET output_json=? WHERE run_id='output' AND step_key='repo_pipelines'",
                (json.dumps(provisional),),
            )
            legacy = next(
                s["output"] for s in status.build_run(conn, "output")["steps"] if s["step_key"] == "repo_pipelines"
            )
            journal.finish_run(conn, "output", "completed", NOW, summary)
            raw = json.loads(
                conn.execute(
                    "SELECT output_json FROM run_steps WHERE run_id='output' AND step_key='repo_pipelines'"
                ).fetchone()[0]
            )
            self.assertNotIn("repo_summaries", raw)
            cli = status.build_run(conn, "output")
            output = next(s["output"] for s in cli["steps"] if s["step_key"] == "repo_pipelines")
            self.assertEqual(output, legacy)
            self.assertEqual(output["overall_status"], "completed")
            self.assertIn("duration_seconds", output)
        web = dashboard.build_dashboard_data(self.db)
        output = next(s["output"] for s in web["recent_runs"][0]["steps"] if s["step_key"] == "repo_pipelines")
        self.assertEqual(output["repo_summaries"], summary["repo_summaries"])

    def test_compaction_batch_is_bounded_and_repeated_calls_make_progress(self):
        self.seed_history()
        for i in range(5):
            self.detail_run(f"empty-{i}")
        self.detail_run("route-owner", route_at=NOW)
        first = retention.prune_database(self.db, now=NOW, apply=True, batch_size=2)
        self.assertEqual(first["counts"]["run_repo_steps"], 2)
        self.assertTrue(first["more_work"])
        second = retention.prune_database(self.db, now=NOW, apply=True, batch_size=2, max_batches=3)
        self.assertEqual(second["counts"]["run_repo_steps"], 3)
        with self.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM run_repo_steps").fetchone()[0], 1)

    def test_recent_detail_and_unknown_json_shapes_are_retained(self):
        self.seed_history()
        for run, changes in (("null-repo", None), ("string-repo", "unknown"), ("null-details", {})):
            summary = no_action_summary()
            if run == "null-details":
                summary["diagnostics"] = None
            else:
                summary["repo_summaries"].append(changes)
            self.detail_run(run, summary=summary)
        self.detail_run("recent-real")
        with self.connect() as conn:
            conn.execute("UPDATE agent_runs SET started_at=? WHERE run_id='recent-real'", (NOW,))
            before = conn.execute("SELECT * FROM run_repo_steps ORDER BY step_id").fetchall()
        result = retention.prune_database(self.db, now=NOW, apply=True)
        self.assertTrue(result["ok"], result)
        with self.connect() as conn:
            self.assertEqual(conn.execute("SELECT * FROM run_repo_steps ORDER BY step_id").fetchall(), before)

    def test_pipeline_reference_preserves_unmatched_provisional_summary(self):
        self.detail_run("mismatch")
        full = no_action_summary()
        with self.connect() as conn:
            journal.insert_run_step(conn, "mismatch", "repo_pipelines")
            journal.mark_run_step(
                conn,
                "mismatch",
                "repo_pipelines",
                "succeeded",
                NOW,
                output={"overall_status": "completed", "repo_summaries": full["repo_summaries"]},
            )
            canonical = no_action_summary()
            canonical["repo_summaries"][0]["raw_event_count"] = 2
            journal.finish_run(conn, "mismatch", "completed", NOW, canonical)
            output = json.loads(
                conn.execute(
                    "SELECT output_json FROM run_steps WHERE run_id='mismatch' AND step_key='repo_pipelines'"
                ).fetchone()[0]
            )
            self.assertEqual(output["repo_summaries"], full["repo_summaries"])
            self.assertNotIn("repo_summaries_ref", output)

    def test_preview_counts_each_row_once_when_retention_and_compaction_overlap(self):
        from tests.support.retention import OLD

        self.seed_history()
        self.detail_run("expired")
        self.detail_run("empty")
        self.detail_run("route-owner", route_at=NOW)
        with self.connect() as conn:
            conn.execute("UPDATE agent_runs SET started_at=?,finished_at=? WHERE run_id='expired'", (OLD, OLD))
            conn.execute("UPDATE leases SET released_at=? WHERE owner_id IN ('expired','empty')", (OLD,))
        preview = retention.prune_database(self.db, now=NOW, batch_size=100)
        applied = retention.prune_database(self.db, now=NOW, batch_size=100, apply=True)
        self.assertEqual(preview["counts"], applied["counts"])
        self.assertEqual(applied["counts"]["run_repo_steps"], 2)
        self.assertEqual(applied["counts"]["leases"], 2)
