"""Exercise public GitHub code publication for accepted mixed source roots."""

from contextlib import closing
from dataclasses import replace
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

from robert_agent.delivery.application import effects, source_delivery_observation
from robert_agent.work.application import source_github_observation
from tests.support.source_github_lifecycle import SourceGitHubLifecycleFixture


class SourceGitHubCodeAuthorityTests(SourceGitHubLifecycleFixture):
    def mutation_count(self, calls):
        return sum(command[:2] == ("git", "push") or command[:3] == ("gh", "pr", "create") for command in calls)

    def discussion(self, *, parent_edit=None, **options):
        def parent(row):
            row["head"]["sha"] = self.initial_pr_head
            if parent_edit is not None:
                parent_edit(row)

        return self._current_github_runner(parent_edit=parent, **options)

    def test_actual_mixed_code_push_confirms_one_effect_and_leaves_both_comments_pending(self):
        admitted, _prepared, accepted = self.accepted_mixed_development()
        calls, state, _, run = self.runner()
        seen = []
        tapd_reads = []

        def outside(endpoint):
            if "/issues/comments/" in endpoint:
                with closing(sqlite3.connect(self.db, timeout=0)) as conn, conn:
                    conn.execute("BEGIN IMMEDIATE")
                seen.append(bool(self.intent()))

        def observe(client, ticket, **options):
            with closing(sqlite3.connect(self.db, timeout=0)) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
            tapd_reads.append(ticket)
            return self.provider(client, ticket, **options)

        before = self.commitment(admitted)
        _, read = self.discussion(
            during_read=outside, body="@bot Latest authorized text", edited="2026-11-14T20:03:00Z"
        )
        outcome = self.publish("push_existing_pr", run, github_discussion_runner=read, observation_provider=observe)
        self.assertEqual(seen, [False, True])
        self.assertEqual(tapd_reads, [self.ticket, self.ticket])
        self.assertEqual(self.commitment(admitted), before)
        self.assertEqual(outcome.status, "published", outcome)
        self.assertEqual(self.mutation_count(calls), 1)
        self.assertEqual(state["sha"], self.head)
        self.assertEqual(
            self.read(
                "SELECT a.provider,a.action_type,a.publish_status FROM source_required_actions r "
                "JOIN github_actions a USING(action_id) WHERE r.source_result_id=? ORDER BY r.position",
                (accepted.identity,),
            ),
            [
                ("github", "push_existing_pr", "published"),
                ("tapd", "comment", "not_published"),
                ("github", "source_feedback", "not_published"),
            ],
        )
        self.assertEqual(
            self.read("SELECT status FROM source_rounds WHERE round_id=?", (admitted.round_id,)), [("active",)]
        )

    def test_final_discussion_observation_cannot_ignore_newer_parent_base(self):
        self.accepted_mixed_development()

        def parent(row):
            if self.read(
                "SELECT 1 FROM publication_intents WHERE action_id=? AND status='executing'",
                (self.action_ids["push_existing_pr"],),
            ):
                row["base"]["sha"] = "5" * 40

        _, read = self.discussion(parent_edit=parent)
        calls, _, _, run = self.runner()
        outcome = self.publish("push_existing_pr", run, github_discussion_runner=read)
        self.assertEqual(outcome.status, "unknown", outcome)
        self.assertEqual(self.mutation_count(calls), 0)

    def intent(self):
        return self.read(
            "SELECT publication_id,status FROM publication_intents WHERE action_id=?",
            (self.action_ids["push_existing_pr"],),
        )

    def commitment(self, admitted):
        return (
            self.read(
                "SELECT input_snapshot_json,input_sha256 FROM source_rounds WHERE round_id=?", (admitted.round_id,)
            ),
            self.read(
                "SELECT evidence_json,payload_json FROM source_results WHERE source_result_id=?", (self.root_id,)
            ),
            self.read(
                "SELECT i.phase,i.authorization_json,i.content_snapshot_json,i.content_version,e.state,e.source_result_id "
                "FROM source_round_requests r JOIN ingress_requests i USING(request_id) JOIN request_events e USING(request_id) "
                "WHERE r.round_id=? ORDER BY r.request_id",
                (admitted.round_id,),
            ),
        )

    def _assert_revoked(self, expected, *, final, **options):
        admitted, _, _ = self.accepted_mixed_development()
        before = self.commitment(admitted)
        _, current = self.discussion()
        _, revoked = self.discussion(**options)
        calls, _, _, run = self.runner()

        def read(command, **kwargs):
            return (revoked if bool(self.intent()) == final else current)(command, **kwargs)

        outcome = self.publish("push_existing_pr", run, github_discussion_runner=read)
        self.assertEqual(outcome.status, "unknown" if final else "deferred", outcome)
        self.assertEqual(self.mutation_count(calls), 0)
        self.assertEqual([state for _, state in self.intent()], ["attention"] if final else [])
        self.assertEqual(
            self.read("SELECT reason FROM source_round_cancellations WHERE round_id=?", (admitted.round_id,)),
            [(expected,)],
        )
        self.assertEqual(self.commitment(admitted), before)
        if final:
            retry = self.publish("push_existing_pr", run, github_discussion_runner=current)
            self.assertEqual(retry.status, "unknown", retry)
            self.assertEqual(self.mutation_count(calls), 0)

    def test_prestart_removed_mention_cancels_before_intent(self):
        self._assert_revoked("github_current_mention_removed", final=False, body="Removed request")

    def test_deleted_trigger_seen_only_after_intent_cancels_and_keeps_unknown(self):
        self._assert_revoked("github_item_absence_confirmed", final=True, item_status=404)

    def test_untrusted_editor_seen_only_after_intent_cancels(self):
        self._assert_revoked("github_current_actor_untrusted", final=True, actor="outsider")

    def test_started_final_effect_rejects_genuine_prestart_proof(self):
        self.accepted_mixed_development()
        _, read = self.discussion()
        calls, _, _, run = self.runner()
        observe = source_delivery_observation.observe
        saved = []

        def replay(*args, **kwargs):
            if isinstance(kwargs["code_effect"], source_github_observation.CodeStarted):
                self.assertTrue(saved)
                return saved[0]
            proof = observe(*args, **kwargs)
            self.assertIsNotNone(proof)
            saved.append(proof)
            return proof

        with patch.object(source_delivery_observation, "observe", side_effect=replay):
            outcome = self.publish("push_existing_pr", run, github_discussion_runner=read)
        self.assertEqual(outcome.status, "unknown", outcome)
        self.assertEqual(self.mutation_count(calls), 0)
        self.assertEqual([state for _, state in self.intent()], ["attention"])

    def test_final_guard_rejects_missing_mixed_proof_after_a_valid_first_writer(self):
        self.accepted_mixed_development()
        _, read = self.discussion()
        calls, _, _, run = self.runner()
        effect = effects.publish_native_github

        def stripped(*args, native_scope, **kwargs):
            self.assertIsNotNone(native_scope.mixed_observation)
            return effect(*args, native_scope=replace(native_scope, mixed_observation=None), **kwargs)

        with patch.object(effects, "publish_native_github", side_effect=stripped):
            outcome = self.publish("push_existing_pr", run, github_discussion_runner=read)
        self.assertEqual(outcome.status, "unknown", outcome)
        self.assertEqual(self.mutation_count(calls), 0)

    def _assert_stale_final_proof(self, mutation):
        admitted, _, _ = self.accepted_mixed_development()
        before = self.commitment(admitted)
        _, read = self.discussion()
        calls, _, _, run = self.runner()
        observe = source_delivery_observation.observe
        observed = []

        def changed(*args, **kwargs):
            proof = observe(*args, **kwargs)
            if isinstance(kwargs["code_effect"], source_github_observation.CodeStarted):
                self.assertIsNotNone(proof)
                with closing(sqlite3.connect(self.db, timeout=0)) as conn, conn:
                    mutation(conn, proof)
                observed.append(proof)
            return proof

        with patch.object(source_delivery_observation, "observe", side_effect=changed):
            outcome = self.publish("push_existing_pr", run, github_discussion_runner=read)
        self.assertEqual(outcome.status, "unknown", outcome)
        self.assertEqual(len(observed), 1)
        self.assertEqual(self.mutation_count(calls), 0)
        self.assertEqual(self.commitment(admitted), before)
        self.assertFalse(self.read("SELECT 1 FROM source_round_cancellations WHERE round_id=?", (admitted.round_id,)))

    def test_final_writer_refuses_changed_durable_source_fence(self):
        self._assert_stale_final_proof(
            lambda conn, proof: conn.execute(
                "UPDATE source_sessions SET token=token+1 WHERE source_id=?", (self.ticket.source_id,)
            )
        )

    def test_final_writer_refuses_changed_tapd_revision(self):
        self._assert_stale_final_proof(
            lambda conn, proof: conn.execute(
                "UPDATE sources SET metadata_json=json_set(metadata_json,'$.revision','superseded') WHERE source_id=?",
                (self.ticket.source_id,),
            )
        )

    def test_final_writer_refuses_changed_candidate(self):
        self._assert_stale_final_proof(
            lambda conn, proof: conn.execute(
                "UPDATE worker_results SET metadata_json=json_set(metadata_json,'$.changed','value') "
                "WHERE result_id=(SELECT worker_result_id FROM source_results WHERE source_result_id=?)",
                (self.root_id,),
            )
        )

    def test_final_writer_refuses_changed_started_publication(self):
        def changed(conn, proof):
            original = proof.github.scope.code_effect.publication_id
            conn.execute(
                "UPDATE publication_intents SET publication_id='different-intent' WHERE publication_id=?", (original,)
            )
            conn.execute(
                "UPDATE github_publication_preparations SET publication_id='different-intent' WHERE publication_id=?",
                (original,),
            )

        self._assert_stale_final_proof(changed)

    def test_final_writer_refuses_changed_required_action_target(self):
        self._assert_stale_final_proof(
            lambda conn, proof: conn.execute(
                "UPDATE github_actions SET target_json='{}' WHERE action_id=?",
                (proof.github.scope.action_id,),
            )
        )

    def test_final_writer_blocks_foreign_unknown_publication(self):
        self._assert_stale_final_proof(
            lambda conn, proof: conn.execute(
                "UPDATE publication_intents SET status='attention' WHERE action_id=("
                "SELECT a.action_id FROM source_required_actions r JOIN github_actions a USING(action_id) "
                "WHERE r.source_result_id=? AND a.action_type='push_existing_pr')",
                (self.previous_root_id,),
            )
        )

    def test_final_writer_blocks_unsettled_historical_process(self):
        self._assert_stale_final_proof(
            lambda conn, proof: conn.execute(
                "UPDATE attempts SET status='running' WHERE task_id=("
                "SELECT a.task_id FROM source_required_actions r JOIN github_actions a USING(action_id) "
                "WHERE r.source_result_id=? AND a.action_type='open_pr')",
                (self.previous_root_id,),
            )
        )

    def test_started_timeout_then_readback_after_withdrawal_never_pushes_again(self):
        admitted, _, _ = self.accepted_mixed_development()
        before = self.commitment(admitted)
        _, read = self.discussion()
        calls, _, _, transport = self.runner(push_timeout=True)
        hide_readback = True

        def run(command, **kwargs):
            if (
                hide_readback
                and self.mutation_count(calls)
                and command[:4] == ["gh", "api", "--hostname", "github.com"]
                and "/git/matching-refs/" in command[4]
            ):
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            return transport(command, **kwargs)

        first = self.publish("push_existing_pr", run, github_discussion_runner=read)
        self.assertEqual(first.status, "unknown", first)
        self.assertEqual(self.mutation_count(calls), 1)
        [[intent_id, status]] = self.intent()
        self.assertEqual(status, "attention")
        hide_readback = False
        raw_calls, withdrawn = self.discussion(body="This request is withdrawn")
        second = self.publish("push_existing_pr", run, github_discussion_runner=withdrawn)
        self.assertEqual(second.status, "published", second)
        self.assertEqual(second.receipt.publication_id, intent_id)
        self.assertEqual(raw_calls, [])
        self.assertEqual(self.mutation_count(calls), 1)
        self.assertEqual(self.commitment(admitted), before)
