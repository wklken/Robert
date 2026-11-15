"""Exercise live coordinator ownership and mixed taskless model authority."""

from contextlib import closing
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch

from robert_agent.adapters.processes.source_model import SourceModelOutcome
from robert_agent.delivery.lock import task_publication_lock
from robert_agent.execution.application import source_models
from robert_agent.foundation.values import utc_now
from robert_agent.persistence import database, transactions
from robert_agent.persistence.stores import source_sessions
from robert_agent.work.application import source_model_observation
from tests.support.source_github_lifecycle import SourceGitHubLifecycleFixture

IDENTITY = {
    "pid": 643027,
    "process_group_id": 643027,
    "identity_source": "proc",
    "process_start_token": "mixed-synthesis-start",
    "command_fingerprint": "a" * 64,
}


class SourceGitHubModelTests(SourceGitHubLifecycleFixture):
    def _ready(self, reader, identifiers=(617,)):
        self._delivered_previous_pr()
        self.labels = ()
        self.tick("idle", ticket=self.ticket)
        self._admit_github_discussion([self._github_discussion(identifier=identifier) for identifier in identifiers])
        followup = self.comment("93")
        version = replace(followup.version, created=utc_now())
        self.comments.append(
            replace(
                followup,
                version=version,
                creator=replace(followup.creator, observation=version),
                own_body=replace(followup.own_body, observation=version),
            )
        )
        admitted = self.tick("admitted", github_discussion_runner=reader)
        self.tick("prepared", github_discussion_runner=reader)
        self.tick("dispatched", github_discussion_runner=reader)
        root = Path(self.configured["repos"][0]["repo_root"])
        self.reference = {
            "repo_id": "repo:org/alpha",
            "path": "README.md",
            "revision": "sha256:" + sha256((root / "README.md").read_bytes()).hexdigest(),
        }
        self.submit("local_result", evidence=[self.reference])
        self.tick("waiting", github_discussion_runner=reader)
        self.tick("audited", github_discussion_runner=reader)
        return admitted.round_id

    def test_running_synthesis_observes_deletion_before_group_exit_in_same_tick(self):
        _, original = self._current_github_runner()
        round_id = self._ready(original)
        withdrawn = False
        launch = []

        def unlocked(endpoint):
            if endpoint.endswith("/issues/comments/617"):
                with closing(sqlite3.connect(self.db, timeout=0)) as conn, conn:
                    conn.execute("BEGIN IMMEDIATE")

        reads, deleted = self._current_github_runner(item_status=404, during_read=unlocked)

        def reader(command, **kwargs):
            return (deleted if withdrawn else original)(command, **kwargs)

        def model(_profile, prompt, stop, started):
            nonlocal withdrawn
            launch.append(prompt)
            self.assertTrue(started(IDENTITY))
            self.assertIn("@bot please inspect", json.loads(prompt)["prompt"])
            with task_publication_lock(self.db, "source-coordinator:" + self.ticket.source_id) as locked:
                self.assertFalse(locked)
            withdrawn = True
            self.assertTrue(stop.is_set(), "running model did not re-read the deleted GitHub item")
            self.assertEqual(
                self.read(
                    "SELECT status,slot_reserved,output_json FROM source_model_runs WHERE round_id=?", (round_id,)
                ),
                [("running", 1, None)],
            )
            return SourceModelOutcome("stopped", exit_kind="group_exited", identity=IDENTITY)

        provider = self.provider
        outside = []

        def read_ticket(client, ticket, **options):
            if withdrawn:
                with closing(sqlite3.connect(self.db, timeout=0)) as conn, conn:
                    conn.execute("BEGIN IMMEDIATE")
                outside.append(ticket)
            return provider(client, ticket, **options)

        self.provider = read_ticket
        self.model = model
        with patch.object(source_models, "RECHECK_SECONDS", 0):
            self.tick("draining", github_discussion_runner=reader)
        self.assertTrue(outside)
        self.assertEqual(set(outside), {self.ticket})
        self.assertEqual(len(launch), 1)
        self.assertIn("repos/org/alpha/issues/comments/617", reads)
        self.assertEqual(
            self.read("SELECT reason FROM source_round_cancellations WHERE round_id=?", (round_id,)),
            [("github_item_absence_confirmed",)],
        )
        self.assertEqual(
            self.read(
                "SELECT status,slot_reserved,output_json,json_extract(exit_json,'$.kind') FROM source_model_runs "
                "WHERE round_id=?",
                (round_id,),
            ),
            [("interrupted", 0, None, "group_exited")],
        )

    def _answer(self, prompt):
        packet = json.loads(prompt)
        packet.pop("prompt")
        fingerprints = self.read(
            "SELECT e.event_fingerprint FROM source_round_requests r JOIN request_events l USING(request_id) "
            "JOIN source_events e USING(event_id) WHERE r.round_id=? ORDER BY e.event_fingerprint",
            (packet["round_id"],),
        )
        return json.dumps(
            {
                **packet,
                "kind": "answer",
                "answer": "The repository documents the behavior.",
                "references": [self.reference],
                "handled_event_fingerprints": [fingerprint for (fingerprint,) in fingerprints],
            }
        ).encode()

    def _run_state(self, round_id):
        return self.read(
            "SELECT status,slot_reserved,output_json,json_extract(exit_json,'$.kind') FROM source_model_runs "
            "WHERE round_id=?",
            (round_id,),
        )

    def test_periodic_fresh_positive_edit_preserves_packet_and_records_local_model_output(self):
        reads, original = self._current_github_runner()
        round_id = self._ready(original)
        edited = False
        edits, newer = self._current_github_runner(body="@bot Revised after launch", edited="2026-11-14T20:03:00Z")
        requests = self.read("SELECT content_version FROM ingress_requests WHERE provider='github'")
        captured = []
        before = len(reads)
        calls = self.factory_calls

        def reader(command, **kwargs):
            return (newer if edited else original)(command, **kwargs)

        def model(_profile, prompt, stop, started):
            nonlocal edited
            self.assertTrue(started(IDENTITY))
            captured.append(prompt)
            edited = True
            self.assertFalse(stop.is_set())
            return SourceModelOutcome("read", self._answer(prompt), "group_exited", IDENTITY)

        self.model = model
        with patch.object(source_models, "RECHECK_SECONDS", 0):
            self.tick("stage_recorded", github_discussion_runner=reader)
        [[packet]] = self.read("SELECT input_packet_json FROM source_model_runs WHERE round_id=?", (round_id,))
        self.assertEqual(json.loads(packet)["payload"], captured[0])
        self.assertIn("@bot please inspect", captured[0])
        self.assertNotIn("Revised after launch", captured[0])
        [[status, slots, output, exit_kind]] = self._run_state(round_id)
        self.assertEqual((status, slots, exit_kind), ("succeeded", 0, "group_exited"))
        self.assertEqual(json.loads(output)["kind"], "answer")
        self.assertNotEqual(self.read("SELECT content_version FROM ingress_requests WHERE provider='github'"), requests)
        self.assertEqual(reads[before:].count("repos/org/alpha/issues/comments/617"), 3)
        self.assertEqual(edits.count("repos/org/alpha/issues/comments/617"), 2)
        self.assertEqual(self.factory_calls - calls, 5)
        self.assertEqual(self.read("SELECT count(*) FROM source_results WHERE round_id=?", (round_id,)), [(0,)])
        self.assertEqual(
            self.read(
                "SELECT DISTINCT e.state FROM source_round_requests r JOIN request_events e USING(request_id) "
                "WHERE r.round_id=?",
                (round_id,),
            ),
            [("pending",)],
        )
        self.tick("publication_pending", github_discussion_runner=reader)

    def test_on_started_keeps_real_identity_even_when_fresh_current_mention_is_gone(self):
        _, original = self._current_github_runner()
        round_id = self._ready(original)
        _, newer = self._current_github_runner(body="Withdrawn before startup")
        edited = False

        def reader(command, **kwargs):
            return (newer if edited else original)(command, **kwargs)

        def model(_profile, prompt, _stop, started):
            nonlocal edited
            edited = True
            self.assertFalse(started(IDENTITY))
            [[dispatch, slot]] = self.read(
                "SELECT dispatch_json,slot_reserved FROM source_model_runs WHERE round_id=?", (round_id,)
            )
            self.assertEqual((json.loads(dispatch), slot), (IDENTITY, 1))
            return SourceModelOutcome("read", self._answer(prompt), "group_exited", IDENTITY)

        self.model = model
        self.tick("draining", github_discussion_runner=reader)
        self.assertEqual(self._run_state(round_id), [("interrupted", 0, None, "group_exited")])
        self.assertEqual(
            self.read("SELECT reason FROM source_round_cancellations WHERE round_id=?", (round_id,)),
            [("github_current_mention_removed",)],
        )

    def test_final_only_revocation_discards_otherwise_valid_output_but_records_group_exit(self):
        _, original = self._current_github_runner()
        round_id = self._ready(original)
        changed = False
        _, newer = self._current_github_runner(actor="outsider")

        def reader(command, **kwargs):
            return (newer if changed else original)(command, **kwargs)

        def model(_profile, prompt, _stop, started):
            nonlocal changed
            self.assertTrue(started(IDENTITY))
            changed = True
            return SourceModelOutcome("read", self._answer(prompt), "group_exited", IDENTITY)

        self.model = model
        self.tick("draining", github_discussion_runner=reader)
        self.assertEqual(self._run_state(round_id), [("interrupted", 0, None, "group_exited")])
        self.assertEqual(
            self.read("SELECT reason FROM source_round_cancellations WHERE round_id=?", (round_id,)),
            [("github_current_actor_untrusted",)],
        )

    def test_model_final_unavailable_read_interrupts_without_fabricating_revocation(self):
        _, original = self._current_github_runner()
        round_id = self._ready(original)
        changed = False
        _, unavailable = self._current_github_runner(item_status=503)

        def reader(command, **kwargs):
            return (unavailable if changed else original)(command, **kwargs)

        def model(_profile, prompt, _stop, started):
            nonlocal changed
            self.assertTrue(started(IDENTITY))
            changed = True
            return SourceModelOutcome("read", self._answer(prompt), "group_exited", IDENTITY)

        self.model = model
        self.tick("held", github_discussion_runner=reader)
        self.assertEqual(self._run_state(round_id), [("interrupted", 0, None, "group_exited")])
        self.assertFalse(self.read("SELECT 1 FROM source_round_cancellations WHERE round_id=?", (round_id,)))
        self.assertEqual(
            self.read("SELECT state FROM request_events WHERE event_id='event:comment:617'"), [("pending",)]
        )

    def test_same_launch_started_proof_cannot_substitute_for_final_observation(self):
        _, original = self._current_github_runner()
        round_id = self._ready(original)
        self.model = lambda _profile, prompt, stop, started: (
            SourceModelOutcome("read", self._answer(prompt), "group_exited", IDENTITY)
            if started(IDENTITY)
            else self.fail("model should start")
        )
        observe = source_model_observation.MixedModelObserver.observe
        started_proof = []

        def replay(owner, target, **kwargs):
            if target.check == "final":
                self.assertNotEqual(started_proof[0].proof.target.check_id, target.check_id)
                return started_proof[0]
            checked = observe(owner, target, **kwargs)
            if target.check == "started":
                self.assertIsNotNone(checked)
                started_proof.append(checked)
            return checked

        with patch.object(source_model_observation.MixedModelObserver, "observe", new=replay):
            self.tick("held", github_discussion_runner=original)
        self.assertEqual(self._run_state(round_id), [("interrupted", 0, None, "group_exited")])

    def test_periodic_checks_every_batch_and_durable_cancellation_keeps_unknown_group_reserved(self):
        identifiers = tuple(range(617, 623))
        originals = {identifier: self._current_github_runner(identifier=identifier)[1] for identifier in identifiers}
        alternatives = {}
        commands = []
        selected = identifiers[0]
        changed = False

        def reader(command, **kwargs):
            nonlocal selected
            endpoint = command[7]
            if "/issues/comments/" in endpoint:
                selected = int(endpoint.rsplit("/", 1)[1])
                if changed:
                    commands.append(selected)
            return (alternatives.get(selected, originals[selected]) if changed else originals[selected])(
                command, **kwargs
            )

        round_id = self._ready(reader, identifiers)
        ordered = [
            int(event_id.rsplit(":", 1)[1])
            for (event_id,) in self.read(
                "SELECT e.event_id FROM source_round_requests r JOIN request_events e USING(request_id) "
                "JOIN ingress_requests i USING(request_id) WHERE r.round_id=? AND i.provider='github' ORDER BY r.request_id",
                (round_id,),
            )
        ]
        first, last = ordered[0], ordered[-1]
        alternatives.update(
            {
                first: self._current_github_runner(identifier=first, item_status=404)[1],
                last: self._current_github_runner(identifier=last, item_status=503)[1],
            }
        )

        def model(_profile, _prompt, stop, started):
            nonlocal changed
            self.assertTrue(started(IDENTITY))
            changed = True
            self.assertTrue(stop.is_set())
            return SourceModelOutcome("process_attention")

        self.model = model
        with patch.object(source_models, "RECHECK_SECONDS", 0):
            self.assertEqual(
                self.tick("attention", github_discussion_runner=reader).reason, "source_model_exit_unconfirmed"
            )
        self.assertEqual(commands, ordered)
        self.assertEqual(self._run_state(round_id), [("running", 1, None, None)])
        self.assertEqual(
            self.read("SELECT state FROM request_events WHERE event_id=?", (f"event:comment:{first}",)), [("revoked",)]
        )
        self.assertEqual(
            self.read("SELECT state FROM request_events WHERE event_id=?", (f"event:comment:{last}",)), [("pending",)]
        )
        self.tick("draining", terminate_model=lambda observed: {"status": "terminated"} if observed == IDENTITY else {})
        self.assertEqual(self._run_state(round_id), [("interrupted", 0, None, "group_exited")])

    def test_exact_starting_grant_allows_stop_poll_before_identity_is_captured(self):
        reads, original = self._current_github_runner()
        round_id = self._ready(original)
        before = len(reads)

        def model(_profile, prompt, stop, started):
            self.assertFalse(stop.is_set(), "the transport polls stop before Popen and during identity capture")
            self.assertEqual(
                self.read(
                    "SELECT status,slot_reserved,dispatch_json FROM source_model_runs WHERE round_id=?", (round_id,)
                ),
                [("starting", 1, None)],
            )
            self.assertTrue(started(IDENTITY))
            return SourceModelOutcome("read", self._answer(prompt), "group_exited", IDENTITY)

        self.model = model
        with patch.object(source_models, "RECHECK_SECONDS", 0):
            self.tick("stage_recorded", github_discussion_runner=original)
        self.assertEqual(reads[before:].count("repos/org/alpha/issues/comments/617"), 5)

    def test_final_writer_refuses_fresh_proof_if_an_operation_is_overwritten(self):
        _, original = self._current_github_runner()
        round_id = self._ready(original)
        observe = source_model_observation.MixedModelObserver.observe

        def model(_profile, prompt, _stop, started):
            self.assertTrue(started(IDENTITY))
            return SourceModelOutcome("read", self._answer(prompt), "group_exited", IDENTITY)

        def supersede(owner, target, **options):
            checked = observe(owner, target, **options)
            if target.check == "final":
                self.assertIsNotNone(checked)
                with closing(sqlite3.connect(self.db, timeout=0)) as conn, conn:
                    conn.execute("UPDATE source_github_observations SET operation_id='another-operation'")
            return checked

        self.model = model
        with patch.object(source_model_observation.MixedModelObserver, "observe", new=supersede):
            self.tick("held", github_discussion_runner=original)
        self.assertEqual(self._run_state(round_id), [("interrupted", 0, None, "group_exited")])

    def test_final_writer_fences_old_owner_and_new_owner_can_record_exact_exit(self):
        _, original = self._current_github_runner()
        round_id = self._ready(original)
        observe = source_model_observation.MixedModelObserver.observe
        replacement = []

        def model(_profile, prompt, _stop, started):
            self.assertTrue(started(IDENTITY))
            return SourceModelOutcome("read", self._answer(prompt), "group_exited", IDENTITY)

        def supersede(owner, target, **options):
            checked = observe(owner, target, **options)
            if target.check == "final":
                self.assertIsNotNone(checked)
                with closing(sqlite3.connect(self.db, timeout=0)) as conn, conn:
                    conn.execute("UPDATE source_sessions SET lease_until='2000-01-01T00:00:00+00:00'")
                with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
                    transactions.begin_immediate(conn)
                    claim = source_sessions.claim(
                        conn, source_id=self.ticket.source_id, owner="recovery", lease_seconds=120, now=utc_now()
                    )
                    self.assertIsNotNone(claim)
                    replacement.append(claim)
            return checked

        self.model = model
        with patch.object(source_model_observation.MixedModelObserver, "observe", new=supersede):
            outcome = self.tick("attention", github_discussion_runner=original)
        self.assertEqual(outcome.reason, "source_claim_changed")
        self.assertEqual(self._run_state(round_id), [("running", 1, None, None)])
        repaired = source_models.recover(
            self.db, replacement[0], run_id=outcome.identity, inspect_exit=lambda observed: observed == IDENTITY
        )
        self.assertEqual(repaired.status, "interrupted")
        self.assertEqual(self._run_state(round_id), [("interrupted", 0, None, "group_exited")])
