"""Maintenance reuses identical diagnostics without changing history projections."""

import json
import hashlib
from unittest.mock import patch

from tests.support.retention import RetentionFixture, NOW
from robert_agent.operations import retention
from robert_agent.readmodels import dashboard, status
from robert_agent.persistence.queries import agent_runs
from robert_agent.execution import journal
from robert_agent.persistence.stores import diagnostics


RECENT = "2026-10-22T00:00:00+00:00"


class SummaryDeduplicationTests(RetentionFixture):
    def seed_compacted(self, run):
        summary = {
            "overall_status": "completed",
            "repo_summaries": [{"repo_id": "repo", "message": "保留所有原始信息", "details": "x" * 8000}],
            "diagnostics": {"detail_status": "compacted_no_actions"},
        }
        self.add_run(run, when=RECENT)
        with self.connect() as conn:
            conn.execute("DELETE FROM run_steps WHERE run_id=?", (run,))
            conn.execute("DELETE FROM run_repo_steps WHERE run_id=?", (run,))
            conn.execute("UPDATE agent_runs SET summary_json=? WHERE run_id=?", (json.dumps(summary), run))
        return summary

    def test_identical_summaries_share_storage_and_preserve_cli_web_and_query_output(self):
        summary = self.seed_compacted("first")
        self.seed_compacted("second")
        business = self.core_rows()
        with self.connect() as conn:
            before = {run: status.build_run(conn, run) for run in ("first", "second")}
        result = retention.prune_database(self.db, apply=True, now=NOW, max_batches=5, batch_size=1)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result.get("deduplicated_summaries"), 2)
        with self.connect() as conn:
            stored_bytes = conn.execute("SELECT sum(length(summary_json)) FROM agent_runs").fetchone()[0]
            self.assertLess(stored_bytes, 500)
            self.assertEqual(conn.execute("SELECT count(*) FROM diagnostic_run_summaries").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM agent_run_summary_refs").fetchone()[0], 2)
            for run in before:
                self.assertEqual(status.build_run(conn, run), before[run])
                self.assertEqual(json.loads(agent_runs.find_summary_json_by_run_id(conn, run)[0][0]), summary)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        web = dashboard.build_dashboard_data(self.db)
        self.assertEqual({r["run_id"] for r in web["recent_runs"]}, {"first", "second"})
        self.assertTrue(all(r["summary"] == summary for r in web["recent_runs"]))
        self.assertEqual(self.core_rows(), business)
        again = retention.prune_database(self.db, apply=True, now=NOW)
        self.assertEqual(again.get("deduplicated_summaries"), 0)

    def test_matching_historical_steps_reference_summary_and_keep_duration_and_error(self):
        summary = {"overall_status": "failed", "repo_summaries": [{"repo_id": "repo", "reason": "failed"}]}
        self.add_run("failed", "failed", RECENT)
        with self.connect() as conn:
            conn.execute(
                "UPDATE agent_runs SET summary_json=?,error_json='{}' WHERE run_id='failed'", (json.dumps(summary),)
            )
            conn.execute("UPDATE run_steps SET output_json=? WHERE run_id='failed'", (json.dumps(summary),))
            conn.execute(
                "INSERT INTO run_steps(step_id,run_id,step_key,status,finished_at,output_json,error_json) "
                "VALUES ('pipeline','failed','repo_pipelines','failed',?,?,?)",
                (RECENT, json.dumps({**summary, "duration_seconds": 2.5}), '{"reason":"keep"}'),
            )
            before = status.build_run(conn, "failed")
        result = retention.prune_database(self.db, apply=True, now=NOW)
        self.assertTrue(result["ok"], result)
        with self.connect() as conn:
            self.assertEqual(status.build_run(conn, "failed"), before)
            raw = dict(conn.execute("SELECT step_key,output_json FROM run_steps WHERE run_id='failed'"))
            self.assertEqual(json.loads(raw["summarize"]), {"summary_ref": "agent_runs.summary_json"})
            output = json.loads(raw["repo_pipelines"])
            self.assertNotIn("repo_summaries", output)
            self.assertEqual(output["duration_seconds"], 2.5)
            self.assertEqual(
                json.loads(conn.execute("SELECT summary_json FROM agent_runs WHERE run_id='failed'").fetchone()[0]),
                summary,
            )

    def test_preview_is_bounded_and_does_not_change_storage(self):
        self.seed_compacted("first")
        self.seed_compacted("second")
        with self.connect() as conn:
            before = conn.iterdump()
            before = list(before)
        result = retention.prune_database(self.db, now=NOW, batch_size=1, max_batches=5)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["deduplicated_summaries"], 1)
        self.assertTrue(result["more_work"])
        self.assertEqual(result["batches"], 1)
        with self.connect() as conn:
            self.assertEqual(list(conn.iterdump()), before)

    def test_shared_content_survives_one_owner_expiring_and_is_removed_after_last_owner(self):
        self.seed_compacted("first")
        self.seed_compacted("second")
        self.assertTrue(retention.prune_database(self.db, apply=True, now=NOW)["ok"])
        with self.connect() as conn:
            conn.executemany(
                "INSERT INTO agent_runs(run_id,status,started_at,finished_at) VALUES (?,'completed',?,?)",
                [(f"newer-{i}", NOW, NOW) for i in range(200)],
            )
            conn.execute("UPDATE agent_runs SET finished_at='2026-08-01T00:00:00Z' WHERE run_id='first'")
        result = retention.prune_database(self.db, apply=True, now=NOW)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["pruned_shared_summaries"], 0)
        with self.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM diagnostic_run_summaries").fetchone()[0], 1)
            conn.execute("UPDATE agent_runs SET finished_at='2026-08-01T00:00:00Z' WHERE run_id='second'")
        result = retention.prune_database(self.db, apply=True, now=NOW)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["pruned_shared_summaries"], 1)
        with self.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM agent_run_summary_refs").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM diagnostic_run_summaries").fetchone()[0], 0)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_replacing_summary_releases_old_reference(self):
        self.seed_compacted("first")
        self.assertTrue(retention.prune_database(self.db, apply=True, now=NOW)["ok"])
        replacement = {"overall_status": "failed", "reason": "replacement"}
        with self.connect() as conn:
            journal.finish_run(conn, "first", "failed", RECENT, replacement, error={"reason": "keep"})
            self.assertEqual(json.loads(agent_runs.find_summary_json_by_run_id(conn, "first")[0][0]), replacement)
            self.assertEqual(conn.execute("SELECT count(*) FROM agent_run_summary_refs").fetchone()[0], 0)
        result = retention.prune_database(self.db, apply=True, now=NOW)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["pruned_shared_summaries"], 1)

    def test_malformed_and_nonterminal_outputs_remain_unchanged(self):
        for run, summary in (("invalid", "invalid"), ("partial", '{"repo_summaries":[]}')):
            self.add_run(run, "failed", RECENT)
            with self.connect() as conn:
                conn.execute("UPDATE agent_runs SET summary_json=? WHERE run_id=?", (summary, run))
                conn.execute(
                    "UPDATE run_steps SET output_json=? WHERE run_id=?",
                    ("invalid" if run == "invalid" else summary, run),
                )
                conn.execute(
                    "INSERT INTO run_steps(step_id,run_id,step_key,status,output_json) VALUES (?,?,'repo_pipelines','running',?)",
                    ("pipeline-" + run, run, '{"repo_summaries":[]}'),
                )
        with self.connect() as conn:
            before = list(conn.execute("SELECT step_id,output_json FROM run_steps ORDER BY step_id"))
        result = retention.prune_database(self.db, apply=True, now=NOW)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["referenced_step_outputs"], 1)
        with self.connect() as conn:
            after = list(conn.execute("SELECT step_id,output_json FROM run_steps ORDER BY step_id"))
        self.assertEqual(
            [tuple(r) for r in after if r[0] != "s-partial"], [tuple(r) for r in before if r[0] != "s-partial"]
        )

    def test_identity_conflict_rolls_back_cleanup_and_reports_failure(self):
        summary = self.seed_compacted("first")
        canonical = json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        identity = hashlib.sha256(canonical.encode()).hexdigest()
        self.add_event("expired")
        with self.connect() as conn:
            conn.execute("INSERT INTO diagnostic_run_summaries VALUES (?, '{}')", (identity,))
            before = list(conn.iterdump())
        result = retention.prune_database(self.db, apply=True, now=NOW)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        with self.connect() as conn:
            self.assertEqual(list(conn.iterdump()), before)

    def test_legacy_database_without_shared_tables_remains_readable(self):
        summary = self.seed_compacted("first")
        with self.connect() as conn:
            before = status.build_run(conn, "first")
            conn.execute("DROP TABLE agent_run_summary_refs")
            conn.execute("DROP TABLE diagnostic_run_summaries")
            self.assertEqual(json.loads(agent_runs.find_summary_json_by_run_id(conn, "first")[0][0]), summary)
            self.assertEqual(status.build_run(conn, "first"), before)
        result = retention.prune_database(self.db, apply=True, now=NOW)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["deduplicated_summaries"], 0)

    def test_storage_failure_rolls_back_the_whole_batch(self):
        self.seed_compacted("first")
        self.add_event("expired")
        with self.connect() as conn:
            before = list(conn.iterdump())
        with patch.object(diagnostics, "prune_shared_summaries", side_effect=RuntimeError("stop")):
            with self.assertRaisesRegex(RuntimeError, "stop"):
                retention.prune_database(self.db, apply=True, now=NOW)
        with self.connect() as conn:
            self.assertEqual(list(conn.iterdump()), before)

    def test_different_or_reserved_reference_outputs_are_retained(self):
        self.add_run("different", "failed", RECENT)
        self.add_run("active", "running", RECENT, finished=False)
        for run in ("different", "active"):
            with self.connect() as conn:
                conn.execute("UPDATE agent_runs SET summary_json=? WHERE run_id=?", ('{"repo_summaries":[]}', run))
                conn.execute("UPDATE run_steps SET output_json=? WHERE run_id=?", ('{"reason":"different"}', run))
                conn.execute(
                    "INSERT INTO run_steps(step_id,run_id,step_key,status,output_json) VALUES (?,?,'repo_pipelines','succeeded',?)",
                    ("pipeline-" + run, run, '{"repo_summaries":[],"repo_summaries_ref":"preserve existing value"}'),
                )
        with self.connect() as conn:
            before = list(conn.iterdump())
        result = retention.prune_database(self.db, apply=True, now=NOW)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["referenced_step_outputs"], 0)
        with self.connect() as conn:
            self.assertEqual(list(conn.iterdump()), before)
