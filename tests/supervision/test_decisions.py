from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent.persistence.schema import database as schema
from robert_agent.supervision import decisions
from robert_agent.supervision.application.decision_loop import DecisionLoop
from robert_agent.supervision.application import decision_control


class DecisionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = self.root / "state.db"
        schema.init_database(self.db)
        self.now = datetime.now(timezone.utc).isoformat()
        self.config = {
            "db_path": str(self.db),
            "supervision": {
                "mode": "decide",
                "decision": {"command": [sys.executable], "cooldown_seconds": 1, "timeout_seconds": 3},
            },
            "repos": [{"full_name": "acme/project", "enabled": True}],
        }
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) VALUES ('r','acme/project','bot','main','/repo','/trees')"
            )
            conn.execute(
                "INSERT INTO workstreams(workstream_id,repo_id,created_at,updated_at) VALUES ('w','r',?,?)",
                [self.now] * 2,
            )
            conn.execute(
                "INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) VALUES ('t','w','running',?,?)",
                [self.now] * 2,
            )
            conn.execute(
                "INSERT INTO work_items(work_item_id,repo_id,title,description,origin_type,workstream_id,creation_idempotency_key,created_by,created_at,updated_at) VALUES ('i','r','Deliver the requested change','Keep the original requirements','web','w','key','operator',?,?)",
                [self.now] * 2,
            )
            conn.commit()
        self.candidate_args = {
            "workstream_id": "w",
            "action": "retry_worker",
            "target_id": "attempt-1",
            "facts": {"task_id": "t", "identity_status": "dead"},
            "now": self.now,
        }

    def connect(self):
        return closing(sqlite3.connect(self.db))

    def offer(self, conn):
        conn.execute("BEGIN IMMEDIATE")
        return decisions.offer(conn, self.config, **self.candidate_args)

    def request(self, conn):
        return decisions.packet(decisions.snapshot(conn, "w", self.config, self.now), decisions.read(conn, "w"))

    def output(self, request, action="retry_worker"):
        return {
            "bundle_id": request["bundle_id"],
            "bundle_sha256": request["bundle_sha256"],
            "action": action,
            "candidate_id": request["candidates"][0]["id"] if action == "retry_worker" else None,
            "reason": "The prior process exited and the goal remains unfinished",
            "evidence_ids": ["tasks"],
            "expected_progress": "Observe a new attempt and then a result",
            "recheck_seconds": 300,
            "message": "Which behavior should the change preserve?" if action == "ask_user" else "",
        }

    def test_no_action_until_model_selects_it_and_exact_version_is_current(self):
        with self.connect() as conn:
            self.offer(conn)
            self.assertFalse(decisions.permit(conn, self.config, **self.candidate_args))
            request = self.request(conn)
            instructions = request["instructions"]
            self.assertIn("supervision.decide", instructions)
            self.assertIn("$.evidence", instructions)
            self.assertIn("$.candidates", instructions)
            self.assertIn("$.allowed_actions", instructions)
            self.assertNotIn("Keep the original requirements", instructions)
            self.assertEqual(
                request["bundle_sha256"], decisions.digest({k: v for k, v in request.items() if k != "bundle_sha256"})
            )
            result = decisions.validate(json.dumps(self.output(request)), request)
            self.assertTrue(decisions.approve(conn, self.config, request, result, self.now))
            self.assertTrue(decisions.permit(conn, self.config, **self.candidate_args))
            conn.execute("UPDATE tasks SET lifecycle='canceled' WHERE task_id='t'")
            self.assertFalse(decisions.permit(conn, self.config, **self.candidate_args))

    def test_grant_expires_at_the_recorded_deadline(self):
        for offset, allowed in ((-1, True), (0, False), (1, False)):
            with self.subTest(offset_microseconds=offset), self.connect() as conn:
                self.offer(conn)
                request = self.request(conn)
                self.assertTrue(decisions.approve(conn, self.config, request, self.output(request), self.now))
                expires = datetime.fromisoformat(decisions.read(conn, "w")["expires_at"])
                at = (expires + timedelta(microseconds=offset)).isoformat()
                self.assertEqual(decisions.permit(conn, self.config, **{**self.candidate_args, "now": at}), allowed)

    def test_wait_never_grants_recovery_and_stale_decision_is_rejected(self):
        with self.connect() as conn:
            self.offer(conn)
            request = self.request(conn)
            self.assertTrue(decisions.approve(conn, self.config, request, self.output(request, "wait"), self.now))
            self.assertFalse(decisions.permit(conn, self.config, **self.candidate_args))
            conn.execute("UPDATE work_items SET description='Changed requirement',version=2")
            self.assertFalse(decisions.approve(conn, self.config, request, self.output(request), self.now))

    def test_cannot_invent_an_action_or_select_a_different_evidence_bundle(self):
        with self.connect() as conn:
            self.offer(conn)
            request = self.request(conn)
            for changes in (
                {"action": "merge_pr"},
                {"candidate_id": "invented"},
                {"bundle_sha256": "old"},
                {"evidence_ids": ["invented"]},
                {"recheck_seconds": 0},
            ):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    decisions.validate(json.dumps({**self.output(request), **changes}), request)

    def test_question_and_reply_survive_restart_and_bind_the_exact_question(self):
        with self.connect() as conn:
            self.offer(conn)
            request = self.request(conn)
            decisions.approve(conn, self.config, request, self.output(request, "ask_user"), self.now)
            conn.commit()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            with self.assertRaises(ValueError):
                decisions.reply(conn, self.config, "w", "old-question", "Keep behavior", self.now)
            decisions.reply(conn, self.config, "w", request["bundle_id"], "Keep behavior", self.now)
            self.assertEqual(decisions.snapshot(conn, "w", self.config, self.now)["reply"], "Keep behavior")
            self.assertEqual(conn.execute("SELECT count(*) FROM notifications").fetchone()[0], 1)

    def test_real_model_command_decides_without_an_incident(self):
        script = self.root / "model.py"
        script.write_text("""import json, os, sys
p = json.load(sys.stdin)
assert 'GH_TOKEN' not in os.environ
assert 'Deliver the requested change' in json.dumps(p)
json.dump(dict(bundle_id=p['bundle_id'], bundle_sha256=p['bundle_sha256'], action='wait',
candidate_id=None, reason='Work is underway', evidence_ids=['tasks'], expected_progress='A result',
recheck_seconds=300, message=''), sys.stdout)
""")
        self.config["supervision"]["decision"]["command"] = [sys.executable, str(script)]
        loop = DecisionLoop()
        self.addCleanup(loop.shutdown, self.config)
        deadline = time.monotonic() + 8
        result = {}
        while time.monotonic() < deadline:
            result = loop.poll(self.config)
            if result["status"] == "decided":
                break
            time.sleep(0.02)
        self.assertEqual(result["status"], "decided", result)
        with self.connect() as conn:
            self.assertEqual(decisions.read(conn, "w")["decision"]["action"], "wait")

    def test_rate_limit_transport_outcome_preserves_retry_after(self):
        with self.connect() as conn:
            self.offer(conn)
            request = self.request(conn)
            value = {
                "bundle_id": request["bundle_id"],
                "bundle_sha256": request["bundle_sha256"],
                "error": {"code": "rate_limited", "retry_after_seconds": 120},
            }
            self.assertEqual(decisions.validate(json.dumps(value), request)["error"]["retry_after_seconds"], 120)
            value["error"]["retry_after_seconds"] = -1
            with self.assertRaises(ValueError):
                decisions.validate(json.dumps(value), request)

    def test_cli_reply_resolves_the_board_question_without_starting_work(self):
        with self.connect() as conn:
            self.offer(conn)
            request = self.request(conn)
            decisions.approve(conn, self.config, request, self.output(request, "ask_user"), self.now)
            conn.commit()
        self.assertEqual(
            decision_control.reply(self.config, "w", request["bundle_id"], "Preserve API behavior")["status"],
            "reply_received",
        )
        with self.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)
            self.assertEqual(
                conn.execute("SELECT count(*) FROM work_item_events WHERE resolves_event_id IS NOT NULL").fetchone()[0],
                1,
            )
            self.assertEqual(decisions.read(conn, "w")["status"], "reply_received")

    def test_operator_reply_keeps_authorization_and_does_not_create_a_task(self):
        from robert_agent.work.application import commands
        from robert_agent.work.inputs import CommandContext

        with self.connect() as conn:
            self.offer(conn)
            request = self.request(conn)
            decisions.approve(conn, self.config, request, self.output(request, "ask_user"), self.now)
            event_id = decisions.read(conn, "w")["question_event_id"]
            conn.commit()
        context = CommandContext("operator", "local-user", frozenset({"r"}), frozenset())
        result = commands.execute_command(
            self.db,
            "i",
            context=context,
            command="reply",
            expected_version=1,
            expected_event_id=event_id,
            idempotency_key="answer-1",
            body="Preserve API behavior",
        )
        self.assertTrue(result["ok"], result)
        with self.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)
            self.assertEqual(decisions.read(conn, "w")["status"], "reply_received")

    def test_restart_does_not_spawn_over_an_unknown_claim(self):
        from robert_agent.supervision import store
        from robert_agent.supervision.decisions import RUNNER_KEY

        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            store.update_checkpoint(
                conn,
                *RUNNER_KEY,
                now=self.now,
                metadata={"claim": {"bundle_id": "unresolved", "root_id": "w", "claimed_at": self.now}},
            )
            conn.commit()
        self.assertEqual(DecisionLoop().poll(self.config)["status"], "process_attention")

    def test_notification_rechecks_do_not_repeat_an_unchanged_notice(self):
        with self.connect() as conn:
            self.offer(conn)
            for _ in range(2):
                request = self.request(conn)
                output = self.output(request, "notify_user")
                output["message"] = "Repeated failures need attention"
                decisions.approve(conn, self.config, request, output, self.now)
            self.assertEqual(conn.execute("SELECT count(*) FROM notifications").fetchone()[0], 1)

    def test_model_cannot_finish_an_unverified_goal(self):
        with self.connect() as conn:
            self.offer(conn)
            request = self.request(conn)
            with self.assertRaises(ValueError):
                decisions.validate(json.dumps(self.output(request, "finish_supervision")), request)

    def test_legacy_empty_configuration_does_not_require_a_model(self):
        with self.connect() as conn:
            for config in ({}, {"supervision": {"mode": "observe"}}):
                self.assertTrue(decisions.permit(conn, config, **self.candidate_args))

    def test_next_packet_includes_immediate_decision_and_execution_receipt(self):
        with self.connect() as conn:
            self.offer(conn)
            request = self.request(conn)
            decisions.approve(conn, self.config, request, self.output(request), self.now)
            decisions.executed(conn, self.config, "w", {"resume_attempt_id": "new-attempt"}, self.now)
            next_packet = self.request(conn)
            history = next(e["data"] for e in next_packet["evidence"] if e["id"] == "decision_history")
            self.assertEqual(history[-1]["status"], "verifying")
            self.assertEqual(history[-1]["receipt"]["resume_attempt_id"], "new-attempt")

    def test_structured_credentials_are_removed_before_model_input(self):
        with self.connect() as conn:
            self.offer(conn)
            snapshot = decisions.snapshot(conn, "w", self.config, self.now)
            snapshot["context"]["results"] = [
                {
                    "metadata_json": json.dumps({"password": "opaque-password", "message": "safe fact"}),
                    "token": "opaque-token",
                }
            ]
            encoded = decisions.encode(decisions.packet(snapshot, {}))
            self.assertNotIn("opaque-password", encoded)
            self.assertNotIn("opaque-token", encoded)
            self.assertIn("safe fact", encoded)

    def test_closed_source_and_abandoned_task_do_not_prove_completion(self):
        with self.connect() as conn:
            conn.execute("UPDATE tasks SET lifecycle='canceled'")
            snapshot = decisions.snapshot(conn, "w", self.config, self.now)
            snapshot["context"]["sources"] = [{"state": "closed"}]
            self.assertFalse(decisions.can_finish(snapshot))

    def test_long_requirements_produce_a_bounded_usable_packet(self):
        with self.connect() as conn:
            conn.execute("UPDATE work_items SET description=?", ("requirement detail " * 30000,))
            packet = self.request(conn)
            self.assertLess(len(decisions.encode(packet).encode()), 49152)
            self.assertIn("requirement detail", decisions.encode(packet))

    def test_packet_discloses_compaction_and_preserves_offered_candidate_identity(self):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            offered = []
            for index in range(6):
                candidate = decisions.offer(
                    conn,
                    self.config,
                    **{
                        **self.candidate_args,
                        "target_id": f"attempt-{index}",
                        "facts": {"observation": "failure evidence " * 620},
                    },
                )
                offered.append((candidate["id"], candidate["generation"]))
            packet = self.request(conn)
            self.assertLess(len(decisions.encode(packet).encode()), 49152)
            self.assertCountEqual([(c["id"], c["generation"]) for c in packet["candidates"]], offered)
            self.assertTrue(all("[truncated]" in c["facts"]["observation"] for c in packet["candidates"]))
            self.assertIn("shortened for the bundle byte budget", packet["instructions"])

    def test_skipped_oldest_root_does_not_starve_later_targets(self):
        self.config["supervision"]["batch_size"] = 1
        with self.connect() as conn:
            self.offer(conn)
            request = self.request(conn)
            decisions.approve(conn, self.config, request, self.output(request, "ask_user"), self.now)
            conn.execute(
                "INSERT INTO workstreams(workstream_id,repo_id,created_at,updated_at) VALUES ('z','r',?,?)",
                [self.now] * 2,
            )
            conn.execute(
                "INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) VALUES ('tz','z','running',?,?)",
                [self.now] * 2,
            )
            decisions.write(conn, "z", {"status": "reply_received"}, self.now)
            conn.commit()
        loop = DecisionLoop()
        with patch.object(loop, "_run"):
            results = [loop.poll(self.config), loop.poll(self.config)]
            if loop.thread:
                loop.thread.join()
        self.assertIn("launched", [r["status"] for r in results])

    def test_operator_can_resolve_exact_stranded_claim_after_process_stop(self):
        from robert_agent.supervision import store
        from robert_agent.supervision.decisions import RUNNER_KEY

        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            store.update_checkpoint(
                conn, *RUNNER_KEY, now=self.now, metadata={"claim": {"bundle_id": "stranded", "root_id": "w"}}
            )
            conn.commit()
        self.assertEqual(decision_control.runner_status(self.config)["claim"]["bundle_id"], "stranded")
        with self.assertRaises(ValueError):
            decision_control.resolve_runner(self.config, "different", confirm_process_stopped=True)
        with self.assertRaises(ValueError):
            decision_control.resolve_runner(self.config, "stranded", confirm_process_stopped=False)
        self.assertEqual(
            decision_control.resolve_runner(self.config, "stranded", confirm_process_stopped=True)["status"], "resolved"
        )
        self.assertIsNone(decision_control.runner_status(self.config)["claim"])

    def test_question_uses_scoped_message_envelope_and_reply_actions(self):
        from robert_agent.delivery import messages

        with self.connect() as conn:
            self.offer(conn)
            request = self.request(conn)
            decisions.approve(conn, self.config, request, self.output(request, "ask_user"), self.now)
            config = {**self.config, "messaging": {"enabled": True, "repos": ["acme/project"]}}
            payload = messages.envelope(conn, request["bundle_id"], config=config)
            self.assertIn("reply", payload["actions"])
            self.assertEqual(payload["decision_id"], request["bundle_id"])
            self.assertEqual(payload["work_item_id"], "i")
            self.assertIsNone(
                messages.envelope(conn, request["bundle_id"], config={**config, "messaging": {"repos": []}})
            )

    def test_oversized_family_does_not_block_later_target(self):
        self.config["supervision"]["batch_size"] = 2
        with self.connect() as conn:
            for n in range(33):
                conn.execute(
                    "INSERT INTO workstreams(workstream_id,repo_id,origin_workstream_id,created_at,updated_at) VALUES (?,'r','w',?,?)",
                    [f"child{n}", self.now, self.now],
                )
            conn.execute(
                "INSERT INTO workstreams(workstream_id,repo_id,created_at,updated_at) VALUES ('z','r',?,?)",
                [self.now] * 2,
            )
            conn.execute(
                "INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) VALUES ('tz','z','running',?,?)",
                [self.now] * 2,
            )
            conn.commit()
        loop = DecisionLoop()
        with patch.object(loop, "_run"):
            result = loop.poll(self.config)
            if loop.thread:
                loop.thread.join()
        self.assertEqual(result, {"status": "launched", "workstream_id": "z"})

    def test_operator_cannot_release_known_live_model_claim(self):
        from robert_agent.supervision import store
        from robert_agent.adapters.processes import identity

        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            store.update_checkpoint(
                conn,
                *decisions.RUNNER_KEY,
                now=self.now,
                metadata={
                    "claim": {"bundle_id": "live", "root_id": "w", "process_identity": {"process_group_id": 123}}
                },
            )
            conn.commit()
        with (
            patch.object(identity, "inspect_recorded_identity", return_value="alive_same"),
            self.assertRaises(ValueError),
        ):
            decision_control.resolve_runner(self.config, "live", confirm_process_stopped=True)

    def test_completion_checks_goals_beyond_the_packet_row_limit(self):
        with self.connect() as conn:
            conn.execute("UPDATE tasks SET lifecycle='completed'")
            conn.execute("UPDATE work_items SET completed_at=?", (self.now,))
            for n in range(17):
                conn.execute(
                    "INSERT INTO workstreams(workstream_id,repo_id,origin_workstream_id,created_at,updated_at) VALUES (?,'r','w',?,?)",
                    [f"x{n:02}", self.now, self.now],
                )
                conn.execute(
                    "INSERT INTO work_items(work_item_id,repo_id,title,description,origin_type,workstream_id,creation_idempotency_key,created_by,created_at,updated_at,completed_at) VALUES (?,'r','goal','details','web',?,?,'operator',?,?,?)",
                    [f"x{n:02}", f"x{n:02}", f"goal-{n}", self.now, self.now, self.now if n < 16 else None],
                )
            self.assertFalse(decisions.can_finish(decisions.snapshot(conn, "w", self.config, self.now)))

    def test_json_shaped_question_remains_text_after_redaction(self):
        with self.connect() as conn:
            self.offer(conn)
            request = self.request(conn)
            output = {
                **self.output(request, "ask_user"),
                "message": json.dumps({"question": "Which release?", "token": "opaque-secret"}),
            }
            validated = decisions.validate(json.dumps(output), request)
            self.assertIsInstance(validated["message"], str)
            self.assertNotIn("opaque-secret", validated["message"])
            self.assertTrue(decisions.approve(conn, self.config, request, validated, self.now))
            self.assertEqual(decisions.read(conn, "w")["status"], "ask_user")
