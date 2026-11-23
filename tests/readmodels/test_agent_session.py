from contextlib import closing
import hashlib
import json
import sqlite3
from unittest.mock import patch

from tests.support.summary_database import SummaryFixture


class AgentSessionTests(SummaryFixture):
    def setUp(self):
        super().setUp()
        self.db_path = self.root / "dd.sqlite3"
        self._init_summary_db(self.db_path)
        self.session_dir = self.root / ".tcodex/sessions/2026/11/20"
        self.session_dir.mkdir(parents=True)
        self.sid = "01a1f6d9-45c5-7670-8957-9abde29f8db4"
        self.session_file = self.session_dir / f"rollout-2026-11-20T10-46-36-{self.sid}.jsonl"
        self.stdout = self.root / "task-artifacts/worker.stdout.log"
        self.stdout.write_text("")
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE attempts SET started_at=?, status='running', metadata_json=? WHERE attempt_id='attempt-1'",
                (
                    "2026-11-20T02:46:34Z",
                    json.dumps({"dispatch": {"worker_agent": "tcodex"}}),
                ),
            )
        self.addCleanup(patch.stopall)
        patch("pathlib.Path.home", return_value=self.root).start()

    def write_session(self, items, *, task="task-1", attempt="attempt-1"):
        rows = [
            {"type": "session_meta", "payload": {"id": self.sid, "source": "exec"}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": f"task_id: {task}\nattempt_id: {attempt}\n"}],
                },
            },
        ]
        rows.extend(
            {
                "timestamp": "2026-11-20T03:00:00Z",
                "type": "event_msg",
                "payload": {"type": "item_completed", "item": item},
            }
            for item in items
        )
        rows.insert(
            2,
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "UserMessage",
                        "id": "prompt",
                        "content": [{"text": f"task_id: {task}\nattempt_id: {attempt}\n"}],
                    },
                },
            },
        )
        self.session_file.write_text("".join(json.dumps(row) + "\n" for row in rows))

    def page(self, **kwargs):
        from robert_agent.readmodels.agent_session import load_agent_session

        return load_agent_session(self.db_path, "task-1", "attempt-1", **kwargs)

    def test_empty_stdout_discovers_native_session_and_pages_newest_first(self):
        self.write_session(
            [
                {"type": "AgentMessage", "id": f"message-{i}", "content": [{"type": "Text", "text": f"Message {i}"}]}
                for i in range(7)
            ]
        )
        first = self.page(limit=3)
        self.assertEqual(first["session_id"], self.sid)
        self.assertEqual(first["source"], "session")
        self.assertEqual([r["text"] for r in first["items"]], ["Message 6", "Message 5", "Message 4"])
        self.assertTrue(first["active"])
        second = self.page(limit=3, cursor=first["next_cursor"])
        self.assertEqual([r["text"] for r in second["items"]], ["Message 3", "Message 2", "Message 1"])
        third = self.page(limit=3, cursor=second["next_cursor"])
        self.assertEqual(third["items"][0]["text"], "Message 0")
        self.assertIsNone(third["next_cursor"])

    def test_public_records_filter_runtime_context_and_raw_reasoning(self):
        self.write_session(
            [
                {
                    "type": "CommandExecution",
                    "id": "cmd",
                    "command": ["bash", "-lc", "git status"],
                    "status": "completed",
                    "exit_code": 0,
                    "aggregated_output": "clean",
                },
                {
                    "type": "FileChange",
                    "id": "file",
                    "changes": {"src/app.py": {"type": "update", "unified_diff": "+hello"}},
                },
                {
                    "type": "Reasoning",
                    "id": "reason",
                    "summary_text": ["Visible summary"],
                    "raw_content": ["PRIVATE RAW"],
                },
                {"type": "Reasoning", "id": "empty-reason", "summary_text": [], "raw_content": ["PRIVATE RAW"]},
                {"type": "reasoning", "id": "native-detail", "text": "PRIVATE RAW"},
            ]
        )
        page = self.page()
        self.assertEqual([r["kind"] for r in page["items"]], ["reasoning", "file_change", "command", "user"])
        self.assertEqual(page["items"][0]["text"], "Visible summary")
        self.assertEqual(page["items"][2]["output"], "clean")
        self.assertNotIn("PRIVATE RAW", json.dumps(page))
        self.assertNotIn("session_meta", json.dumps(page))

    def test_event_metadata_is_bounded_and_does_not_expose_raw_values(self):
        self.write_session([{"type": "AgentMessage", "id": "private-event-identifier", "text": "Visible message"}])
        rows = [json.loads(line) for line in self.session_file.read_text().splitlines()]
        rows[-1]["timestamp"] = "private-timestamp-field"
        self.session_file.write_text("".join(json.dumps(row) + "\n" for row in rows))

        first = self.page(limit=1)["items"][0]
        self.assertEqual(first["text"], "Visible message")
        self.assertIsNone(first["timestamp"])
        self.assertNotIn("private-event-identifier", json.dumps(first))
        self.assertLess(len(first["id"]), 64)
        self.assertEqual(self.page(limit=1)["items"][0]["id"], first["id"])

    def test_trusted_home_and_data_root_aliases_preserve_session_reads(self):
        from robert_agent.readmodels.agent_session import load_agent_session

        alias = self.root / "trusted-root-alias"
        alias.symlink_to(self.root, target_is_directory=True)
        self.write_session([{"type": "AgentMessage", "text": "From native history"}])
        with patch("pathlib.Path.home", return_value=alias):
            self.assertEqual(self.page()["items"][0]["text"], "From native history")
        self.session_file.unlink()
        self.stdout.write_text(
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "From stdout"}}) + "\n"
        )
        page = load_agent_session(alias / self.db_path.name, "task-1", "attempt-1")
        self.assertEqual([item["text"] for item in page["items"]], ["From stdout"])

    def test_wrong_attempt_and_foreign_session_are_not_exposed(self):
        self.write_session(
            [{"type": "AgentMessage", "id": "foreign", "content": [{"text": "OTHER TASK"}]}], attempt="attempt-foreign"
        )
        self.assertEqual(self.page()["items"], [])
        from robert_agent.readmodels.agent_session import load_agent_session

        self.assertIsNone(load_agent_session(self.db_path, "task-other", "attempt-1"))

    def test_session_symlink_and_symlink_directory_are_refused(self):
        self.write_session([{"type": "AgentMessage", "id": "msg", "content": [{"text": "SECRET OUTSIDE"}]}])
        outside = self.root / "outside.jsonl"
        self.session_file.rename(outside)
        self.session_file.symlink_to(outside)
        self.assertEqual(self.page()["items"], [])
        self.session_file.unlink()
        self.session_dir.rmdir()
        self.session_dir.symlink_to(self.root)
        self.assertEqual(self.page()["items"], [])

    def test_append_does_not_shift_older_cursor_and_partial_json_is_ignored(self):
        self.write_session(
            [{"type": "AgentMessage", "id": f"msg-{i}", "content": [{"text": str(i)}]} for i in range(4)]
        )
        first = self.page(limit=2)
        appended = json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {"type": "AgentMessage", "id": "msg-new", "content": [{"text": "new"}]},
                },
            }
        )
        with self.session_file.open("a") as fh:
            fh.write(appended[:30])
        self.assertEqual(self.page(limit=2)["items"][0]["text"], "3")
        with self.session_file.open("a") as fh:
            fh.write(appended[30:] + "\n")
        self.assertEqual(self.page(limit=2)["items"][0]["text"], "new")
        self.assertEqual([r["text"] for r in self.page(limit=2, cursor=first["next_cursor"])["items"]], ["1", "0"])

    def test_large_text_is_bounded_and_secrets_are_redacted_before_truncation(self):
        self.write_session(
            [
                {"type": "AgentMessage", "id": "long", "content": [{"text": "x" * 50000}]},
                {
                    "type": "CommandExecution",
                    "id": "secret",
                    "command": ["curl"],
                    "aggregated_output": "api_key=very-sensitive-value",
                    "status": "completed",
                },
            ]
        )
        page = self.page()
        self.assertNotIn("very-sensitive-value", json.dumps(page))
        self.assertTrue(page["items"][1]["truncated"])
        self.assertLess(len(page["items"][1]["text"]), 20000)

    def test_registered_stdout_fallback_normalizes_exec_events(self):
        rows = [
            {"type": "thread.started", "thread_id": self.sid},
            {"type": "item.started", "item": {"id": "cmd", "type": "command_execution", "command": "git status"}},
            {
                "type": "item.completed",
                "item": {
                    "id": "cmd",
                    "type": "command_execution",
                    "command": "git status",
                    "aggregated_output": "clean",
                    "status": "completed",
                },
            },
            {"type": "item.completed", "item": {"id": "msg", "type": "agent_message", "text": "Done"}},
        ]
        self.stdout.write_text("".join(json.dumps(r) + "\n" for r in rows))
        page = self.page()
        self.assertEqual(page["source"], "stdout")
        self.assertEqual([r["kind"] for r in page["items"]], ["assistant", "command"])

    def test_invalid_and_replaced_file_cursor_are_rejected(self):
        from robert_agent.readmodels.agent_session import SessionQueryError

        self.write_session([{"type": "AgentMessage", "id": str(i), "content": [{"text": str(i)}]} for i in range(3)])
        cursor = self.page(limit=1)["next_cursor"]
        with self.assertRaises(SessionQueryError):
            self.page(cursor="../../etc/passwd")
        previous = self.session_file.with_suffix(".old")
        self.session_file.rename(previous)
        self.write_session([])
        with self.assertRaises(SessionQueryError):
            self.page(cursor=cursor)

    def test_legacy_response_items_render_once_with_tool_results(self):
        self.write_session([])
        rows = [json.loads(line) for line in self.session_file.read_text().splitlines()][:2]
        rows += [
            {
                "type": "response_item",
                "payload": {"type": "message", "role": "assistant", "content": [{"text": "Legacy message"}]},
            },
            {
                "type": "response_item",
                "payload": {"type": "function_call", "id": "call", "name": "shell", "arguments": '{"cmd":"pwd"}'},
            },
            {
                "type": "response_item",
                "payload": {"type": "function_call_output", "id": "result", "output": "workspace"},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "reasoning",
                    "summary": [{"text": "Public summary"}],
                    "encrypted_content": "DO NOT EXPOSE",
                },
            },
        ]
        self.session_file.write_text("".join(json.dumps(row) + "\n" for row in rows))
        page = self.page()
        self.assertEqual([r["kind"] for r in page["items"]], ["reasoning", "tool_output", "tool", "assistant", "user"])
        self.assertEqual(page["items"][1]["output"], "workspace")
        self.assertNotIn("DO NOT EXPOSE", json.dumps(page))

    def test_oversized_record_does_not_hide_older_history_or_loop_cursor(self):
        self.write_session(
            [
                {"type": "AgentMessage", "id": "old", "content": [{"text": "Older useful message"}]},
                {"type": "AgentMessage", "id": "huge", "content": [{"text": "x" * 700000}]},
                {"type": "AgentMessage", "id": "new", "content": [{"text": "Newest useful message"}]},
            ]
        )
        page = self.page()
        self.assertEqual(
            [r["text"] for r in page["items"] if r["kind"] == "assistant"],
            ["Newest useful message", "Older useful message"],
        )
        self.assertTrue(page["limited"])
        self.assertIsNone(page["next_cursor"])

    def test_foreign_stdout_thread_id_does_not_bypass_prompt_binding(self):
        self.write_session([{"type": "AgentMessage", "content": [{"text": "FOREIGN"}]}], task="other-task")
        self.stdout.write_text(json.dumps({"type": "thread.started", "thread_id": self.sid}) + "\n")
        self.assertEqual(self.page()["items"], [])

    def test_bad_record_shapes_are_ignored_without_crashing(self):
        self.write_session([{"type": "AgentMessage", "id": "fine", "content": [{"text": "Fine"}]}])
        with self.session_file.open("a") as fh:
            fh.write('[]\n{"type":"event_msg","payload":null}\ninvalid\n')
        self.assertEqual(self.page()["items"][0]["text"], "Fine")

    def test_coordinator_dispatch_still_discovers_implementation_history(self):
        self.write_session([{"type": "AgentMessage", "id": "impl", "content": [{"text": "Implementation done"}]}])
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE attempts SET metadata_json=? WHERE attempt_id='attempt-1'",
                (json.dumps({"dispatch": {"worker_agent": "command"}}),),
            )
        self.assertEqual(self.page()["source"], "session")
        self.assertEqual(self.page()["items"][0]["text"], "Implementation done")

    def test_model_run_uses_its_own_registered_thread_and_owner_attempt(self):
        self.write_session(
            [{"type": "AgentMessage", "id": "reviewer", "content": [{"text": "Review result"}]}], attempt="review-run-1"
        )
        log = self.root / "review.stdout.log"
        log.write_text(json.dumps({"type": "thread.started", "thread_id": self.sid}) + "\n")
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO pre_pr_review_sessions(round_id, task_id, request_json, created_at, updated_at) VALUES('round-1','task-1','{}',?,?)",
                ("2026-11-20T02:46:34Z", "2026-11-20T02:46:34Z"),
            )
            conn.execute(
                "INSERT INTO pre_pr_review_runs(run_id,round_id,stage,worker_id,attempt_no,owner_attempt_id,status,output_dir,prompt_sha256,started_at,dispatch_json,created_at,updated_at) VALUES('review-run-1','round-1','review','reviewer',1,'attempt-1','running',?,'digest',?,?,?,?)",
                (
                    str(self.root / "review"),
                    "2026-11-20T02:46:34Z",
                    json.dumps({"stdout_path": str(log), "worker_agent": "tcodex"}),
                    "2026-11-20T02:46:34Z",
                    "2026-11-20T02:46:34Z",
                ),
            )
        digest = hashlib.sha256(b"task_id: task-1\nattempt_id: review-run-1\n").hexdigest()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE pre_pr_review_runs SET prompt_sha256=? WHERE run_id='review-run-1'", (digest,))
        page = self.page(run_id="review-run-1")
        self.assertEqual(page["source"], "session")
        self.assertEqual(page["items"][0]["text"], "Review result")
        self.assertEqual(page["runs"][0]["run_id"], "review-run-1")
        self.assertIsNone(self.page(run_id="foreign-run"))
        self.assertEqual(self.page()["items"], [])
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE pre_pr_review_runs SET prompt_sha256='wrong-digest' WHERE run_id='review-run-1'")
        self.assertEqual(self.page(run_id="review-run-1")["items"], [])

    def test_scan_budget_still_allows_loading_earlier_useful_records(self):
        self.write_session([{"type": "AgentMessage", "id": "old", "content": [{"text": "Older message"}]}])
        with self.session_file.open("a") as fh:
            fh.write((json.dumps({"type": "world_state", "payload": {"data": "x" * 10000}}) + "\n") * 500)
        page = self.page()
        self.assertTrue(page["limited"])
        self.assertTrue(page["next_cursor"])
        old = self.page(cursor=page["next_cursor"])
        self.assertEqual(old["items"][0]["text"], "Older message")

    def test_malformed_item_fields_do_not_break_other_records(self):
        self.write_session(
            [
                {"type": "AgentMessage", "id": "valid", "content": [{"text": "Valid"}]},
                {"type": ["AgentMessage"], "content": None},
                {"type": "AgentMessage", "id": "bad", "status": {"secret": "value"}, "content": []},
            ]
        )
        self.assertTrue(any(record["text"] == "Valid" for record in self.page()["items"]))

    def test_malformed_event_types_do_not_hide_valid_native_or_stdout_records(self):
        self.write_session([])
        rows = [json.loads(line) for line in self.session_file.read_text().splitlines()][:2]
        rows += [
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": "Valid"}},
            {"type": "response_item", "payload": {"type": []}},
            {"type": "response_item", "payload": {"type": "message", "role": []}},
        ]
        self.session_file.write_text("".join(json.dumps(row) + "\n" for row in rows))
        self.assertEqual(self.page()["items"][0]["text"], "Valid")
        self.session_file.unlink()
        self.stdout.write_text(
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Valid stdout"}})
            + '\n{"type": []}\n'
        )
        self.assertEqual(self.page()["items"][0]["text"], "Valid stdout")
