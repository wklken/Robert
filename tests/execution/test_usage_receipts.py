"""Launch receipts survive interruption and late registration independently."""

import json
import sqlite3

from robert_agent.adapters.processes import model_usage
from robert_agent.execution import usage
from robert_agent.persistence.stores import model_usage as store
from tests.support.temp_workspace import TemporaryWorkspace


class UsageReceiptTests(TemporaryWorkspace):
    def setUp(self):
        super().setUp()
        self.conn = sqlite3.connect(self.root / "usage.sqlite3")
        self.addCleanup(self.conn.close)
        self.conn.execute(
            "CREATE TABLE model_usage(kind TEXT, call_id TEXT, scope_id TEXT, recorded_at TEXT, usage_json TEXT, PRIMARY KEY(kind,call_id))"
        )
        self.conn.commit()

    def saved(self):
        row = self.conn.execute("SELECT usage_json FROM model_usage WHERE call_id='call'").fetchone()
        return json.loads(row[0]) if row else {}

    def test_missing_terminal_usage_still_leaves_unavailable_receipt(self):
        usage.record_dispatch_usage(
            self.conn, self.root, "stage", "call", "task", {"worker_agent": "tclaude", "process_exited": True}
        )
        self.assertEqual(self.saved().get("measurement_status"), "unavailable")
        self.assertFalse(self.saved()["usage_available"])
        self.assertNotIn("usage", self.saved())
        self.assertEqual(self.saved()["source"], "claude_stream_json")

    def test_partial_complete_duplicate_and_late_partial_are_monotonic(self):
        for status, count in (("partial", 80), ("complete", 100), ("complete", 100), ("partial", 120)):
            store.record(
                self.conn,
                "stage",
                "call",
                "task",
                "2026-11-24T00:00:00Z",
                {"measurement_status": status, "usage_available": True, "usage": {"input_tokens": count}},
            )
        self.assertEqual(self.saved()["usage"]["input_tokens"], 100)
        self.assertEqual(self.saved()["measurement_status"], "complete")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM model_usage").fetchone()[0], 1)

    def test_pending_is_durable_before_exit_and_rollback_remains_caller_owned(self):
        usage.start_call(
            self.conn,
            self.root,
            "stage",
            "call",
            "task",
            {"worker_agent": "tcodex"},
            stage="review",
            started_at="2026-11-24T00:00:00Z",
        )
        self.assertEqual(self.saved()["measurement_status"], "pending")
        self.conn.commit()
        with sqlite3.connect(self.root / "usage.sqlite3") as other:
            self.assertEqual(other.execute("SELECT count(*) FROM model_usage").fetchone()[0], 1)
        usage.record_dispatch_usage(self.conn, self.root, "stage", "call", "task", {}, observed={})
        self.conn.rollback()
        self.assertEqual(self.saved()["measurement_status"], "pending")

    def test_resume_segments_are_independent_and_duplicate_exit_is_idempotent(self):
        for segment, count in (("initial", 100), ("resume", 60), ("resume", 60)):
            usage.start_call(self.conn, self.root, "stage", "call", "task", {}, segment_id=segment)
            usage.record_dispatch_usage(
                self.conn,
                self.root,
                "stage",
                "call",
                "task",
                {},
                segment_id=segment,
                observed={"usage_available": True, "usage": {"input_tokens": count}, "measurement_status": "complete"},
            )
        self.assertEqual(self.saved()["usage"]["input_tokens"], 160)
        self.assertEqual(len(self.saved()["segments"]), 2)

    def test_registration_does_not_add_call_or_change_usage(self):
        store.record(
            self.conn,
            "stage",
            "call",
            "task",
            "2026-11-24T00:00:00Z",
            {"usage_available": True, "usage": {"input_tokens": 100}},
        )
        usage.record_call_event(
            self.conn,
            "stage",
            "call",
            "task",
            "artifact_adopted",
            at="2026-11-24T02:00:00Z",
            metadata={"binding_digest": "abc"},
        )
        usage.record_call_event(self.conn, "stage", "call", "task", "registered", at="2026-11-24T02:01:00Z")
        self.assertEqual(self.saved()["usage"]["input_tokens"], 100)
        self.assertEqual(len(self.saved()["segments"]), 1)
        self.assertEqual(self.saved()["segments"]["call"]["events"]["registered"]["at"], "2026-11-24T02:01:00Z")

    def test_unconfirmed_launch_does_not_count_until_process_is_observed(self):
        usage.start_call(self.conn, self.root, "stage", "call", "task", {}, confirmed=False)
        before = self.saved()["segments"]["call"]
        self.assertFalse(before["launched"])
        self.assertIn("launch_requested", before["events"])
        self.assertNotIn("started", before["events"])
        usage.start_call(self.conn, self.root, "stage", "call", "task", {"pid": 20})
        after = self.saved()["segments"]["call"]
        self.assertTrue(after["launched"])
        self.assertIn("started", after["events"])

    def test_bound_native_resume_is_one_receipt_even_after_history_grows_or_stdout_arrives(self):
        history = self.root / "native.jsonl"

        def write(tokens):
            row = {
                "type": "event_msg",
                "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": tokens}}},
            }
            with history.open("a") as stream:
                stream.write(json.dumps(row) + "\n")

        write(100)
        info = history.stat()
        binding = {
            "history_path": str(history),
            "history_root": str(self.root),
            "agent": "codex",
            "history_identity": {"dev": info.st_dev, "ino": info.st_ino},
        }
        cursor = model_usage.native_resume_usage_cursor(binding)
        write(160)
        terminal = "2026-11-24T12:00:00+00:00"
        with history.open("a") as stream:
            stream.write(
                json.dumps({"timestamp": terminal, "type": "event_msg", "payload": {"type": "task_complete"}}) + "\n"
            )
        dispatch = {
            "worker_agent": "codex",
            "native_resume": {"segment": 1},
            "native_session": binding,
            "native_usage_cursor": cursor,
            "process_exited": True,
        }
        usage.record_dispatch_usage(self.conn, self.root, "stage", "call", "task", dispatch)
        self.assertEqual(self.saved()["usage"]["input_tokens"], 60)
        receipt = self.saved()["segments"]["call"]
        self.assertEqual(receipt["events"]["model_completed"]["at"], terminal)
        self.assertEqual(receipt["measurement_status"], "complete")
        write(300)
        usage.record_dispatch_usage(self.conn, self.root, "stage", "call", "task", dispatch)
        self.assertEqual(self.saved()["usage"]["input_tokens"], 60)
        usage.record_dispatch_usage(
            self.conn,
            self.root,
            "stage",
            "call",
            "task",
            dispatch,
            observed={"measurement_status": "complete", "usage_available": True, "usage": {"input_tokens": 60}},
        )
        self.assertEqual(self.saved()["usage"]["input_tokens"], 60)
        self.assertEqual(self.saved()["measurement_status"], "complete")
        self.assertEqual(len(self.saved()["segments"]), 1)

    def test_scope_rebind_refused_and_complete_counters_never_decrease(self):
        store.record(
            self.conn,
            "stage",
            "call",
            "task",
            "2026-11-24T00:00:00Z",
            {
                "measurement_status": "partial",
                "usage_available": True,
                "usage": {"input_tokens": 80, "output_tokens": 4},
            },
        )
        store.record(
            self.conn,
            "stage",
            "call",
            "other",
            "2026-11-24T00:01:00Z",
            {"usage_available": True, "usage": {"input_tokens": 200}},
        )
        store.record(
            self.conn,
            "stage",
            "call",
            "task",
            "2026-11-24T00:02:00Z",
            {"measurement_status": "complete", "usage_available": True, "usage": {"input_tokens": 70}},
        )
        self.assertEqual(self.saved()["usage"], {"input_tokens": 80, "output_tokens": 4})

    def test_poll_without_exit_preserves_pending_and_never_fabricates_exit(self):
        usage.start_call(self.conn, self.root, "stage", "call", "task", {})
        usage.record_dispatch_usage(self.conn, self.root, "stage", "call", "task", {}, observed={})
        self.assertEqual(self.saved()["measurement_status"], "pending")
        self.assertNotIn("process_exit_observed", self.saved()["segments"]["call"]["events"])

    def test_complete_receipt_receives_exit_fact_without_changing_counters(self):
        store.record(
            self.conn,
            "stage",
            "call",
            "task",
            "2026-11-24T00:00:00Z",
            {"usage_available": True, "usage": {"input_tokens": 100}},
        )
        usage.record_dispatch_usage(
            self.conn, self.root, "stage", "call", "task", {"process_exited": True}, observed={}
        )
        self.assertEqual(self.saved()["usage"]["input_tokens"], 100)
        self.assertIn("process_exit_observed", self.saved()["segments"]["call"]["events"])

    def test_launch_failed_without_process_is_unavailable_without_exit_fact(self):
        usage.start_call(self.conn, self.root, "stage", "call", "task", {})
        usage.record_dispatch_usage(
            self.conn,
            self.root,
            "stage",
            "call",
            "task",
            {"ok": False, "status": "failed_dispatch", "pid": None},
            observed={},
        )
        self.assertEqual(self.saved()["measurement_status"], "unavailable")
        self.assertIn("launch_failed", self.saved()["segments"]["call"]["events"])
        self.assertNotIn("process_exit_observed", self.saved()["segments"]["call"]["events"])

    def test_queued_only_events_do_not_count_as_model_calls_and_first_time_is_pinned(self):
        from robert_agent.readmodels import usage as projection

        usage.record_call_event(self.conn, "stage", "call", "task", "queued", at="2026-11-24T00:00:00Z", stage="review")
        usage.record_call_event(self.conn, "stage", "call", "task", "queued", at="2026-11-24T00:01:00Z", stage="review")
        counts = projection.summarize_receipt_usage(self.root, [], recorded={("diagnosis", "call"): self.saved()})
        self.assertEqual(counts["run_count"], 0)
        self.assertEqual(counts["queued_segment_count"], 1)
        self.assertEqual(self.saved()["segments"]["call"]["events"]["queued"]["at"], "2026-11-24T00:00:00Z")

    def test_cumulative_resume_from_registered_dispatch_counts_only_new_segment(self):
        path = self.root / "stdout.log"

        def write_snapshot(count):
            path.write_text(
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {"total_token_usage": {"input_tokens": count, "output_tokens": count // 10}},
                        },
                    }
                )
                + "\n"
            )

        initial = {
            "worker_agent": "tcodex",
            "stdout_path": str(path),
            "usage_segment_id": "initial",
            "process_exited": True,
        }
        write_snapshot(100)
        usage.start_call(self.conn, self.root, "stage", "call", "task", initial, stage="review")
        usage.record_dispatch_usage(self.conn, self.root, "stage", "call", "task", initial)
        baseline = {"usage": self.saved()["usage"]}
        resume = {**initial, "usage_segment_id": "resume", "usage_baseline": baseline, "parent_run_id": "original"}
        write_snapshot(160)
        usage.start_call(self.conn, self.root, "stage", "call", "task", resume, stage="review")
        usage.record_dispatch_usage(self.conn, self.root, "stage", "call", "task", resume)
        usage.record_dispatch_usage(self.conn, self.root, "stage", "call", "task", resume)
        self.assertEqual(self.saved()["usage"]["input_tokens"], 160)
        self.assertEqual(self.saved()["segments"]["resume"]["usage"]["input_tokens"], 60)
        self.assertEqual(self.saved()["segments"]["resume"]["parent_run_id"], "original")

    def test_pending_provider_label_matches_command_sidecar_and_metadata_is_bounded(self):
        usage.start_call(
            self.conn,
            self.root,
            "stage",
            "call",
            "task",
            {"worker_agent": "command", "model": "configured-model", "private_text": "not retained"},
        )
        usage.record_call_event(
            self.conn,
            "stage",
            "call",
            "task",
            "registered",
            metadata={"binding_digest": "hash", "private_text": "not retained"},
        )
        self.assertEqual(self.saved()["source"], "gateway")
        self.assertNotIn("not retained", json.dumps(self.saved()))
