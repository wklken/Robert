"""Check current mixed accepted-root authority at the actual TAPD publisher."""

from contextlib import closing
from hashlib import sha256
import sqlite3
from unittest.mock import patch

from robert_agent.adapters.tapd.publication import TapdPublisherIdentity
from robert_agent.delivery.application import source_delivery_observation
from robert_agent.delivery.application.source_publication import publish_tapd_action
from robert_agent.foundation.values import utc_now
from robert_agent.work import tapd_authority as facts
from robert_agent.work.application import tapd_intake
from tests.support.source_github_lifecycle import SourceGitHubLifecycleFixture


class SourceGitHubDeliveryAuthorityTests(SourceGitHubLifecycleFixture):
    def accepted(self, identifiers=(617,), **options):
        round_id, read = self.prepare_mixed_model(identifiers, **options)
        step = self.tick("publication_pending", github_discussion_runner=read)
        [[action_id]] = self.read(
            "SELECT a.action_id FROM source_required_actions r JOIN github_actions a USING(action_id) "
            "WHERE r.source_result_id=? AND a.provider='tapd'",
            (step.identity,),
        )
        return round_id, step.identity, action_id

    def publish_reply(self, root_id, action_id, *, reader, fault=False):
        replies = getattr(self, "replies", [])
        self.replies = replies
        test = self

        class Client:
            def __init__(self, **_):
                pass

            def add_comment(self, workspace_id, kind, external_id, body, *, stop):
                test.assertEqual((workspace_id, kind, external_id), test.ticket.key[1:])
                with closing(sqlite3.connect(test.db, timeout=0)) as conn, conn:
                    conn.execute("BEGIN IMMEDIATE")
                version = facts.CommentVersion(
                    test.ticket, "bug", "201", "2026-11-15 00:00:00", None, sha256(body.encode()).hexdigest()
                )
                test.comments.append(
                    facts.CommentObservation(
                        version,
                        body,
                        "tapd-service",
                        facts.CreatorEvidence(version, "tapd-service", "creator-proof"),
                        facts.OwnBodyEvidence(version, "tapd-service", "body-proof"),
                    )
                )
                replies.append(body)
                if fault:
                    raise TimeoutError("simulated timeout after committing write")
                return version.external_id

        def identity(_client, ticket, **_):
            return TapdPublisherIdentity(ticket.host, ticket.workspace_id, "tapd-service", "account-proof", utc_now())

        def observe(client, ticket, **options):
            with closing(sqlite3.connect(self.db, timeout=0)) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
            return self.provider(client, ticket, **options)

        return publish_tapd_action(
            self.db,
            self.configured,
            source_result_id=root_id,
            action_id=action_id,
            owner="test-coordinator",
            client_factory=Client,
            identity_provider=identity,
            observation_provider=observe,
            github_discussion_runner=reader,
        )

    def test_real_mixed_model_result_publishes_one_tapd_receipt_and_keeps_feedback_pending(self):
        round_id, root_id, action_id = self.accepted()

        def outside(endpoint):
            if endpoint.endswith("/issues/comments/617"):
                with closing(sqlite3.connect(self.db, timeout=0)) as conn, conn:
                    conn.execute("BEGIN IMMEDIATE")

        calls, read = self._current_github_runner(during_read=outside)
        outcome = self.publish_reply(root_id, action_id, reader=read)
        self.assertEqual((outcome.status, outcome.external_id), ("published", "201"), outcome)
        self.assertEqual(outcome.completion.status, "held")
        self.assertEqual(len(self.replies), 1)
        self.assertIn("repos/org/alpha/issues/comments/617", calls)
        self.assertEqual(
            self.read(
                "SELECT a.provider,a.publish_status FROM source_required_actions r "
                "JOIN github_actions a USING(action_id) WHERE r.source_result_id=? ORDER BY r.position",
                (root_id,),
            ),
            [("tapd", "published"), ("github", "not_published")],
        )
        self.assertEqual(self.read("SELECT status FROM source_rounds WHERE round_id=?", (round_id,)), [("active",)])

    def commitment(self, round_id, root_id):
        return (
            self.read("SELECT input_snapshot_json,input_sha256 FROM source_rounds WHERE round_id=?", (round_id,)),
            self.read("SELECT evidence_json,payload_json FROM source_results WHERE source_result_id=?", (root_id,)),
            self.read(
                "SELECT i.phase,i.authorization_json,i.content_snapshot_json,i.content_version,e.state,e.source_result_id,e.consumed_at "
                "FROM source_round_requests r JOIN ingress_requests i USING(request_id) JOIN request_events e USING(request_id) "
                "WHERE r.round_id=? ORDER BY r.request_id",
                (round_id,),
            ),
        )

    def test_authorized_post_acceptance_edit_does_not_rewrite_original_result_or_consumption(self):
        round_id, root_id, action_id = self.accepted()
        commitment = self.commitment(round_id, root_id)
        calls, edited = self._current_github_runner(body="@bot changed after acceptance", edited="2026-11-14T20:03:00Z")
        result = self.publish_reply(root_id, action_id, reader=edited)
        self.assertEqual(result.status, "published", result)
        self.assertIn("repos/org/alpha/issues/comments/617", calls)
        self.assertEqual(self.commitment(round_id, root_id), commitment)
        self.assertNotIn("changed after acceptance", self.replies[0])

    def _assert_accepted_revocation(self, expected, **options):
        round_id, root_id, action_id = self.accepted()
        commitment = self.commitment(round_id, root_id)
        _, altered = self._current_github_runner(**options)
        result = self.publish_reply(root_id, action_id, reader=altered)
        self.assertEqual((result.status, result.reason), ("deferred", "source_grant_unavailable"))
        self.assertEqual(self.replies, [])
        self.assertEqual(
            self.read("SELECT reason FROM source_round_cancellations WHERE round_id=?", (round_id,)), [(expected,)]
        )
        self.assertEqual(self.commitment(round_id, root_id), commitment)
        self.assertFalse(self.read("SELECT 1 FROM publication_intents WHERE action_id=?", (action_id,)))

    def test_removed_mention_cancels_new_reply_without_mutating_consumed_member(self):
        self._assert_accepted_revocation("github_current_mention_removed", body="Removed mention")

    def test_changed_current_creator_cancels_new_reply(self):
        self._assert_accepted_revocation("github_current_creator_changed", creator="outsider", actor="outsider")

    def test_read_proved_deletion_cancels_new_reply(self):
        self._assert_accepted_revocation("github_item_absence_confirmed", item_status=404)

    def test_unreadable_last_member_and_known_negative_with_sibling_unavailable_on_tapd_subset(self):
        identifiers = tuple(range(617, 623))
        round_id, root_id, action_id = self.accepted(identifiers, handle_github=False)
        commitment = self.commitment(round_id, root_id)
        ordered = [
            int(event.rsplit(":", 1)[1])
            for (event,) in self.read(
                "SELECT e.event_id FROM source_round_requests r JOIN request_events e USING(request_id) "
                "JOIN ingress_requests i USING(request_id) WHERE r.round_id=? AND i.provider='github' ORDER BY r.request_id",
                (round_id,),
            )
        ]
        self.assertEqual(len(ordered), 6)
        self.assertEqual(self.read("SELECT count(*) FROM request_events WHERE source_result_id=?", (root_id,)), [(1,)])
        self.assertEqual(
            self.read("SELECT count(*) FROM source_required_actions WHERE source_result_id=?", (root_id,)), [(1,)]
        )
        readers = {identifier: self._current_github_runner(identifier=identifier)[1] for identifier in identifiers}
        readers[ordered[-1]] = self._current_github_runner(identifier=ordered[-1], item_status=503)[1]
        selected = identifiers[0]
        probed = []

        def read(command, **options):
            nonlocal selected
            if "/issues/comments/" in command[7]:
                selected = int(command[7].rsplit("/", 1)[1])
                probed.append(selected)
            return readers[selected](command, **options)

        self.assertEqual(self.publish_reply(root_id, action_id, reader=read).status, "deferred")
        self.assertEqual(probed, ordered)
        self.assertFalse(self.read("SELECT 1 FROM source_round_cancellations WHERE round_id=?", (round_id,)))
        probed.clear()
        readers[ordered[0]] = self._current_github_runner(identifier=ordered[0], item_status=404)[1]
        self.assertEqual(self.publish_reply(root_id, action_id, reader=read).status, "deferred")
        self.assertEqual(probed, ordered)
        self.assertEqual(
            self.read("SELECT reason FROM source_round_cancellations WHERE round_id=?", (round_id,)),
            [("github_item_absence_confirmed",)],
        )
        self.assertEqual(self.commitment(round_id, root_id), commitment)
        self.assertEqual(self.replies, [])

    def test_unknown_original_tapd_write_reconciles_after_github_revocation_without_reposting(self):
        round_id, root_id, action_id = self.accepted()
        commitment = self.commitment(round_id, root_id)
        _, current = self._current_github_runner()
        unknown = self.publish_reply(root_id, action_id, reader=current, fault=True)
        self.assertEqual((unknown.status, unknown.reason), ("unknown", "write_outcome_unknown"))
        [[intent, state]] = self.read(
            "SELECT publication_id,status FROM publication_intents WHERE action_id=?", (action_id,)
        )
        self.assertEqual(state, "attention")
        reads, revoked = self._current_github_runner(item_status=404)
        outcome = self.publish_reply(root_id, action_id, reader=revoked)
        self.assertEqual((outcome.status, outcome.external_id), ("published", "201"))
        self.assertEqual(outcome.completion.status, "held")
        self.assertEqual(reads, [])
        self.assertEqual(len(self.replies), 1)
        self.assertEqual(
            self.read("SELECT publication_id,status FROM publication_intents WHERE action_id=?", (action_id,)),
            [(intent, "published")],
        )
        self.assertEqual(self.commitment(round_id, root_id), commitment)

    def test_active_execution_proof_cannot_authorize_an_already_accepted_action(self):
        active = tapd_intake.refresh_mixed_round
        seen = []

        def capture(*args, **kwargs):
            refreshed = active(*args, **kwargs)
            if refreshed.worker_proof is not None:
                seen.append(refreshed.worker_proof)
            return refreshed

        with patch.object(tapd_intake, "refresh_mixed_round", side_effect=capture):
            _, root_id, action_id = self.accepted()
        self.assertTrue(seen)
        _, current = self._current_github_runner()
        with patch.object(source_delivery_observation, "observe", return_value=seen[-1]):
            outcome = self.publish_reply(root_id, action_id, reader=current)
        self.assertEqual((outcome.status, outcome.reason), ("deferred", "source_grant_unavailable"))
        self.assertEqual(self.replies, [])

    def test_required_feedback_suffix_tampering_refuses_even_tapd_lineage_and_post(self):
        _, root_id, action_id = self.accepted()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE github_actions SET target_json='{}' WHERE action_type='source_feedback'")
        calls, current = self._current_github_runner()
        result = self.publish_reply(root_id, action_id, reader=current)
        self.assertEqual(result.status, "deferred")
        self.assertEqual(self.replies, [])
        self.assertEqual(calls, [])

    def test_action_proof_refuses_superseded_github_operation_tapd_revision_or_owner(self):
        round_id, root_id, action_id = self.accepted()
        commitment = self.commitment(round_id, root_id)
        observe = source_delivery_observation.observe
        for drift in ("operation", "revision", "owner", "action"):
            with self.subTest(drift=drift):

                def supersede(*args, **kwargs):
                    checked = observe(*args, **kwargs)
                    self.assertIsNotNone(checked)
                    with closing(sqlite3.connect(self.db, timeout=0)) as conn, conn:
                        if drift == "operation":
                            conn.execute("UPDATE source_github_observations SET operation_id='superseded'")
                        elif drift == "revision":
                            conn.execute(
                                "UPDATE sources SET metadata_json=json_set(metadata_json,'$.revision','superseded') "
                                "WHERE source_id=?",
                                (self.ticket.source_id,),
                            )
                        elif drift == "action":
                            conn.execute(
                                "UPDATE github_actions SET target_url='changed' WHERE action_id=?", (action_id,)
                            )
                        else:
                            conn.execute(
                                "UPDATE source_sessions SET token=token+1 WHERE source_id=?", (self.ticket.source_id,)
                            )
                    return checked

                _, current = self._current_github_runner()
                with patch.object(source_delivery_observation, "observe", side_effect=supersede):
                    result = self.publish_reply(root_id, action_id, reader=current)
                self.assertEqual(result.status, "deferred")
                self.assertEqual(self.replies, [])
                self.assertEqual(self.commitment(round_id, root_id), commitment)
                if drift == "action":
                    with closing(sqlite3.connect(self.db)) as conn, conn:
                        conn.execute("UPDATE github_actions SET target_url=NULL WHERE action_id=?", (action_id,))
        self.assertEqual(self.publish_reply(root_id, action_id, reader=current).status, "published")
