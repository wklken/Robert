"""Observe active native rounds using canonical current GitHub discussion evidence."""

from contextlib import closing
from dataclasses import replace
from hashlib import sha256
import json
import sqlite3

from robert_agent.foundation.values import utc_now
from robert_agent.persistence import database, transactions
from robert_agent.persistence.queries import source_sessions as rounds
from robert_agent.persistence.rounds import source_model
from robert_agent.persistence.stores import source_sessions
from robert_agent.work.application import source_github_observation, tapd_intake

from tests.support.source_github_lifecycle import SourceGitHubLifecycleFixture


class SourceGitHubRoundLifecycleTests(SourceGitHubLifecycleFixture):
    def _admit_mixed(self):
        _claim, _revision, _owners = self._mixed_pending_fixture()
        _, run = self._current_github_runner()
        return self.tick("admitted", github_discussion_runner=run)

    def _fresh_tapd_comment(self, identifier):
        comment = self.comment(str(identifier))
        version = replace(comment.version, created=utc_now())
        updated = replace(
            comment,
            version=version,
            creator=replace(comment.creator, observation=version),
            own_body=replace(comment.own_body, observation=version),
        )
        self.comments.append(updated)
        return updated

    def _frozen(self, round_id):
        [[encoded]] = self.read("SELECT input_snapshot_json FROM source_rounds WHERE round_id=?", (round_id,))
        return json.loads(encoded)

    def test_unlaunched_edit_refreezes_exact_mixed_batch(self):
        admitted = self._admit_mixed()
        frozen = self._frozen(admitted.round_id)
        starts = len(self.started)
        later = self._fresh_tapd_comment(93)
        self._admit_github_discussion([self._github_discussion(identifier=618, body="@bot A later request.")])
        self.ticket_body = "Latest complete TAPD context."
        calls, edited = self._current_github_runner(body="@bot Please check the edited case.")
        refreshed = self.tick("readmitted", ticket=self.ticket, github_discussion_runner=edited)
        self.assertNotEqual(refreshed.round_id, admitted.round_id)
        updated = self._frozen(refreshed.round_id)
        self.assertEqual([r["request_id"] for r in updated["requests"]], [r["request_id"] for r in frozen["requests"]])
        [new] = [r for r in updated["requests"] if r["input"].get("provider") == "github"]
        [old] = [r for r in frozen["requests"] if r["input"].get("provider") == "github"]
        self.assertEqual(new["input"]["body"], "@bot Please check the edited case.")
        self.assertEqual(new["input"]["return_target"], old["input"]["return_target"])
        self.assertNotEqual(new["content_version"], old["content_version"])
        self.assertEqual(self._frozen(admitted.round_id), frozen)
        self.assertEqual(updated["ticket"]["body"], self.ticket_body)
        [future] = [row for row in updated["discussion"] if row["event_id"] == later.version.event_id]
        self.assertEqual(future["request_scope"], "future_request")
        self.assertNotIn("repos/org/alpha/issues/comments/618", calls)
        ledger = self.read(
            "SELECT i.phase,e.state FROM ingress_requests i JOIN request_events e USING(request_id) "
            "WHERE e.event_id='event:comment:618'"
        )
        self.assertEqual(ledger, [("held", "pending")])
        self.assertEqual(len(self.started), starts)

    def _record_model_launch(self, round_id):
        prompt = "Recorded mixed round prompt"
        binding = {
            "round_id": round_id,
            "run_id": "mixed-model",
            "input_version": self.read("SELECT input_version FROM source_rounds WHERE round_id=?", (round_id,))[0][0],
            "prompt_sha256": sha256(prompt.encode()).hexdigest(),
        }
        request_id = self._frozen(round_id)["requests"][0]["request_id"]
        packet = {"version": 1, "payload": json.dumps({**binding, "prompt": prompt})}
        identity = {
            "pid": 632015,
            "process_group_id": 632015,
            "process_start_token": "recorded-model",
            "command_fingerprint": "1" * 64,
            "identity_source": "ps",
        }
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            now = utc_now()
            claim = source_sessions.acquire_for_owner(
                conn, source_id=self.ticket.source_id, owner="test-coordinator", now=now, lease_seconds=120
            )
            self.assertIsNotNone(
                source_model.enqueue(
                    conn,
                    claim,
                    stage="synthesis",
                    request_id=request_id,
                    worker_id="mock-model",
                    input_packet=packet,
                    now=now,
                    **binding,
                )
            )
            self.assertTrue(source_model.reserve_slot(conn, claim, binding["run_id"], now=now))
            self.assertTrue(
                source_model.authorize_launch(conn, claim, binding["run_id"], launch_id="mixed-launch", now=now)
            )
            self.assertTrue(
                source_model.record_identity(
                    conn,
                    claim,
                    binding["run_id"],
                    launch_id="mixed-launch",
                    identity=identity,
                    now=now,
                )
            )
            self.assertEqual(rounds.round_launch_state(conn, round_id), "started")

    def test_started_round_preserves_prompt_and_detects_fresh_marker_revocation(self):
        self._delivered_previous_pr()
        self.labels = ()
        self.tick("idle", ticket=self.ticket)
        self._admit_github_discussion([self._github_discussion()])
        self._fresh_tapd_comment(81)
        _, original = self._current_github_runner()
        started = self.tick("admitted", github_discussion_runner=original)
        [[capability]] = self.read("SELECT capability FROM source_rounds WHERE round_id=?", (started.round_id,))
        self.assertEqual(capability, "readonly")
        self._record_model_launch(started.round_id)
        frozen = self._frozen(started.round_id)
        packet = self.read("SELECT input_packet_json,prompt_sha256 FROM source_model_runs WHERE run_id='mixed-model'")
        before = len(self.started)
        _, edited = self._current_github_runner(body="@bot Edited after the model launch")
        result = self.tick("held", github_discussion_runner=edited)
        self.assertEqual(result.reason, "github_mixed_model_unavailable")
        self.assertEqual(self._frozen(started.round_id), frozen)
        self.assertEqual(
            self.read("SELECT input_packet_json,prompt_sha256 FROM source_model_runs WHERE run_id='mixed-model'"),
            packet,
        )
        self.assertEqual(self.read("SELECT COUNT(*) FROM ingress_requests WHERE provider='github'"), [(1,)])

        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE tapd_coordination SET status='attention',reason='source_stage_failed' WHERE round_id=?",
                (started.round_id,),
            )
        calls, removed = self._current_github_runner(body="No longer requesting an investigation")
        failure = self.tick(
            "attention", github_discussion_runner=removed, terminate_model=lambda _: {"status": "attention"}
        )
        self.assertEqual(failure.reason, "source_model_exit_unconfirmed")
        self.assertIn("repos/org/alpha/issues/comments/617", calls)
        self.assertEqual(
            self.read("SELECT status,slot_reserved FROM source_model_runs WHERE run_id='mixed-model'"), [("running", 1)]
        )
        self.assertEqual(
            self.read("SELECT reason FROM source_round_cancellations WHERE round_id=?", (started.round_id,)),
            [("github_current_mention_removed",)],
        )
        self.assertEqual(
            self.read("SELECT state FROM request_events WHERE event_id='event:comment:617'"), [("revoked",)]
        )
        self.tick("draining", terminate_model=lambda _: {"status": "terminated"})
        self.tick("canceled")
        self.assertEqual(self._frozen(started.round_id), frozen)
        self.assertEqual(len(self.started), before)

    def _runner_many(self, identifiers, *, per_item=None):
        options = per_item or {}
        readers = {
            identifier: self._current_github_runner(identifier=identifier, **options.get(identifier, {}))[1]
            for identifier in identifiers
        }
        calls = []
        selected = identifiers[0]

        def run(command, **kwargs):
            nonlocal selected
            endpoint = command[7]
            calls.append(endpoint)
            if "/issues/comments/" in endpoint:
                selected = int(endpoint.rsplit("/", 1)[1])
            return readers[selected](command, **kwargs)

        return calls, run

    def test_every_active_member_is_checked_and_unknown_sibling_does_not_erase_exact_absence(self):
        self._delivered_previous_pr()
        identifiers = tuple(range(617, 623))
        self._admit_github_discussion([self._github_discussion(identifier=identifier) for identifier in identifiers])
        self._fresh_tapd_comment(85)
        _, initial = self._runner_many(identifiers)
        admitted = self.tick("admitted", github_discussion_runner=initial)
        ordered = [
            int(event_id.rsplit(":", 1)[1])
            for (event_id,) in self.read(
                "SELECT e.event_id FROM source_round_requests r JOIN request_events e USING(request_id) "
                "JOIN ingress_requests i USING(request_id) WHERE r.round_id=? AND i.provider='github' ORDER BY r.request_id",
                (admitted.round_id,),
            )
        ]
        self.assertEqual(len(ordered), 6)
        frozen = self._frozen(admitted.round_id)
        calls, partial = self._runner_many(identifiers, per_item={ordered[-1]: {"item_status": 503}})
        result = self.tick("held", github_discussion_runner=partial)
        self.assertEqual(result.reason, "github_observation_unavailable")
        self.assertEqual(
            [endpoint for endpoint in calls if "/issues/comments/" in endpoint],
            [f"repos/org/alpha/issues/comments/{identifier}" for identifier in ordered],
        )
        self.assertEqual(self._frozen(admitted.round_id), frozen)
        self.assertFalse(self.read("SELECT * FROM source_round_cancellations WHERE round_id=?", (admitted.round_id,)))
        _, missing = self._runner_many(
            identifiers, per_item={ordered[0]: {"item_status": 404}, ordered[-1]: {"item_status": 503}}
        )
        self.tick("canceled", github_discussion_runner=missing)
        self.assertEqual(
            self.read("SELECT state FROM request_events WHERE event_id=?", (f"event:comment:{ordered[0]}",)),
            [("revoked",)],
        )
        self.assertEqual(
            self.read("SELECT state FROM request_events WHERE event_id=?", (f"event:comment:{ordered[-1]}",)),
            [("pending",)],
        )
        self.assertEqual(
            self.read("SELECT reason FROM source_round_cancellations WHERE round_id=?", (admitted.round_id,)),
            [("github_item_absence_confirmed",)],
        )

    def test_active_transition_refuses_the_old_admission_operation(self):
        admitted = self._admit_mixed()
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            now = utc_now()
            claim = source_sessions.acquire_for_owner(
                conn, source_id=self.ticket.source_id, owner="test-coordinator", now=now, lease_seconds=120
            )
            active = rounds.active_round(conn, self.ticket.source_id)
            scope = source_github_observation.active_scope(conn, active)
            [[operation]] = self.read("SELECT operation_id FROM source_github_observations")
            revision = self.read(
                "SELECT json_extract(metadata_json,'$.revision') FROM sources WHERE source_id=?",
                (self.ticket.source_id,),
            )[0][0]
            self.assertIsNone(
                source_github_observation.current_batch(
                    conn,
                    self.configured,
                    claim,
                    self.ticket,
                    revision=revision,
                    now=now,
                    active=scope,
                    operation_id=operation,
                    request_ids=scope.request_ids,
                )
            )
            refusal = tapd_intake.refresh_mixed_round(
                conn,
                self.configured,
                self.ticket,
                claim,
                active=scope,
                revision=revision,
                now=now,
                operations=((operation, scope.request_ids),),
                replacement_id="invalid-replacement",
            )
            self.assertEqual((refusal.status, refusal.reason), ("held", "github_observation_scope_changed"))
        self.assertEqual(
            self.read("SELECT status FROM source_rounds WHERE round_id=?", (admitted.round_id,)), [("active",)]
        )

    def test_active_remote_reads_release_writer_and_reject_scope_drift(self):
        admitted = self._admit_mixed()
        frozen = self._frozen(admitted.round_id)
        [[version]] = self.read("SELECT input_version FROM source_rounds WHERE round_id=?", (admitted.round_id,))
        for drift in ("round", "revision", "claim"):
            with self.subTest(drift=drift):

                def supersede(endpoint):
                    if not endpoint.endswith("/pulls/17"):
                        return
                    with closing(sqlite3.connect(self.db, timeout=0)) as conn, conn:
                        if drift == "round":
                            conn.execute(
                                "UPDATE source_rounds SET input_version='changed' WHERE round_id=?",
                                (admitted.round_id,),
                            )
                        elif drift == "revision":
                            conn.execute(
                                "UPDATE sources SET metadata_json=json_set(metadata_json,'$.revision','changed') "
                                "WHERE source_id=?",
                                (self.ticket.source_id,),
                            )
                        else:
                            conn.execute(
                                "UPDATE source_sessions SET token=token+1 WHERE source_id=?", (self.ticket.source_id,)
                            )

                _, run = self._current_github_runner(body="@bot Edited", during_read=supersede)
                result = self.tick("held", github_discussion_runner=run)
                self.assertEqual(
                    result.reason, "source_claim_changed" if drift == "claim" else "github_observation_scope_changed"
                )
                self.assertEqual(self._frozen(admitted.round_id), frozen)
                self.assertFalse(
                    self.read("SELECT * FROM source_round_cancellations WHERE round_id=?", (admitted.round_id,))
                )
                with closing(sqlite3.connect(self.db)) as conn, conn:
                    conn.execute(
                        "UPDATE source_rounds SET input_version=? WHERE round_id=?", (version, admitted.round_id)
                    )

    def test_known_label_loss_cancels_started_github_round_despite_missing_ticket_body(self):
        self._delivered_previous_pr()
        self._admit_github_discussion([self._github_discussion()])
        _, current = self._current_github_runner()
        admitted = self.tick("admitted", github_discussion_runner=current)
        self._record_model_launch(admitted.round_id)
        original = self._frozen(admitted.round_id)
        self.ticket_body = None
        self.labels = None
        unknown = self.tick("held", github_discussion_runner=current)
        self.assertEqual(unknown.reason, "source_observation_unavailable")
        self.assertFalse(self.read("SELECT * FROM source_round_cancellations WHERE round_id=?", (admitted.round_id,)))
        self.labels = ()
        calls, unavailable = self._current_github_runner(item_status=503)
        stopped = self.tick(
            "attention", github_discussion_runner=unavailable, terminate_model=lambda _: {"status": "attention"}
        )
        self.assertEqual(stopped.reason, "source_model_exit_unconfirmed")
        self.assertFalse(calls)
        self.assertEqual(
            self.read("SELECT reason FROM source_round_cancellations WHERE round_id=?", (admitted.round_id,)),
            [("github_development_binding_paused",)],
        )
        self.assertEqual(
            self.read(
                "SELECT i.phase,e.state FROM ingress_requests i JOIN request_events e USING(request_id) WHERE i.provider='github'"
            ),
            [("paused", "pending")],
        )
        self.assertEqual(self._frozen(admitted.round_id), original)
        self.assertEqual(
            self.read("SELECT status,slot_reserved FROM source_model_runs WHERE run_id='mixed-model'"), [("running", 1)]
        )
        self.tick("draining", terminate_model=lambda _: {"status": "terminated"})
        self.tick("canceled")

    def test_only_later_github_timestamp_does_not_refreeze(self):
        admitted = self._admit_mixed()
        frozen = self._frozen(admitted.round_id)
        [[version]] = self.read("SELECT content_version FROM ingress_requests WHERE provider='github'")
        _, run = self._current_github_runner(edited="2026-11-14T20:02:01Z")
        before = len(self.started)
        self.tick("prepared", github_discussion_runner=run)
        self.assertEqual(len(self.started), before)
        self.assertEqual(self._frozen(admitted.round_id), frozen)
        self.assertEqual(
            self.read("SELECT content_version FROM ingress_requests WHERE provider='github'"), [(version,)]
        )
        self.assertEqual(
            self.read(
                "SELECT json_extract(content_snapshot_json,'$.item.updated_at') FROM ingress_requests WHERE provider='github'"
            ),
            [("2026-11-14T20:02:01Z",)],
        )

    def test_known_label_loss_pauses_active_github_development_without_revoking_ledger(self):
        self._delivered_previous_pr()
        self._admit_github_discussion([self._github_discussion()])
        _, run = self._current_github_runner()
        admitted = self.tick("admitted", github_discussion_runner=run)
        self.assertEqual(
            self.read("SELECT capability FROM source_rounds WHERE round_id=?", (admitted.round_id,)),
            [("development",)],
        )
        self.labels = ()
        _, unavailable = self._current_github_runner(item_status=503)
        self.tick("canceled", github_discussion_runner=unavailable)
        self.assertEqual(
            self.read("SELECT reason FROM source_round_cancellations WHERE round_id=?", (admitted.round_id,)),
            [("github_development_binding_paused",)],
        )
        self.assertEqual(
            self.read(
                "SELECT i.phase,e.state FROM ingress_requests i JOIN request_events e USING(request_id) WHERE i.provider='github'"
            ),
            [("paused", "pending")],
        )
