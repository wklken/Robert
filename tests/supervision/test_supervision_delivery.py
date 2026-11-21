from robert_agent.delivery.application import receipts as publication_receipts
from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent.persistence import execution_control as execution_store
from robert_agent.acceptance import publish_dedupe as publish_dedupe_acceptance
from robert_agent.persistence.schema import database as storage
from robert_agent.supervision.application import observation


class DeliveryReceiptTests(unittest.TestCase):
    def test_confirmation_preserves_remote_evidence_and_binds_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "db.sqlite3"
            storage.init_database(db)
            publish_dedupe_acceptance._insert_fixture(db)
            with sqlite3.connect(db) as conn:
                conn.row_factory = sqlite3.Row
                action = dict(conn.execute("SELECT * FROM github_actions WHERE action_id='action-comment'").fetchone())
                intent = execution_store.prepare_publication_intent(
                    conn,
                    action_id=action["action_id"],
                    idempotency_key="receipt-test",
                    request_fingerprint="fingerprint",
                    now="2026-11-06T10:00:00+00:00",
                )
                intent = execution_store.transition_publication_intent(
                    conn,
                    intent["publication_id"],
                    "executing",
                    "2026-11-06T10:00:00+00:00",
                    remote_evidence={"source_sha": "a" * 40},
                )
                evidence = {
                    "external_id": "987",
                    "target_url": "https://github.com/x/y/issues/1#issuecomment-987",
                    "marker": "verified-comment-marker",
                }
                publication_receipts.mark_published(
                    conn,
                    action,
                    {"deduplicated": True, "external_id": "987", "response": {"reconciliation": evidence}},
                    "2026-11-06T10:01:00+00:00",
                    intent=intent,
                )
                persisted = json.loads(
                    conn.execute("SELECT remote_evidence_json FROM publication_intents").fetchone()[0]
                )
                self.assertEqual(persisted["source_sha"], "a" * 40)
                self.assertEqual(persisted["reconciliation"], evidence)
                receipt = json.loads(
                    conn.execute(
                        "SELECT metadata_json FROM github_actions WHERE action_id='action-comment'"
                    ).fetchone()[0]
                )["publish"]["receipt"]
                self.assertEqual(receipt["action_id"], action["action_id"])
                self.assertEqual(receipt["result_id"], action["result_id"])
                self.assertEqual(receipt["confirmation_method"], "remote_reconciliation")
                self.assertEqual(receipt["external_id"], "987")
                self.assertEqual(persisted["receipt"], receipt)


class TapdEffectiveDeliveryTests(unittest.TestCase):
    def delivery(self, *, target="https://tapd.example/100/prong/stories/view/1001"):
        scope = {
            "repo": "example/repo",
            "result_head_sha": "a" * 40,
            "sources": [
                {
                    "provider": "tapd",
                    "source_type": "tapd_story",
                    "project_key": "tapd.example/100",
                    "external_id": "1001",
                }
            ],
        }
        plan = [
            {"type": "push_existing_pr", "remote": "origin", "branch": "codex/fix"},
            {"type": "open_pr", "repo": "example/repo", "base": "main", "head": "codex/fix"},
            {"type": "comment", "target_url": target, "body": "Implemented"},
        ]
        facts = {
            "task_id": "task",
            "result_id": "result",
            "expected_output": "new_pr",
            "output_type": "new_pr",
            "planned_actions": json.dumps(plan),
            "audit_status": "accepted",
            "audited_scope": json.dumps(scope),
        }
        actions = []
        for index, payload in enumerate(plan):
            kind = payload["type"]
            actual = dict(payload)
            if kind == "comment":
                actual["body"] += "\n\nPR：https://github.com/example/repo/pull/19"
            action = {
                "action_id": f"action-{index}",
                "task_id": "task",
                "action_type": kind,
                "audit_status": "accepted",
                "publish_status": "published",
                "intent_status": "published",
                "external_id": str(19 + index - 1),
                "target_url": "https://github.com/example/repo/pull/19" if kind == "open_pr" else target,
            }
            receipt = {
                "schema_version": 1,
                "action_id": action["action_id"],
                "task_id": "task",
                "result_id": "result",
                "action_type": kind,
                "scope": scope,
                "external_id": action["external_id"],
                "target_url": action["target_url"],
                "result_head_sha": scope["result_head_sha"],
                "confirmation_method": "publisher_acknowledgement",
                "confirmed_at": "2026-11-16T12:00:00+00:00",
            }
            action["intent_receipt"] = json.dumps(receipt)
            action["metadata_json"] = json.dumps({**actual, "publish": {"receipt": receipt}})
            actions.append(action)
        return facts, actions

    def test_exact_controller_pr_suffix_confirms_without_mutating_evidence(self):
        facts, actions = self.delivery()
        original_plan, original_actions = facts["planned_actions"], json.dumps(actions, sort_keys=True)
        observation._delivery_facts(facts, actions)
        self.assertEqual(facts["delivery_status"], "confirmed", facts)
        self.assertEqual(facts["planned_actions"], original_plan)
        self.assertEqual(json.dumps(actions, sort_keys=True), original_actions)

    def test_suffix_requires_same_result_published_pr_and_current_receipt(self):
        for invalid in ("other_result", "missing_receipt", "receipt_scope", "unpublished", "unaudited", "other_repo"):
            with self.subTest(invalid=invalid):
                facts, actions = self.delivery()
                pr = actions[1]
                payload = json.loads(pr["metadata_json"])
                if invalid == "other_result":
                    payload["publish"]["receipt"]["result_id"] = "other-result"
                elif invalid == "missing_receipt":
                    payload["publish"].pop("receipt")
                elif invalid == "receipt_scope":
                    payload["publish"]["receipt"]["scope"]["repo"] = "other/repo"
                elif invalid == "unpublished":
                    pr["publish_status"] = "not_published"
                elif invalid == "unaudited":
                    pr["audit_status"] = "pending"
                else:
                    pr["target_url"] = "https://github.com/other/repo/pull/19"
                    payload["publish"]["receipt"]["target_url"] = pr["target_url"]
                pr["intent_receipt"] = json.dumps(payload["publish"].get("receipt"))
                pr["metadata_json"] = json.dumps(payload)
                observation._delivery_facts(facts, actions)
                self.assertEqual(facts["delivery_status"], "inconsistent", facts)

    def test_suffix_does_not_allow_other_body_or_target_changes(self):
        for invalid in ("wrong_link", "extra_body", "extra_field", "github_target", "wrong_ticket"):
            with self.subTest(invalid=invalid):
                target = (
                    "https://github.com/example/repo/issues/1"
                    if invalid == "github_target"
                    else "https://tapd.example/100/prong/stories/view/1001"
                )
                facts, actions = self.delivery(target=target)
                comment = actions[-1]
                payload = json.loads(comment["metadata_json"])
                if invalid == "wrong_link":
                    payload["body"] = payload["body"].replace("pull/19", "pull/20")
                elif invalid == "extra_body":
                    payload["body"] += "\nAdditional text"
                elif invalid == "extra_field":
                    payload["extra"] = "changed"
                elif invalid == "wrong_ticket":
                    payload["target_url"] = target.replace("1001", "1002")
                comment["metadata_json"] = json.dumps(payload)
                observation._delivery_facts(facts, actions)
                self.assertEqual(facts["delivery_status"], "inconsistent", facts)

    def test_accepted_suffix_still_requires_comment_receipt(self):
        facts, actions = self.delivery()
        actions[-1]["intent_receipt"] = None
        observation._delivery_facts(facts, actions)
        self.assertEqual(facts["delivery_reason"], "delivery_receipt_unverified", facts)

    def test_accepted_suffix_still_rejects_mismatched_comment_receipt(self):
        facts, actions = self.delivery()
        payload = json.loads(actions[-1]["metadata_json"])
        payload["publish"]["receipt"]["task_id"] = "wrong-task"
        actions[-1]["metadata_json"] = json.dumps(payload)
        observation._delivery_facts(facts, actions)
        self.assertEqual(facts["delivery_reason"], "delivery_receipt_binding_mismatch", facts)

    def test_persisted_historical_actions_are_verified_through_read_only_query(self):
        facts, actions = self.delivery()
        facts["lifecycle"] = "completed"
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "state.db"
            storage.init_database(db)
            with closing(sqlite3.connect(db)) as conn, conn:
                conn.execute(
                    "INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) "
                    "VALUES('repo','example/repo','bot','main','/repo','/worktrees')"
                )
                conn.execute(
                    "INSERT INTO workstreams(workstream_id,repo_id,created_at,updated_at) "
                    "VALUES('stream','repo','2026-11-16T12:00:00+00:00','2026-11-16T12:00:00+00:00')"
                )
                conn.execute(
                    "INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) "
                    "VALUES('task','stream','completed','2026-11-16T12:00:00+00:00','2026-11-16T12:00:00+00:00')"
                )
                conn.execute(
                    "INSERT INTO attempts(attempt_id,task_id,attempt_no,status) VALUES('attempt','task',1,'completed')"
                )
                conn.execute(
                    "INSERT INTO worker_results(result_id,task_id,attempt_id,output_type,created_at,metadata_json) "
                    "VALUES('result','task','attempt','new_pr','2026-11-16T12:00:00+00:00','{}')"
                )
                for action in actions:
                    conn.execute(
                        "INSERT INTO github_actions(action_id,result_id,task_id,action_type,target_url,external_id,"
                        "audit_status,publish_status,created_at,metadata_json) VALUES(?,'result',?,?,?,?,?,?,?,?)",
                        (
                            action["action_id"],
                            action["task_id"],
                            action["action_type"],
                            action["target_url"],
                            action["external_id"],
                            action["audit_status"],
                            action["publish_status"],
                            "2026-11-16T12:00:00+00:00",
                            action["metadata_json"],
                        ),
                    )
                    conn.execute(
                        "INSERT INTO publication_intents(publication_id,action_id,idempotency_key,request_fingerprint,"
                        "status,created_at,updated_at,remote_evidence_json) VALUES(?,?,?,'fingerprint','published',?,?,?)",
                        (
                            "intent-" + action["action_id"],
                            action["action_id"],
                            action["action_id"],
                            "2026-11-16T12:00:00+00:00",
                            "2026-11-16T12:00:00+00:00",
                            json.dumps({"receipt": json.loads(action["intent_receipt"])}),
                        ),
                    )
            with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as conn:
                conn.execute("PRAGMA query_only=ON")
                for _ in range(2):
                    observed = dict(facts)
                    observation._publication_facts(conn, observed, "2026-11-16T13:00:00+00:00")
                    self.assertEqual(observed["delivery_status"], "confirmed", observed)
                self.assertEqual(conn.total_changes, 0)
