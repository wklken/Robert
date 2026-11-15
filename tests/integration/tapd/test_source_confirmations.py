"""Exercise controller confirmations through actual TAPD intake and publication."""

from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch
import json
import sqlite3

from robert_agent.adapters.tapd.client import TapdError
from robert_agent.adapters.tapd.publication import TapdPublisherIdentity
from robert_agent.delivery.application import source_delivery_resume
from robert_agent.delivery.application.source_publication import publish_tapd_action
from robert_agent.foundation.values import utc_now
from robert_agent.persistence import database, transactions
from robert_agent.persistence.queries import source_confirmation as read
from robert_agent.persistence.stores import source_sessions
from robert_agent.work import tapd_authority as facts
from robert_agent.work.application import messaging, source_observation, tapd_intake
from tests.support.source_cycle import SourceCycleFixture


class SourceConfirmationTests(SourceCycleFixture):
    def setup_paused(self, *, restored=True, pause=True):
        self.ticket = facts.TicketIdentity("tapd.confirmation.example", "0123", "bug", "713")
        self.config(two=True, development=True)
        self.project = self.configured["tapd"]["projects"][0]
        self.project["rules"].append(
            {**self.project["rules"][0], "id": "alternate", "label": "robot-b", "repo": "org/beta"}
        )
        self.labels = ("robot",)
        self.comments = []
        self.explicit_absences = []
        self.writes = []
        self.first_unknown = False
        initial = self.observe()
        self.assertEqual(len(initial.request_ids), 1)
        self.original_request = initial.request_ids[0]
        if not pause:
            return
        self.labels = ()
        self.observe()
        self.labels = ("robot",) if restored else ("robot-b",)
        self.observe()

    def provider(self, client, ticket, **options):
        observed = super().provider(client, ticket, **options)
        return replace(
            observed,
            facts=replace(observed.facts, labels=self.labels, owners=getattr(self, "assignees", observed.facts.owners)),
            absences=tuple(self.explicit_absences),
        )

    def observe(self):
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            snapshot = self.provider(None, self.ticket)
            observed = tapd_intake.observe_ticket(
                conn,
                self.configured["tapd"],
                snapshot,
                source_observation.publication_facts(conn, self.ticket),
                now=utc_now(),
            )
        self.revision = observed.revision
        return observed

    def question(self):
        with closing(database.connect(self.db)) as conn:
            return read.current(conn, self.ticket.source_id)

    def observe_comment(
        self, external_id, body, *, entry="bug", principal="requester", proof=True, created=None, modified=None
    ):
        timestamp = created or utc_now()
        version = facts.CommentVersion(
            self.ticket,
            entry,
            str(external_id),
            timestamp,
            modified,
            sha256(body.encode()).hexdigest(),
        )
        observation = facts.CommentObservation(
            version,
            body,
            "requester",
            facts.CreatorEvidence(version, principal, "creator") if principal is not None else None,
            facts.OwnBodyEvidence(version, principal, "own-body") if proof else None,
        )
        self.comments = [old for old in self.comments if old.version.event_id != version.event_id] + [observation]
        return observation

    def offer(self):
        pending = self.tick("publication_pending", ticket=self.ticket)
        question = self.question()
        self.assertIsNotNone(question)
        self.assertEqual(question["source_result_id"], pending.identity)
        return question

    def send_question(self, question):
        test = self

        class Client:
            def __init__(self, **_):
                pass

            def add_comment(self, workspace_id, kind, external_id, body, *, stop):
                test.assertEqual((workspace_id, kind, external_id), test.ticket.key[1:])
                test.writes.append(body)
                identifier = str(100 + len(test.writes))
                test.observe_comment(identifier, body, principal="authenticated-tapd-service")
                if test.first_unknown:
                    test.first_unknown = False
                    raise TapdError("timeout", effect_unknown=True)
                return identifier

        def identity(_client, ticket, **_):
            return TapdPublisherIdentity(
                ticket.host, ticket.workspace_id, "authenticated-tapd-service", "scope-proof", utc_now()
            )

        return publish_tapd_action(
            self.db,
            self.configured,
            source_result_id=question["source_result_id"],
            action_id=question["action_id"],
            owner="test-coordinator",
            client_factory=Client,
            observation_provider=self.provider,
            identity_provider=identity,
        )

    def command(self, question, *, option=None, bot="test-helper"):
        scope = json.loads(question["scope_json"])
        return f"@{bot} ROBERT-CHOICE {question['question_id']} v{question['version']} {option or scope['option']}"

    def receipt(self, question, *, consumer=None, token_delta=0, option=None):
        scope = json.loads(question["scope_json"])
        args = {
            "revision": self.revision,
            "question_id": question["question_id"],
            "version": question["version"],
            "option": option or scope["option"],
            "scope_digest": question["scope_digest"],
        }
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            if consumer is None:
                return tapd_intake.current_confirmation(conn, self.configured["tapd"], self.ticket, **args)
            [[owner, token]] = self.read(
                "SELECT owner,token FROM source_sessions WHERE source_id=?", (self.ticket.source_id,)
            )
            claim = source_sessions.SourceClaim(self.ticket.source_id, owner, token + token_delta)
            return tapd_intake.claim_confirmation(
                conn, self.configured["tapd"], self.ticket, claim, consumer_id=consumer, now=utc_now(), **args
            )

    def test_same_rule_published_choice_resumes_original_pending_request_through_coordinator(self):
        self.setup_paused()
        original = self.read(
            "SELECT authorization_json,created_at FROM ingress_requests WHERE request_id=?", (self.original_request,)
        )
        question = self.offer()
        self.assertEqual(self.send_question(question).status, "published")
        self.assertEqual(self.tick("held", ticket=self.ticket).reason, "waiting_for_confirmation")
        answer = self.observe_comment(1, self.command(question))
        resumed = self.tick("admitted", ticket=self.ticket)
        self.assertEqual(
            self.read("SELECT request_id FROM source_round_requests WHERE round_id=?", (resumed.round_id,)),
            [(self.original_request,)],
        )
        self.assertEqual(
            self.read(
                "SELECT authorization_json,created_at FROM ingress_requests WHERE request_id=?",
                (self.original_request,),
            ),
            original,
        )
        self.assertEqual(self.read("SELECT state,source_result_id FROM request_events"), [("pending", None)])
        self.assertEqual(self.read("SELECT event_id FROM source_confirmation_answers"), [(answer.version.event_id,)])

    def test_resumed_receipt_survives_identical_observation_and_worker_result(self):
        self.setup_paused()
        question = self.offer()
        self.assertEqual(self.send_question(question).status, "published")
        self.observe_comment(4, self.command(question))
        with patch.object(tapd_intake, "observe_ticket", wraps=tapd_intake.observe_ticket) as observed:
            resumed = self.tick("admitted", ticket=self.ticket)
            captured = observed.call_args.args[2]
            publication = observed.call_args.args[3]
        [[metadata]] = self.read("SELECT metadata_json FROM sources WHERE source_id=?", (self.ticket.source_id,))
        saved = json.loads(metadata)
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            repeated = tapd_intake.observe_ticket(conn, self.configured["tapd"], captured, publication, now=utc_now())
        self.assertEqual(repeated.revision, saved["revision"])
        [[transition, receipt, request_ids]] = self.read(
            "SELECT transition_id,receipt_json,request_ids_json FROM source_resume_transitions"
        )
        self.assertEqual(saved["binding"]["resume_transition_id"], transition)
        self.assertEqual(json.loads(receipt)["consumer_id"], transition)
        self.assertEqual(json.loads(request_ids), [self.original_request])
        self.assertIsNone(self.question())
        self.assertEqual(self.read("SELECT revoked_at FROM source_confirmation_answers"), [(None,)])
        root = self._accept_answer()
        self.assertEqual(root.round_id, resumed.round_id)
        self.assertEqual(self.read("SELECT COUNT(*) FROM source_resume_transitions"), [(1,)])
        self.assertEqual(self.read("SELECT state,source_result_id FROM request_events"), [("consumed", root.identity)])
        [[action]] = self.read(
            "SELECT action_id FROM source_required_actions WHERE source_result_id=?", (root.identity,)
        )
        self.assertEqual(
            self.send_question({"source_result_id": root.identity, "action_id": action}).status, "published"
        )
        self.assertEqual(self.read("SELECT status FROM source_rounds"), [("execution_finished",)])

    def test_unknown_answer_proof_holds_then_confirmed_deletion_cancels_running_round(self):
        self.setup_paused()
        question = self.offer()
        self.assertEqual(self.send_question(question).status, "published")
        answer = self.observe_comment(4, self.command(question))
        original = self.tick("admitted", ticket=self.ticket)
        self.tick("prepared")
        self.tick("dispatched")
        saved_prompt = self.started[-1]["prompt_path"].read_bytes()
        self.observe_comment(4, self.command(question), principal=None, proof=False, created=answer.version.created)
        held = self.tick("held")
        self.assertEqual(held.reason, "source_authority_unavailable")
        self.assertFalse(self.read("SELECT * FROM source_round_cancellations"))
        self.assertEqual(self.read("SELECT revoked_at FROM source_confirmation_answers"), [(None,)])
        self.assertEqual(self.read("SELECT phase FROM ingress_requests"), [("held",)])

        self.comments = [comment for comment in self.comments if comment.version.event_id != answer.version.event_id]
        self.explicit_absences = [facts.CommentAbsence(self.ticket, "bug", "4", "authoritative-absence")]
        self.tick("draining", terminate_worker=lambda _metadata: {"status": "terminated"})
        self.tick("canceled")
        self.assertEqual(self.read("SELECT round_id FROM source_round_cancellations"), [(original.round_id,)])
        self.assertIsNotNone(self.read("SELECT revoked_at FROM source_confirmation_answers")[0][0])
        self.assertEqual(self.read("SELECT state,source_result_id FROM request_events"), [("pending", None)])
        self.assertEqual(self.started[-1]["prompt_path"].read_bytes(), saved_prompt)
        replacement = self.offer()
        self.assertEqual(replacement["version"], question["version"] + 1)
        self.assertEqual(self.send_question(replacement).status, "published")
        self.observe_comment(5, self.command(question))  # Old published token stays unusable.
        self.assertEqual(self.tick("held").reason, "waiting_for_confirmation")
        self.observe_comment(6, self.command(replacement))
        resumed = self.tick("admitted")
        self.assertNotEqual(resumed.round_id, original.round_id)
        self.assertEqual(self.read("SELECT COUNT(*) FROM source_resume_transitions"), [(2,)])
        self.assertEqual(self.read("SELECT round_id FROM source_round_cancellations"), [(original.round_id,)])

    def test_label_loss_requires_another_question_even_while_original_choice_is_present(self):
        self.setup_paused()
        question = self.offer()
        self.assertEqual(self.send_question(question).status, "published")
        self.observe_comment(4, self.command(question))
        old = self.tick("admitted", ticket=self.ticket)
        self.labels = ()
        self.tick("canceled", ticket=self.ticket)
        self.labels = ("robot",)
        new = self.offer()
        self.assertEqual(new["version"], question["version"] + 1)
        self.assertEqual(self.send_question(new).status, "published")
        self.assertEqual(self.tick("held", ticket=self.ticket).reason, "waiting_for_confirmation")
        self.assertEqual(self.read("SELECT COUNT(*) FROM source_resume_transitions"), [(1,)])
        self.assertEqual(self.read("SELECT round_id FROM source_round_cancellations"), [(old.round_id,)])

    def test_independent_readonly_is_admitted_while_waiting_and_resume_waits_for_its_round(self):
        self.setup_paused()
        question = self.offer()
        self.assertEqual(self.send_question(question).status, "published")
        self.observe_comment(4, "@test-helper Please investigate the current repository.")
        readonly = self.tick("admitted", ticket=self.ticket)
        self.assertEqual(
            self.read("SELECT capability FROM source_rounds WHERE round_id=?", (readonly.round_id,)), [("readonly",)]
        )
        self.assertNotIn((self.original_request,), self.read("SELECT request_id FROM source_round_requests"))
        self.assertEqual(
            self.read("SELECT phase FROM ingress_requests WHERE request_id=?", (self.original_request,)), [("paused",)]
        )
        self.assertFalse(self.read("SELECT * FROM source_confirmation_answers"))
        self.tick("stage_recorded")
        self.observe_comment(5, self.command(question))
        self.tick("prepared")
        self.assertEqual(self.read("SELECT consumer_id FROM source_confirmation_answers"), [(None,)])
        self.assertFalse(self.read("SELECT * FROM source_resume_transitions"))
        self.assertEqual(
            self.read("SELECT status FROM source_rounds WHERE round_id=?", (readonly.round_id,)), [("active",)]
        )

    def test_resume_waits_for_newly_unknown_historical_worker_and_preserves_old_cancellation(self):
        self.setup_paused(pause=False)
        first = self.tick("admitted", ticket=self.ticket)
        self.tick("prepared")
        self.tick("dispatched")
        self.labels = ()
        self.tick("draining", terminate_worker=lambda _metadata: {"status": "terminated"})
        self.tick("canceled")
        [[attempt, settled]] = self.read("SELECT attempt_id,metadata_json FROM attempts")
        self.labels = ("robot",)
        question = self.offer()
        self.assertEqual(self.send_question(question).status, "published")
        self.observe_comment(4, self.command(question))
        uncertain = json.loads(settled)
        uncertain.pop("process_stopped_at", None)
        uncertain.pop("termination", None)
        uncertain["dispatch"]["cleanup_pending"] = True
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE attempts SET metadata_json=? WHERE attempt_id=?", (json.dumps(uncertain), attempt))
        held = self.tick("held", ticket=self.ticket)
        self.assertEqual(held.reason, "prior_source_effect_or_execution")
        self.assertFalse(self.read("SELECT * FROM source_resume_transitions"))
        self.assertEqual(self.read("SELECT consumer_id FROM source_confirmation_answers"), [(None,)])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE attempts SET metadata_json=? WHERE attempt_id=?", (settled, attempt))
        resumed = self.tick("admitted", ticket=self.ticket)
        self.assertNotEqual(first.round_id, resumed.round_id)
        self.assertEqual(self.read("SELECT round_id FROM source_round_cancellations"), [(first.round_id,)])
        self.assertEqual(self.read("SELECT status FROM attempts WHERE attempt_id=?", (attempt,)), [("canceled",)])

    def _accept_answer(self):
        self.tick("prepared")
        self.tick("dispatched")
        self.submit("classification_result", recommended_route="comment-analysis")
        self.tick("waiting")
        self.tick("audited")
        self.tick("prepared")
        self.tick("dispatched")
        self.submit(
            "comment_analysis",
            planned_actions=[{"provider": "tapd", "type": "comment", "target_id": "ticket", "body": "Findings."}],
        )
        self.tick("waiting")
        self.tick("audited")
        return self.tick("publication_pending")

    def test_label_loss_releases_accepted_delivery_without_replaying_consumed_work(self):
        self.setup_paused(pause=False)
        self.tick("admitted", ticket=self.ticket)
        root = self._accept_answer()
        ledger = self.read("SELECT * FROM request_events")
        candidate = self.read("SELECT * FROM github_actions WHERE result_id IS NOT NULL")
        attempts = self.read("SELECT * FROM attempts")
        self.labels = ()
        paused = self.tick("canceled", ticket=self.ticket)
        self.assertEqual(paused.reason, "accepted_delivery_paused")
        self.assertEqual(self.read("SELECT * FROM request_events"), ledger)
        self.assertEqual(self.read("SELECT * FROM github_actions WHERE result_id IS NOT NULL"), candidate)
        self.assertEqual(self.read("SELECT * FROM attempts"), attempts)
        self.assertEqual(self.read("SELECT round_id FROM source_round_cancellations"), [(root.round_id,)])
        self.assertEqual(self.read("SELECT round_id,generation FROM source_delivery_pauses"), [(root.round_id, 1)])
        self.tick("idle", ticket=self.ticket)
        self.assertEqual(self.read("SELECT COUNT(*) FROM source_delivery_pauses"), [(1,)])
        [[action]] = self.read(
            "SELECT action_id FROM source_required_actions WHERE source_result_id=?", (root.identity,)
        )
        self.assertEqual(
            self.send_question({"source_result_id": root.identity, "action_id": action}).status, "deferred"
        )
        self.assertFalse(self.writes)
        self.labels = ("robot",)
        question = self.offer()
        self.assertEqual(self.send_question(question).status, "published")
        self.tick("held", ticket=self.ticket)
        choice = self.observe_comment(24, self.command(question))
        self.tick("held", ticket=self.ticket)
        self.assertEqual(
            self.read("SELECT event_id,consumer_id FROM source_confirmation_answers"), [(choice.version.event_id, None)]
        )
        self.assertEqual(self.read("SELECT COUNT(*) FROM source_resume_transitions"), [(0,)])
        independent = self.observe_comment(25, "@test-helper Please inspect independent work.")
        self.comments = [independent, *self.comments[:-1]]
        future = self.tick("admitted", ticket=self.ticket)
        self.assertNotEqual(future.round_id, root.round_id)
        self.assertEqual(
            self.read("SELECT capability FROM source_rounds WHERE round_id=?", (future.round_id,)), [("readonly",)]
        )
        self.assertEqual(
            self.read("SELECT state,source_result_id FROM request_events WHERE request_id=?", (self.original_request,)),
            [("consumed", root.identity)],
        )
        self.tick("stage_recorded")
        for _ in self.configured["repos"]:
            child = self.tick("prepared")
            [[repo_id, repo_root]] = self.read(
                "SELECT r.repo_id,r.repo_root FROM source_round_tasks t JOIN repos r ON r.repo_id=t.repo_id WHERE t.task_id=?",
                (child.identity,),
            )
            self.tick("dispatched")
            digest = sha256((Path(repo_root) / "README.md").read_bytes()).hexdigest()
            self.submit(
                "local_result",
                evidence=[{"repo_id": repo_id, "path": "README.md", "revision": "sha256:" + digest}],
            )
            self.tick("waiting")
            self.tick("audited")
        self.tick("stage_recorded")
        answer = self.tick("publication_pending")
        self.assertEqual(answer.round_id, future.round_id)
        result = self.send_question(self._root_action(answer))
        self.assertEqual(result.status, "published")
        self.assertEqual(result.completion.status, "completed")
        self.assertEqual(
            self.read("SELECT status FROM source_rounds WHERE round_id=?", (future.round_id,)),
            [("execution_finished",)],
        )
        self.assertEqual(self.read("SELECT round_id,generation FROM source_delivery_pauses"), [(root.round_id, 1)])
        self.assertEqual(self.read("SELECT consumer_id FROM source_confirmation_answers"), [(None,)])

    def test_answer_resume_holds_when_original_checkout_has_no_verifiable_snapshot(self):
        self.setup_paused(pause=False)
        self.tick("admitted", ticket=self.ticket)
        root = self._accept_answer()
        ledger = self.read("SELECT * FROM request_events")
        accepted = self.read("SELECT * FROM source_results WHERE source_result_id=?", (root.identity,))
        self.labels = ()
        self.tick("canceled", ticket=self.ticket)
        cancellation = self.read("SELECT * FROM source_round_cancellations")
        self.labels = ("robot",)
        question = self.offer()
        self.assertEqual(self.send_question(question).status, "published")
        self.observe_comment(61, self.command(question))
        held = self.tick("held", ticket=self.ticket)
        self.assertEqual(held.reason, "source_delivery_original_checkout_unproven")
        self.assertEqual(
            self.read("SELECT cause FROM source_attentions WHERE kind='delivery_recovery'"),
            [("source_delivery_original_checkout_unproven",)],
        )
        self.assertEqual(self.read("SELECT consumer_id FROM source_confirmation_answers"), [(None,)])
        self.assertEqual(self.send_question(self._root_action(root)).status, "deferred")
        self.assertEqual(self.read("SELECT * FROM request_events"), ledger)
        self.assertEqual(self.read("SELECT * FROM source_results WHERE source_result_id=?", (root.identity,)), accepted)
        self.assertEqual(self.read("SELECT * FROM source_round_cancellations"), cancellation)
        self.assertEqual(len(self.started), 2)
        self.assertEqual(len(self.writes), 1)

    def test_answer_recovery_requires_unchanged_original_repository_snapshot(self):
        import subprocess

        self.setup_paused(pause=False)
        repo = Path(self.configured["repos"][0]["repo_root"])
        for arguments in (
            ("init", "-b", "main"),
            ("config", "user.name", "Repository Bot"),
            ("config", "user.email", "bot@example.com"),
            ("remote", "add", "upstream", "https://github.com/org/alpha.git"),
            ("add", "README.md"),
            ("commit", "-m", "Initial repository"),
        ):
            subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
        original = (repo / "README.md").read_text()
        self.tick("admitted", ticket=self.ticket)
        root = self._accept_answer()
        ledger = self.read("SELECT * FROM request_events")
        accepted = self.read("SELECT * FROM source_results WHERE source_result_id=?", (root.identity,))
        self.labels = ()
        self.tick("canceled", ticket=self.ticket)
        self.labels = ("robot",)
        question = self.offer()
        self.assertEqual(self.send_question(question).status, "published")
        self.observe_comment(61, self.command(question))
        (repo / "README.md").write_text("Newer local evidence")
        observer = source_delivery_resume.observe

        def lose_claim(*args, **kwargs):
            result = observer(*args, **kwargs)
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute("UPDATE source_sessions SET token=token+1 WHERE source_id=?", (self.ticket.source_id,))
            return result

        with patch.object(source_delivery_resume, "observe", side_effect=lose_claim):
            self.tick("attention", ticket=self.ticket)
        self.assertFalse(self.read("SELECT * FROM source_recovery_attentions"))
        held = self.tick("attention", ticket=self.ticket)
        self.assertEqual(held.reason, "source_delivery_reprocess_required")
        self.assertFalse(self.read("SELECT * FROM source_delivery_recoveries"))
        actor = {
            "protocol": "robert.message.v1",
            "provider": "openclaw",
            "channel": "telegram",
            "conversation": "telegram:42",
            "thread": "",
            "account": "test-account",
            "sender_id": "42",
        }
        self.configured["db_path"] = str(self.db)
        self.configured["messaging"] = {
            **actor,
            "enabled": True,
            "allowed_senders": ["42", "43"],
            "send_after": "2026-07-01T00:00:00Z",
            "target": "42",
            "repos": ["org/alpha"],
            "tapd_projects": [{"host": self.ticket.host, "workspace_id": self.ticket.workspace_id}],
        }
        self.project["operators"] = ["openclaw:telegram:test-account:42"]
        [notice] = messaging.inbox(self.configured, actor)["messages"]
        self.assertEqual(notice["reason_code"], "source_delivery_reprocess_required")
        self.assertEqual(notice["target"]["source_result_id"], root.identity)
        self.assertEqual(notice["target"]["generation"], 1)
        self.assertEqual(notice["target"]["repo_id"], "repo:org/alpha")
        self.assertEqual(notice["actions"], ["ack"])
        self.assertEqual(messaging.inbox(self.configured, {**actor, "sender_id": "43"})["messages"], [])
        self.configured["messaging"]["repos"] = []
        self.assertEqual(messaging.inbox(self.configured, actor)["messages"], [])
        self.configured["messaging"]["repos"] = ["org/alpha"]
        messaging.respond(
            self.configured,
            {
                **actor,
                "notification_id": notice["notification_id"],
                "version": notice["version"],
                "action": "ack",
            },
        )
        self.tick("attention", ticket=self.ticket)
        self.assertEqual(self.read("SELECT COUNT(*) FROM source_recovery_attentions"), [(1,)])
        self.assertEqual(self.read("SELECT consumer_id FROM source_confirmation_answers"), [(None,)])
        self.assertIsNone(self.read("SELECT resolved_at FROM source_attentions WHERE kind='delivery_recovery'")[0][0])
        (repo / "README.md").write_text(original)
        self.assertEqual(self.tick("publication_pending", ticket=self.ticket).identity, root.identity)
        self.assertIsNotNone(
            self.read("SELECT resolved_at FROM source_attentions WHERE kind='delivery_recovery'")[0][0]
        )
        (repo / "README.md").write_text("Changed again before delivery")
        self.assertEqual(self.tick("attention", ticket=self.ticket).reason, "source_delivery_reprocess_required")
        self.assertEqual(self.send_question(self._root_action(root)).status, "deferred")
        self.assertEqual(len(self.writes), 1)
        [renewed] = messaging.inbox(self.configured, actor)["messages"]
        self.assertEqual(renewed["version"], 2)
        self.assertNotEqual(renewed["notification_id"], notice["notification_id"])
        (repo / "README.md").write_text(original)
        self.assertEqual(self.send_question(self._root_action(root)).completion.status, "completed")
        self.assertEqual(messaging.inbox(self.configured, actor)["messages"], [])
        self.assertEqual(len(self.writes), 2)
        self.assertEqual(len(self.started), 2)
        self.assertEqual(self.read("SELECT * FROM request_events"), ledger)
        self.assertEqual(self.read("SELECT * FROM source_results WHERE source_result_id=?", (root.identity,)), accepted)

    def test_assignee_loss_requires_fresh_drain_and_label_restoration_does_not_erase_pause(self):
        self.setup_paused(pause=False)
        self.tick("admitted", ticket=self.ticket)
        root = self._accept_answer()
        self.assignees = ("other",)
        observed = self.observe()
        self.assertEqual(observed.cancel_round_id, root.round_id)
        self.tick("held", ticket=self.ticket, local_only=True)
        with patch.object(self, "provider", side_effect=ValueError("unavailable observation")):
            self.tick("held", ticket=self.ticket)
        self.assertEqual(self.read("SELECT COUNT(*) FROM source_delivery_pauses"), [(0,)])
        self.assignees = ("owner",)
        self.assertEqual(self.tick("canceled", ticket=self.ticket).reason, "accepted_delivery_paused")
        self.assertEqual(self.offer()["version"], 1)

    def test_original_trigger_removal_after_label_loss_cannot_become_dormant(self):
        self.setup_paused(pause=False)
        self.tick("admitted", ticket=self.ticket)
        first = self._accept_answer()
        self.assertEqual(self.send_question(self._root_action(first)).status, "published")
        trigger = self.observe_comment(31, "@test-helper Please inspect this change.")
        self.tick("admitted", ticket=self.ticket)
        root = self._accept_answer()
        ledger = self.read("SELECT state,source_result_id FROM request_events")
        self.assertCountEqual(ledger, [("consumed", first.identity), ("consumed", root.identity)])
        self.labels = ()
        self.observe()
        complete = self.comments[:]
        self.comments = [item for item in self.comments if item.version.event_id != trigger.version.event_id]
        self.assertEqual(self.tick("held", ticket=self.ticket).reason, "source_publication_settlement_required")
        self.comments = complete
        tapd = self.configured["tapd"]
        original_name = tapd["bot_name"]
        tapd["bot_name"] = "renamed-helper"
        self.assertEqual(self.tick("held", ticket=self.ticket).reason, "source_publication_settlement_required")
        tapd["bot_name"] = original_name
        self.observe_comment(31, "Withdrawn.", created=trigger.version.created, modified=utc_now())
        result = self.tick("canceled", ticket=self.ticket)
        self.assertEqual(result.reason, "accepted_delivery_retired")
        self.assertEqual(self.read("SELECT reason FROM source_round_cancellations"), [("confirmation_required",)])
        self.assertEqual(self.read("SELECT COUNT(*) FROM source_delivery_pauses"), [(0,)])
        self.assertEqual(self.read("SELECT state,source_result_id FROM request_events"), ledger)
        self.assertEqual(
            self.read(
                "SELECT publish_status FROM github_actions WHERE action_id=?", (self._root_action(root)["action_id"],)
            ),
            [("skipped",)],
        )
        self.assertEqual(
            self.read("SELECT status FROM source_rounds WHERE round_id=?", (root.round_id,)), [("canceled",)]
        )

        self.assertEqual(self.send_question(self._root_action(root)).status, "deferred")
        self.assertEqual(len(self.writes), 1)
        self.tick("idle", ticket=self.ticket)
        self.assertEqual(self.read("SELECT COUNT(*) FROM source_result_retirements"), [(1,)])
        self.observe_comment(32, "@test-helper Investigate independently.")
        future = self.tick("admitted", ticket=self.ticket)
        self.assertEqual(
            self.read("SELECT capability FROM source_rounds WHERE round_id=?", (future.round_id,)), [("readonly",)]
        )

    def test_terminal_scan_selects_historical_delivery_without_explicit_ticket(self):
        from robert_agent.application.source_cycle import has_due_source

        self.setup_paused(pause=False)
        self.tick("admitted", ticket=self.ticket)
        root = self._accept_answer()
        self.labels = ()
        self.assertEqual(self.tick("canceled", ticket=self.ticket).reason, "accepted_delivery_paused")
        self.terminal = True
        observed = self.observe()
        self.assertEqual(observed.reason, "terminal")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE tapd_coordination SET next_due_at='2000-01-01T00:00:00+00:00'")
        self.assertTrue(has_due_source(self.db, self.configured, owner="test-coordinator"))
        self.assertFalse(has_due_source(self.db, self.configured, owner="test-coordinator", local_only=True))
        outcome = self.tick("canceled")
        self.assertEqual((outcome.reason, outcome.identity), ("accepted_delivery_retired", root.identity))
        self.tick("idle")
        self.assertEqual(self.read("SELECT COUNT(*) FROM source_result_retirements"), [(1,)])
        self.assertFalse(self.writes)

    def test_terminal_dormant_retirement_proves_prepared_intent_and_historical_exit(self):
        from robert_agent.delivery.application import source_retirement
        from robert_agent.delivery.tapd_policy import prepare_comment
        from robert_agent.persistence import execution_control
        from robert_agent.persistence.queries import source_retirement as retired
        from robert_agent.persistence.rounds.source_evidence import context_digest
        from robert_agent.persistence.stores import source_results

        self.setup_paused(pause=False)
        self.tick("admitted", ticket=self.ticket)
        root = self._accept_answer()
        action = self._root_action(root)["action_id"]
        [[raw_target]] = self.read("SELECT target_json FROM github_actions WHERE action_id=?", (action,))
        packet = prepare_comment("Findings.", "rbt-0123456789abcd", bot_name="test-helper", reply_name="test-desk")
        now = utc_now()
        prepared = {
            "action_id": action,
            "target": json.loads(raw_target),
            "body": packet.body,
            "body_digest": packet.body_digest,
            "normalization_version": packet.normalization_version,
            "receipt_id": packet.receipt_id,
            "bot_name": packet.bot_name,
            "reply_name": packet.reply_name,
            "publisher_principal": "bot",
            "publisher_identity_evidence_id": "provider-proof",
            "publisher_observed_at": now,
        }
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            intent = execution_control.prepare_publication_intent(
                conn, action, "test-prepare", context_digest(prepared), now
            )
            source_results.insert_tapd_publication_preparation(
                conn, publication_id=intent["publication_id"], evidence=prepared
            )
        [[prep]] = self.read("SELECT prepared_evidence_json FROM tapd_publication_preparations")
        self.labels = ()
        self.assertEqual(self.tick("canceled", ticket=self.ticket).reason, "accepted_delivery_paused")
        original = self.read("SELECT * FROM source_results")
        ledger = self.read("SELECT * FROM request_events")
        cancellation = self.read("SELECT * FROM source_round_cancellations")
        self.terminal = True
        observed = self.observe()
        [[owner, token]] = self.read(
            "SELECT owner,token FROM source_sessions WHERE source_id=?", (self.ticket.source_id,)
        )
        claim = source_sessions.SourceClaim(self.ticket.source_id, owner, token)
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            for fence, revision in ((replace(claim, token=token + 1), observed.revision), (claim, "stale")):
                self.assertIsNone(
                    source_retirement.retire_accepted(
                        conn,
                        self.configured,
                        self.ticket,
                        fence,
                        round_id=root.round_id,
                        revision=revision,
                        now=utc_now(),
                    )
                )
        self.assertEqual(self.read("SELECT COUNT(*) FROM source_result_retirements"), [(0,)])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE tapd_publication_preparations SET prepared_evidence_json='{}'")
        self.tick("held", ticket=self.ticket)
        self.assertEqual(self.read("SELECT status FROM publication_intents"), [("prepared",)])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE tapd_publication_preparations SET prepared_evidence_json=?", (prep,))
            [[attempt, metadata]] = conn.execute(
                "SELECT attempt_id,metadata_json FROM attempts ORDER BY started_at LIMIT 1"
            ).fetchall()
            uncertain = json.loads(metadata)
            uncertain.pop("process_stopped_at", None)
            uncertain.pop("termination", None)
            uncertain["dispatch"]["cleanup_pending"] = True
            conn.execute("UPDATE attempts SET metadata_json=? WHERE attempt_id=?", (json.dumps(uncertain), attempt))
        self.tick("held", ticket=self.ticket)
        self.assertEqual(self.read("SELECT status FROM publication_intents"), [("prepared",)])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE attempts SET metadata_json=? WHERE attempt_id=?", (metadata, attempt))
        self.assertEqual(self.tick("canceled", ticket=self.ticket).reason, "accepted_delivery_retired")
        self.assertEqual(
            self.read("SELECT status,started_at,remote_evidence_json FROM publication_intents"),
            [("failed", None, "{}")],
        )
        self.assertEqual(self.read("SELECT * FROM source_results"), original)
        self.assertEqual(self.read("SELECT * FROM request_events"), ledger)
        self.assertEqual(self.read("SELECT * FROM source_round_cancellations"), cancellation)
        with closing(database.connect(self.db)) as conn:
            self.assertTrue(retired.is_retired(conn, root.identity))
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE publication_intents SET error_json='{}'")
        with closing(database.connect(self.db)) as conn:
            self.assertFalse(retired.is_retired(conn, root.identity))
        self.tick("held", ticket=self.ticket)
        self.assertFalse(self.writes)

    def _root_action(self, root):
        [[action]] = self.read(
            "SELECT action_id FROM source_required_actions WHERE source_result_id=?", (root.identity,)
        )
        return {"source_result_id": root.identity, "action_id": action}

    def test_historical_model_requires_exit_proof_before_releasing_new_dormant_round(self):
        self.setup_paused()
        offer = self.offer()
        self.assertEqual(self.send_question(offer).status, "published")
        independent = self.observe_comment(41, "@test-helper Investigate independently.")
        self.comments = [independent, *self.comments[:-1]]
        self.model_kind = "question"
        readonly = self.tick("admitted", ticket=self.ticket)
        self.tick("stage_recorded")
        question = self.tick("publication_pending", ticket=self.ticket)
        self.assertEqual(self.send_question(self._root_action(question)).status, "published")
        [[run, proof]] = self.read(
            "SELECT run_id,exit_json FROM source_model_runs WHERE round_id=?", (readonly.round_id,)
        )
        self.observe_comment(42, self.command(offer))
        self.tick("admitted", ticket=self.ticket)
        root = self._accept_answer()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE source_model_runs SET exit_json='{}' WHERE run_id=?", (run,))
        self.labels = ()
        self.assertEqual(self.tick("held", ticket=self.ticket).reason, "source_publication_settlement_required")
        self.assertEqual(self.read("SELECT COUNT(*) FROM source_delivery_pauses"), [(0,)])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE source_model_runs SET exit_json=? WHERE run_id=?", (proof, run))
        self.assertEqual(self.tick("canceled", ticket=self.ticket).identity, root.identity)

    def test_historical_worker_requires_exit_proof_before_releasing_new_dormant_round(self):
        self.setup_paused(pause=False)
        self.tick("admitted", ticket=self.ticket)
        first = self._accept_answer()
        self.assertEqual(self.send_question(self._root_action(first)).status, "published")
        old = self.started[-1]["attempt_id"]
        self.observe_comment(51, "@test-helper Inspect new work.")
        self.tick("admitted", ticket=self.ticket)
        root = self._accept_answer()
        [[proof]] = self.read("SELECT metadata_json FROM attempts WHERE attempt_id=?", (old,))
        unclear = json.loads(proof)
        unclear.pop("process_stopped_at", None)
        unclear.pop("termination", None)
        unclear["dispatch"] = {**unclear["dispatch"], "cleanup_pending": True}
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE attempts SET metadata_json=? WHERE attempt_id=?", (json.dumps(unclear), old))
        self.labels = ()
        self.assertEqual(self.tick("held", ticket=self.ticket).reason, "source_publication_settlement_required")
        self.assertEqual(self.read("SELECT COUNT(*) FROM source_delivery_pauses"), [(0,)])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE attempts SET metadata_json=? WHERE attempt_id=?", (proof, old))
        self.assertEqual(self.tick("canceled", ticket=self.ticket).identity, root.identity)

    def test_unknown_comment_holds_until_actual_readback_then_can_release_execution(self):
        self.setup_paused(pause=False)
        self.tick("admitted", ticket=self.ticket)
        root = self._accept_answer()
        [[action]] = self.read(
            "SELECT action_id FROM source_required_actions WHERE source_result_id=?", (root.identity,)
        )
        supplied = {"source_result_id": root.identity, "action_id": action}
        self.first_unknown = True
        self.assertEqual(self.send_question(supplied).status, "unknown")
        self.labels = ()
        self.tick("held", ticket=self.ticket)
        self.assertEqual(self.read("SELECT COUNT(*) FROM source_delivery_pauses"), [(0,)])
        self.assertEqual(self.send_question(supplied).status, "published")
        confirmed = self.read(
            "SELECT status,remote_evidence_json FROM publication_intents WHERE action_id=?", (action,)
        )
        self.assertEqual(self.tick("canceled", ticket=self.ticket).reason, "accepted_delivery_paused")
        self.assertEqual(
            self.read("SELECT status,remote_evidence_json FROM publication_intents WHERE action_id=?", (action,)),
            confirmed,
        )
        self.assertEqual(len(self.writes), 1)

    def test_dormant_proof_holds_on_tampered_frozen_input_or_required_effect(self):
        self.setup_paused(pause=False)
        self.tick("admitted", ticket=self.ticket)
        root = self._accept_answer()
        self.labels = ()
        [[action, metadata, target]] = self.read(
            "SELECT action_id,metadata_json,target_json FROM github_actions WHERE action_id IN "
            "(SELECT action_id FROM source_required_actions WHERE source_result_id=?)",
            (root.identity,),
        )
        with closing(sqlite3.connect(self.db)) as conn, conn:
            other = {**json.loads(target), "external_id": "999"}
            conn.execute("UPDATE github_actions SET target_json=? WHERE action_id=?", (json.dumps(other), action))
        self.assertEqual(self.tick("held", ticket=self.ticket).reason, "source_publication_settlement_required")
        self.assertEqual(self.read("SELECT COUNT(*) FROM source_delivery_pauses"), [(0,)])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE github_actions SET target_json=? WHERE action_id=?", (target, action))
        self.assertEqual(self.tick("canceled", ticket=self.ticket).reason, "accepted_delivery_paused")
        [[frozen]] = self.read("SELECT input_snapshot_json FROM source_rounds WHERE round_id=?", (root.round_id,))
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE source_rounds SET input_snapshot_json=? WHERE round_id=?", (frozen + " ", root.round_id)
            )
        self.assertEqual(self.tick("held", ticket=self.ticket).reason, "prior_source_effect_or_execution")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE source_rounds SET input_snapshot_json=? WHERE round_id=?", (frozen, root.round_id))
            changed = {**json.loads(metadata), "body": "Changed body"}
            conn.execute("UPDATE github_actions SET metadata_json=? WHERE action_id=?", (json.dumps(changed), action))
        self.assertEqual(self.tick("held", ticket=self.ticket).reason, "prior_source_effect_or_execution")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            pacing = {
                "status": "deferred",
                "reason_code": "scope_unavailable",
                "retryable": True,
                "next_retry_at": utc_now(),
            }
            conn.execute(
                "UPDATE github_actions SET metadata_json=? WHERE action_id=?",
                (json.dumps({**json.loads(metadata), "publish": pacing}), action),
            )
        self.tick("idle", ticket=self.ticket)
        self.assertEqual(self.read("SELECT COUNT(*) FROM source_delivery_pauses"), [(1,)])

    def test_canceled_accepted_worker_reproves_exit_without_changing_completion(self):
        self.setup_paused(pause=False)
        self.tick("admitted", ticket=self.ticket)
        root = self._accept_answer()
        attempt = self.started[-1]["attempt_id"]
        [[completed_at, encoded]] = self.read(
            "SELECT finished_at,metadata_json FROM attempts WHERE attempt_id=?", (attempt,)
        )
        uncertain = json.loads(encoded)
        uncertain.pop("process_stopped_at", None)
        uncertain.pop("termination", None)
        uncertain["dispatch"] = {**uncertain["dispatch"], "cleanup_pending": True}
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE attempts SET metadata_json=? WHERE attempt_id=?", (json.dumps(uncertain), attempt))
        self.labels = ()
        blocked = self.tick("attention", ticket=self.ticket, terminate_worker=lambda _: {"status": "unknown"})
        self.assertEqual(blocked.reason, "worker_exit_unconfirmed")
        self.assertEqual(self.read("SELECT COUNT(*) FROM source_delivery_pauses"), [(0,)])
        self.tick("draining", ticket=self.ticket, terminate_worker=lambda _: {"status": "already_dead"})
        self.assertEqual(
            self.read("SELECT status,finished_at FROM attempts WHERE attempt_id=?", (attempt,)),
            [("completed", completed_at)],
        )
        self.assertEqual(self.tick("canceled", ticket=self.ticket).identity, root.identity)

    def test_settled_consumed_initial_request_is_never_replayed_on_resume(self):
        self.setup_paused(pause=False)
        self.tick("admitted", ticket=self.ticket)
        root = self._accept_answer()
        [[action]] = self.read(
            "SELECT action_id FROM source_required_actions WHERE source_result_id=?", (root.identity,)
        )
        self.assertEqual(
            self.send_question({"source_result_id": root.identity, "action_id": action}).status, "published"
        )
        original = self.read("SELECT * FROM request_events")
        self.labels = ()
        self.observe()
        self.labels = ("robot",)
        question = self.offer()
        self.assertEqual(self.send_question(question).status, "published")
        self.observe_comment(8, self.command(question))
        self.tick("idle", ticket=self.ticket)
        self.assertEqual(self.read("SELECT request_ids_json FROM source_resume_transitions"), [("[]",)])
        self.assertEqual(self.read("SELECT * FROM request_events"), original)
        self.observe_comment(9, "@test-helper Please inspect the next change.")
        future = self.tick("admitted", ticket=self.ticket)
        self.assertNotIn(
            (self.original_request,),
            self.read("SELECT request_id FROM source_round_requests WHERE round_id=?", (future.round_id,)),
        )

    def test_choice_withdrawal_holds_accepted_root_without_reexecution_or_publication(self):
        self.setup_paused()
        question = self.offer()
        self.assertEqual(self.send_question(question).status, "published")
        answer = self.observe_comment(4, self.command(question))
        self.tick("admitted", ticket=self.ticket)
        root = self._accept_answer()
        self.observe_comment(4, "@test-helper Please pause.", created=answer.version.created, modified=utc_now())
        held = self.tick("held", ticket=self.ticket)
        self.assertEqual(held.reason, "source_publication_settlement_required")
        [[action]] = self.read(
            "SELECT action_id FROM source_required_actions WHERE source_result_id=?", (root.identity,)
        )
        result = self.send_question({"source_result_id": root.identity, "action_id": action})
        self.assertEqual(result.status, "deferred")
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.read("SELECT state,source_result_id FROM request_events"), [("consumed", root.identity)])
        self.assertEqual(self.read("SELECT round_id FROM source_round_cancellations"), [(root.round_id,)])
        self.assertEqual(len(self.started), 2)
        self.assertEqual(self.read("SELECT COUNT(*) FROM source_resume_transitions"), [(1,)])

    def test_explicit_resume_keeps_individually_revoked_paused_request_revoked(self):
        self.setup_paused(pause=False)
        comment = self.observe_comment(4, "@test-helper A separate request before the pause.")
        queued = self.observe()
        [extra] = queued.request_ids
        self.labels = ()
        self.observe()
        self.observe_comment(4, "Withdrawn.", created=comment.version.created, modified=utc_now())
        self.labels = ("robot",)
        question = self.offer()
        self.assertEqual(self.send_question(question).status, "published")
        self.observe_comment(5, self.command(question))
        resumed = self.tick("admitted", ticket=self.ticket)
        self.assertEqual(
            self.read("SELECT request_id FROM source_round_requests WHERE round_id=?", (resumed.round_id,)),
            [(self.original_request,)],
        )
        self.assertEqual(self.read("SELECT phase FROM ingress_requests WHERE request_id=?", (extra,)), [("revoked",)])
        self.assertEqual(self.read("SELECT state FROM request_events WHERE request_id=?", (extra,)), [("revoked",)])

    def test_actual_question_receipt_then_exact_choice_can_be_claimed_once(self):
        self.setup_paused()
        question = self.offer()
        self.assertEqual(self.offer()["question_id"], question["question_id"])
        pushed = self.send_question(question)
        self.assertEqual(pushed.status, "published", pushed)
        self.assertIsNone(pushed.completion)
        self.assertEqual(self.read("SELECT COUNT(*) FROM source_question_attentions"), [(0,)])
        self.assertEqual(self.read("SELECT COUNT(*) FROM source_attentions WHERE kind='ordinary_question'"), [(0,)])
        self.assertIn("原仓库:org/alpha。当前仓库:org/alpha。标签:robot。", self.writes[0])
        self.assertNotIn("@test-helper", self.writes[0])
        self.assertIn("test-desk", self.writes[0])
        self.assertIn("ROBERT-CHOICE", self.writes[0])
        self.assertEqual(self.tick("held", ticket=self.ticket).reason, "waiting_for_confirmation")
        choice = self.observe_comment(1, self.command(question, bot="ｔｅｓｔ－ｈｅｌｐｅｒ").replace("@", "＠"))
        observed = self.observe()
        self.assertEqual(observed.request_ids, ())
        receipt = self.receipt(question)
        self.assertEqual(
            (receipt.event_id, receipt.comment_id, receipt.option), (choice.version.event_id, "101", "resume")
        )
        self.assertIsNone(self.receipt(question, consumer="other", token_delta=1))
        self.assertIsNone(self.receipt(question, consumer="wrong-option", option="switch"))
        self.assertEqual(self.receipt(question, consumer="resume:original").consumer_id, "resume:original")
        self.assertIsNone(self.receipt(question, consumer="resume:second"))
        self.assertEqual(self.read("SELECT COUNT(*) FROM tasks"), [(0,)])
        self.assertEqual(self.read("SELECT COUNT(*) FROM worker_results"), [(0,)])
        self.assertEqual(
            self.read("SELECT phase FROM ingress_requests WHERE request_id=?", (self.original_request,)), [("paused",)]
        )
        self.assertEqual(self.read("SELECT state,source_result_id FROM request_events"), [("pending", None)])

    def test_named_confirmation_does_not_authorize_another_active_same_rule_binding(self):
        self.setup_paused()
        question = self.offer()
        self.assertEqual(self.send_question(question).status, "published")
        self.observe_comment(1, self.command(question))
        self.observe()
        self.assertIsNotNone(self.receipt(question, consumer="named-resume"))

        def claimed(config):
            with closing(database.connect(self.db, foreign_keys=True)) as conn:
                return tapd_intake.claimed_confirmation(
                    conn,
                    config,
                    self.ticket,
                    revision=self.revision,
                    question_id=question["question_id"],
                    version=question["version"],
                    option="resume",
                    scope_digest=question["scope_digest"],
                    consumer_id="named-resume",
                )

        config = self.configured["tapd"]
        self.assertIsNotNone(claimed(config))
        self.assertIsNone(claimed({**config, "enabled": False}))
        self.assertIsNone(claimed({**config, "host": "different.example"}))
        # Simulate an unrelated operator state change, not a supported resume.
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE sources SET metadata_json=json_set(metadata_json,'$.binding.state','active',"
                "'$.binding.request_id','another-episode') WHERE source_id=?",
                (self.ticket.source_id,),
            )
        self.assertIsNone(claimed(config))
        self.observe()
        self.assertIsNone(claimed(config))
        [[revoked]] = self.read("SELECT revoked_at FROM source_confirmation_answers")
        self.assertTrue(revoked)

    def test_same_second_answer_waits_before_later_unknown_write_readback(self):
        self.setup_paused()
        question = self.offer()
        self.first_unknown = True
        self.assertEqual(self.send_question(question).status, "unknown")
        [[started]] = self.read("SELECT started_at FROM publication_intents")
        created = datetime.fromisoformat(started).astimezone(timezone.utc).replace(microsecond=0).isoformat()
        choice = self.observe_comment(1, self.command(question), created=created)
        self.observe()
        self.assertIsNone(self.receipt(question))
        self.assertEqual(
            self.read(
                "SELECT json_extract(payload_json,'$.disposition') FROM source_events WHERE event_id=?",
                (choice.version.event_id,),
            ),
            [("confirmation_pending",)],
        )
        self.assertEqual(self.send_question(question).status, "published")
        self.observe()
        self.assertEqual(self.receipt(question).event_id, choice.version.event_id)
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.read("SELECT COUNT(*) FROM source_confirmation_answers"), [(1,)])

    def test_same_repository_labels_use_the_existing_selected_rule(self):
        self.setup_paused()
        selected = self.project["rules"][0]
        self.project["rules"].append({**selected, "id": "zzz-same-repo", "label": "same-repo-extra"})
        self.labels = ("robot", "same-repo-extra")
        self.observe()
        question = self.offer()
        scope = json.loads(question["scope_json"])
        self.assertEqual(scope["option"], "resume")
        self.assertEqual(scope["current_rule"]["id"], selected["id"])
        self.assertEqual(self.send_question(question).status, "published")
        self.observe_comment(1, self.command(question))
        self.observe()
        self.assertEqual(self.receipt(question, consumer="same-repo-resume").option, "resume")

    def test_stale_edited_forged_commands_do_not_enqueue_and_quoted_ordinary_reply_can(self):
        self.setup_paused()
        original_context = self.observe_comment(1, "Old background context")
        self.observe()
        question = self.offer()
        self.assertEqual(self.send_question(question).status, "published")
        version = question["version"]
        qid = question["question_id"]
        self.observe_comment(1, self.command(question), created=original_context.version.created)
        self.observe_comment(2, self.command(question, option="switch"))
        self.observe_comment(3, self.command(question).replace(f"v{version}", f"v{version + 1}"))
        self.observe_comment(4, self.command(question), principal=None, proof=False)
        self.observe_comment(5, self.command(question), entry="bug_remark", proof=False)
        self.observe_comment(6, self.command(question), modified=utc_now())
        self.observe_comment(7, f"<blockquote>{self.command(question)}</blockquote>")
        self.observe_comment(
            8,
            f"<blockquote>ROBERT-CHOICE {qid} v{version} resume</blockquote><p>@test-helper inspect independently</p>",
        )
        self.observe_comment(10, "@test-helper 请解释 ROBERT-CHOICE 的含义")
        self.observe_comment(11, "@test-helper ROBERT-CHOICE malformed")
        result = self.observe()
        self.assertIsNone(self.receipt(question))
        self.assertEqual(len(result.request_ids), 2)
        self.assertEqual(
            self.read(
                "SELECT json_extract(authorization_json,'$.capability') FROM ingress_requests WHERE request_id IN (?,?)",
                result.request_ids,
            ),
            [("readonly",), ("readonly",)],
        )
        new = self.read(
            "SELECT json_extract(payload_json,'$.latest.external_id') FROM source_events WHERE json_extract(payload_json,'$.disposition')='queued'"
        )
        self.assertCountEqual(new, [("8",), ("10",)])
        choice = self.observe_comment(9, self.command(question), entry="bug_remark")
        self.assertEqual(self.observe().request_ids, ())
        self.assertEqual(self.receipt(question).event_id, choice.version.event_id)

    def test_choice_prose_edit_then_deletion_revokes_with_new_version(self):
        self.setup_paused()
        question = self.offer()
        self.assertEqual(self.send_question(question).status, "published")
        choice = self.observe_comment(1, self.command(question))
        self.observe()
        self.assertEqual(self.receipt(question, consumer="named-resume").event_id, choice.version.event_id)
        self.observe_comment(
            1, self.command(question) + "\nClarifying wording.", created=choice.version.created, modified=utc_now()
        )
        self.observe()
        self.assertEqual(self.receipt(question).consumer_id, "named-resume")
        self.explicit_absences = [facts.CommentAbsence(self.ticket, "bug", "1", "verified-deletion")]
        self.comments = [comment for comment in self.comments if comment.version.external_id != "1"]
        self.observe()
        self.assertIsNone(self.receipt(question))
        [[revoked, consumed]] = self.read("SELECT revoked_at,consumer_id FROM source_confirmation_answers")
        self.assertTrue(revoked)
        self.assertEqual(consumed, "named-resume")
        replacement = self.offer()
        self.assertEqual(replacement["version"], question["version"] + 1)
        self.assertNotEqual(replacement["question_id"], question["question_id"])
        self.assertEqual(self.send_question(replacement).status, "published")
        self.observe_comment(2, self.command(question))
        self.observe()
        self.assertIsNone(self.receipt(replacement))
        self.assertEqual(self.read("SELECT COUNT(*) FROM ingress_requests"), [(1,)])

    def test_scope_change_supersedes_only_unstarted_question_and_conflict_never_overrides(self):
        self.setup_paused(restored=False)
        question = self.offer()
        self.assertEqual(json.loads(question["scope_json"])["option"], "switch")
        self.labels = ("robot", "robot-b")
        self.observe()
        self.assertIsNone(self.question())
        self.assertEqual(self.read("SELECT publish_status FROM github_actions"), [("skipped",)])
        self.assertEqual(self.send_question(question).status, "deferred")
        self.assertEqual(self.writes, [])
        self.labels = ("robot",)
        self.observe()
        replacement = self.offer()
        self.assertEqual(json.loads(replacement["scope_json"])["option"], "resume")
        self.assertEqual(replacement["version"], question["version"] + 1)

    def test_changed_scope_preserves_started_unknown_question_until_reconciled(self):
        self.setup_paused()
        question = self.offer()
        self.first_unknown = True
        self.assertEqual(self.send_question(question).status, "unknown")
        self.labels = ("robot-b",)
        self.observe()
        self.assertIsNone(self.question())
        self.assertEqual(self.read("SELECT status FROM publication_intents"), [("attention",)])
        self.assertEqual(self.tick("held", ticket=self.ticket).reason, "prior_source_effect_or_execution")
        self.assertEqual(self.send_question(question).status, "published")
        replacement = self.offer()
        self.assertEqual(json.loads(replacement["scope_json"])["option"], "switch")
        self.assertEqual(replacement["version"], question["version"] + 1)
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.send_question(replacement).status, "published")
        self.assertIn("原仓库:org/alpha。当前仓库:org/beta。标签:robot-b。", self.writes[-1])
        self.assertNotIn("@test-helper", self.writes[-1])

    def test_additive_upgrade_preserves_existing_tapd_source(self):
        from robert_agent.persistence.schema.database import init_database

        self.setup_paused()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("DROP TABLE source_delivery_recoveries")
            conn.execute("DROP TABLE source_delivery_pauses")
            conn.execute("DROP TABLE source_resume_transitions")
            conn.execute("DROP TABLE source_confirmation_answers")
            conn.execute("DROP TABLE source_confirmation_questions")
        original = self.read("SELECT source_id,metadata_json FROM sources")
        init_database(self.db)
        init_database(self.db)
        self.assertEqual(self.read("SELECT source_id,metadata_json FROM sources"), original)
        self.assertEqual(self.read("PRAGMA foreign_key_check"), [])
        self.assertIsNotNone(self.offer())
