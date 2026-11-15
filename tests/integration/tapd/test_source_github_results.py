"""Accept real mixed native roots with closed controller GitHub feedback obligations."""

from contextlib import closing
import json
import sqlite3
from unittest.mock import patch

from sqlalchemy import update

from robert_agent.delivery.application import source_completion, source_results
from robert_agent.delivery.application.source_github_publication import publish_github_action
from robert_agent.foundation.values import utc_now
from robert_agent.persistence import database, statements, transactions
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.queries import source_members
from robert_agent.persistence.queries import source_results as roots
from robert_agent.persistence.rounds import source_feedback
from robert_agent.persistence.stores import source_results as write, source_sessions
from tests.support.source_github_lifecycle import SourceGitHubLifecycleFixture


class SourceGitHubResultTests(SourceGitHubLifecycleFixture):
    def test_mixed_model_question_accepts_real_round_and_attaches_exact_feedback(self):
        round_id, read = self.prepare_mixed_model()
        accepted = self.tick("publication_pending", github_discussion_runner=read)
        self.assertEqual(accepted.round_id, round_id)
        self.assertEqual(
            self.read(
                "SELECT r.position,a.provider,a.action_type,a.task_id FROM source_required_actions r "
                "JOIN github_actions a USING(action_id) WHERE r.source_result_id=? ORDER BY r.position",
                (accepted.identity,),
            ),
            [(0, "tapd", "comment", None), (1, "github", "source_feedback", None)],
        )
        self.assertEqual(
            self.read(
                "SELECT state,count(*) FROM request_events WHERE source_result_id=? GROUP BY state",
                (accepted.identity,),
            ),
            [("consumed", 2)],
        )

    def _ownership(self, conn):
        now = utc_now()
        claim = source_sessions.acquire_for_owner(
            conn, source_id=self.ticket.source_id, owner="test-coordinator", now=now, lease_seconds=120
        )
        self.assertIsNotNone(claim)
        return claim, now

    def test_feedback_deduplicates_real_pr_and_retains_all_original_request_targets(self):
        round_id, read = self.prepare_mixed_model((617, 618))
        accepted = self.tick("publication_pending", github_discussion_runner=read)
        root_id = accepted.identity
        with closing(database.connect(self.db)) as conn:
            root = roots.by_id(conn, root_id)
            evidence = json.loads(root["evidence_json"])
            targets = evidence["github_return_targets"]
            self.assertEqual(
                {item["target"]["event_id"] for item in targets}, {"event:comment:617", "event:comment:618"}
            )
            self.assertEqual({item["target"]["creation_action_id"] for item in targets}, {self.action_ids["open_pr"]})
            [planned] = roots.feedback_plan(conn, root)
            body = source_feedback.fixed_body(planned)
            self.assertIn("No code changes were made", body)
            for private in ("ZX991", "TOP-SECRET-771", "tapd.example", "requester", self.ticket.external_id):
                self.assertNotIn(private, body)
            [base, feedback] = roots.required_action_sets(conn, root, roots.required_actions(conn, root_id))
            self.assertEqual([row["provider"] for row in base], ["tapd"])
            self.assertEqual([row["action_id"] for row in feedback], [planned.action_id])
            self.assertEqual(
                len(roots.consumed_members(conn, root, roots.delivery_round(conn, root["source_id"], round_id))), 3
            )

            consumed = source_members.consumed_root_requests(conn, self.ticket.source_id, round_id)
            self.assertEqual({row.ownership.request["provider"] for row in consumed}, {"tapd", "github"})
            self.assertFalse(source_members.consumed_root_requests(conn, "different-source", round_id))

        # Local pacing metadata is independent of the immutable recipe and candidate body.
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE github_actions SET metadata_json=json_set(metadata_json, '$.publish', "
                "json(?)) WHERE action_id IN (SELECT action_id FROM source_required_actions WHERE source_result_id=?)",
                (json.dumps({"status": "deferred", "reason_code": "pending"}), root_id),
            )
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            claim, now = self._ownership(conn)
            self.assertIsNotNone(roots.required_action_sets(conn, root, roots.required_actions(conn, root_id)))
            completion = source_completion.complete_source_result(
                conn, self.configured, claim, source_result_id=root_id, now=now
            )
            self.assertEqual((completion.status, completion.reason), ("held", "required_receipts_unavailable"))
        with patch.object(self, "fail", side_effect=AssertionError("remote")):
            refused = publish_github_action(
                self.db,
                self.configured,
                source_result_id=root_id,
                action_id=planned.action_id,
                owner="test-coordinator",
                run_command=self.fail,
            )
        self.assertEqual((refused.status, refused.reason), ("invalid", "accepted_native_action_unavailable"))
        self.assertEqual(
            self.tick("publication_pending", ticket=self.ticket, github_discussion_runner=read).identity, root_id
        )

    def test_failure_in_feedback_suffix_rolls_back_root_actions_and_ledger(self):
        round_id, read = self.prepare_mixed_model()
        original = write.insert_source_feedback

        def wrong_target(conn, **options):
            ids = original(conn, **options)
            self.assertEqual(len(ids), 1)
            action = tables["github_actions"]
            statements.write(conn, update(action).where(action.c.action_id == ids[0]).values(target_json="{}"))
            return ids

        with (
            patch.object(write, "insert_source_feedback", side_effect=wrong_target),
            self.assertRaisesRegex(ValueError, "lacks required GitHub feedback"),
        ):
            self.tick("publication_pending", github_discussion_runner=read)
        self.assertEqual(self.read("SELECT count(*) FROM source_results WHERE round_id=?", (round_id,)), [(0,)])
        self.assertEqual(self.read("SELECT count(*) FROM github_actions WHERE action_type='source_feedback'"), [(0,)])
        self.assertEqual(
            self.read("SELECT state FROM request_events WHERE event_id='event:comment:617'"), [("pending",)]
        )
        self.tick("publication_pending", github_discussion_runner=read)

    def test_fresh_active_operation_must_still_match_under_acceptance_writer(self):
        round_id, read = self.prepare_mixed_model()
        accept = source_results.accept_model_result

        def superseded(conn, *args, **kwargs):
            statements.write(conn, update(tables["source_github_observations"]).values(operation_id="superseded"))
            return accept(conn, *args, **kwargs)

        with patch.object(source_results, "accept_model_result", side_effect=superseded):
            self.tick("attention", github_discussion_runner=read)
        self.assertEqual(self.read("SELECT count(*) FROM source_results WHERE round_id=?", (round_id,)), [(0,)])
        self.assertEqual(
            self.read("SELECT state FROM request_events WHERE event_id='event:comment:617'"), [("pending",)]
        )
        _, deleted = self._current_github_runner(item_status=404)
        self.tick("canceled", ticket=self.ticket, github_discussion_runner=deleted)
        self.assertEqual(
            self.read("SELECT state FROM request_events WHERE event_id='event:comment:617'"), [("revoked",)]
        )

    def test_audited_mixed_development_keeps_worker_prefix_and_appends_closed_code_recipe(self):
        admitted, prepared, accepted = self.accepted_mixed_development()
        with closing(database.connect(self.db)) as conn:
            root = roots.by_id(conn, accepted.identity)
            required = roots.required_actions(conn, accepted.identity)
            prefix, feedback = roots.required_action_sets(conn, root, required)
            self.assertEqual(
                [(row["action_type"], row["task_id"]) for row in prefix],
                [("push_existing_pr", prepared.identity), ("comment", prepared.identity)],
            )
            [entry] = feedback
            self.assertIsNone(entry["task_id"])
            self.assertEqual(entry["source_result_id"], accepted.identity)
            recipe = json.loads(entry["metadata_json"])["feedback"]
            self.assertEqual(recipe["mode"], "code_checks")
            self.assertEqual(set(recipe), {"version", "mode", "marker", "code_sha256"})
            self.assertNotIn("ZX991", entry["metadata_json"])
            [planned] = roots.feedback_plan(conn, root)
            self.assertIsNone(source_feedback.fixed_body(planned))
            round_ = roots.delivery_round(conn, root["source_id"], admitted.round_id)
            self.assertEqual(
                source_results.accepted_delivery_lineage(conn, self.configured, root, round_, entry),
                (frozenset({"org/alpha"}), "repo:org/alpha"),
            )
            self.assertIsNone(roots.confirmed_required_effects(conn, root["source_result_id"], None))
