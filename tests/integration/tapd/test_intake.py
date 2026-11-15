from contextlib import closing, contextmanager
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from robert_agent.persistence import database, transactions
from robert_agent.persistence.queries import (
    ingress_requests,
    request_events,
    source_sessions,
    tapd_intake as reads,
    sources,
)
from robert_agent.persistence.schema.database import init_database
from robert_agent.persistence.stores import source_sessions as sessions
from robert_agent.work import tapd_authority as facts
from robert_agent.work.application import tapd_intake as intake

FIRST = "2026-11-14T12:00:00+00:00"
NEXT = "2026-11-14T12:04:00+00:00"
THIRD = "2026-11-14T12:08:00+00:00"
LAST = "2026-11-14T12:12:00+00:00"
MARKER = "<p>@helper-team Please inspect</p>"


class TapdIntakeTests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.db = Path(scratch.name) / "native.sqlite"
        init_database(self.db)
        self.ticket = facts.TicketIdentity("tapd.example", "001", "bug", "90000000000000000009")
        self.project = {
            "workspace_id": "001",
            "trusted_author": "test-user",
            "repositories": ["org/alpha", "org/beta"],
            "mentions": {"enabled": True, "activated_after": "2026-11-14T11:00:00+00:00"},
            "rules": [
                {
                    "id": "a",
                    "assignee": "maintainer",
                    "label": "queue-a",
                    "repo": "org/alpha",
                    "kinds": ["bug"],
                    "repair": {"ci": {"max_wall_minutes": 10}},
                },
                {"id": "b", "assignee": "maintainer", "label": "queue-b", "repo": "org/beta", "kinds": ["bug"]},
            ],
        }
        self.config = {
            "enabled": False,
            "host": self.ticket.host,
            "api_timezone": "Asia/Shanghai",
            "bot_name": "helper-team",
            "reply_name": "service-desk",
            "projects": [self.project],
        }
        self.item = facts.TicketFacts(
            self.ticket, "Bug report", "Ticket content", "open", ("maintainer",), (), False, ("closed",)
        )
        self.sql("""
            INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root)
              VALUES('a','org/alpha','bot','main','/repo','/trees'),('b','org/beta','bot','main','/b','/bt');
            INSERT INTO workstreams(workstream_id,repo_id,created_at,updated_at) VALUES('w','a','now','now');
            INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) VALUES('task','w','queued','now','now');
        """)

    def sql(self, script):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript(script)

    @contextmanager
    def writer(self):
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            yield conn

    def comment(
        self,
        identifier,
        *,
        body=MARKER,
        created="2026-11-14 19:30:00",
        modified=None,
        entry="bug",
        creator="test-user",
        own=True,
        display="test-user",
    ):
        version = facts.CommentVersion(
            self.ticket, entry, identifier, created, modified, sha256(body.encode()).hexdigest()
        )
        return facts.CommentObservation(
            version,
            body,
            display,
            facts.CreatorEvidence(version, creator, "verified-creator") if creator else None,
            facts.OwnBodyEvidence(version, creator, "verified-own") if creator and own else None,
        )

    def observe(
        self,
        conn,
        comments=(),
        *,
        now=FIRST,
        item=None,
        coverage="complete",
        absent=(),
        own=(),
        unknown=(),
        config=None,
    ):
        obs = facts.TicketObservation(item or self.item, now, tuple(comments), coverage, tuple(absent))
        publication = facts.PublicationFacts(self.ticket, frozenset(own), tuple(unknown))
        return intake.observe_ticket(conn, config or self.config, obs, publication, now=now)

    def admit(self, conn, observed, *, round_id="round", now=FIRST):
        claim = sessions.claim(conn, source_id=self.ticket.source_id, owner="owner", now=now, lease_seconds=3600)
        current = source_sessions.session(conn, self.ticket.source_id)
        if claim is None:
            claim = sessions.SourceClaim(self.ticket.source_id, "owner", current["token"])
        result = intake.admit_round(
            conn, self.config, self.ticket, claim, round_id=round_id, revision=observed.revision, now=now
        )
        return claim, result

    def request(self, conn, identifier):
        return ingress_requests.find_by_id(conn, identifier)

    def test_initial_historical_queue_with_complete_context_is_one_shot(self):
        tags = replace(self.item, labels=("queue-a",))
        history = self.comment("1", created="2020-01-01 00:00:00")
        with self.writer() as conn:
            observed = self.observe(conn, (history,), item=tags, coverage="incomplete")
            self.assertEqual(observed.reason, "context_incomplete")
            self.assertEqual(observed.request_ids, ())
            self.assertIsNone(self.admit(conn, observed)[1])
        with self.writer() as conn:
            observed = self.observe(conn, (history,), item=tags, now=NEXT)
            self.assertEqual(len(observed.request_ids), 1)
            row = self.request(conn, observed.request_ids[0])
            self.assertEqual((row["auth_type"], row["repo_id"]), ("label_queue", "a"))
            _, active = self.admit(conn, observed, now=NEXT)
            self.assertEqual(active["capability"], "development")
            discussion = json.loads(active["input_snapshot_json"])["discussion"]
            self.assertEqual(discussion[0]["body_html"], MARKER)
        init_database(self.db)
        with self.writer() as conn:
            observed = self.observe(conn, (history,), item=tags, now=THIRD)
            self.assertEqual(observed.request_ids, ())
            self.assertEqual(len(reads.pending_requests(conn, self.ticket.source_id)), 1)

    def test_comment_arriving_during_initial_scan_queues_behind_initial_request(self):
        tags = replace(self.item, labels=("queue-a",))
        comment = self.comment("23", created="2026-11-14 20:01:00")
        observation = facts.TicketObservation(tags, FIRST, (comment,), "complete")
        with self.writer() as conn:
            observed = intake.observe_ticket(
                conn, self.config, observation, facts.PublicationFacts(self.ticket), now=NEXT
            )
            self.assertEqual(len(observed.request_ids), 1)
            _, initial = self.admit(conn, observed, now=NEXT)
            self.assertIsNotNone(initial)
        with self.writer() as conn:
            later = self.observe(conn, (comment,), item=tags, now=THIRD)
            self.assertEqual(len(later.request_ids), 1)
            self.assertIsNone(self.admit(conn, later, round_id="later", now=THIRD)[1])

    def test_delayed_first_round_keeps_late_tag_followup_separate_and_preserves_discussion(self):
        for mentions, ticket_id in ((True, self.ticket.external_id), (False, "90000000000000000008")):
            with self.subTest(mentions=mentions):
                self.ticket = replace(self.ticket, external_id=ticket_id)
                self.config["projects"][0]["mentions"]["enabled"] = mentions
                tags = replace(self.item, identity=self.ticket, labels=("queue-a",))
                comment = self.comment("23", created="2026-11-14 20:01:00")
                with self.writer() as conn:
                    first = intake.observe_ticket(
                        conn,
                        self.config,
                        facts.TicketObservation(tags, FIRST, (comment,), "complete"),
                        facts.PublicationFacts(self.ticket),
                        now=NEXT,
                    )
                    self.assertEqual(len(first.request_ids), 1)
                with self.writer() as conn:
                    observed = self.observe(conn, (comment,), item=tags, now=THIRD)
                    self.assertEqual(len(observed.request_ids), 1)
                    later_request = observed.request_ids[0]
                    first_request = first.request_ids[0]
                    scope = json.loads(self.request(conn, later_request)["authorization_json"])
                    self.assertEqual((scope["capability"], scope["rule"]["id"]), ("development", "a"))
                    initial_round = "initial-" + ticket_id
                    claim, active = self.admit(conn, observed, round_id=initial_round, now=THIRD)
                    self.assertEqual(
                        [item["request_id"] for item in source_sessions.round_requests(conn, initial_round)],
                        [first_request],
                    )
                    snapshot = json.loads(active["input_snapshot_json"])
                    self.assertEqual(len(snapshot["discussion"]), 1)
                    self.assertEqual(snapshot["discussion"][0]["external_id"], "23")
                    self.assertEqual(snapshot["discussion"][0]["request_scope"], "future_request")
                    self.assertTrue(
                        sessions.finish_round(
                            conn, claim, round_id=initial_round, outcome="execution_finished", now=THIRD
                        )
                    )
                # Unit 5 owns real accepted root consumption. A fixture represents its delivery receipt.
                self.sql(
                    f"UPDATE request_events SET state='consumed',consumed_at='existing-proof' WHERE request_id='{first_request}';"
                )
                with self.writer() as conn:
                    following = "following-" + ticket_id
                    _, active = self.admit(conn, observed, round_id=following, now=THIRD)
                    self.assertEqual(
                        [item["request_id"] for item in source_sessions.round_requests(conn, following)],
                        [later_request],
                    )
                    snapshot = json.loads(active["input_snapshot_json"])
                    self.assertEqual(snapshot["discussion"][0]["request_scope"], "current_request")

    def test_configured_marker_cutoff_verified_creator_and_own_bug_remark(self):
        old = self.comment("1", created="2026-11-14 18:59:59")
        displayed = self.comment("2", creator=None)
        generated = self.comment("3", entry="bug_remark", own=False)
        trusted_remark = self.comment("4", entry="bug_remark")
        malformed_proof = replace(trusted_remark, version=replace(trusted_remark.version, external_id="5"))
        other_marker = self.comment("6", body="@robert-wklken original")
        quoted = self.comment("7", entry="bug_remark", body="<blockquote>@helper-team</blockquote>")
        with self.writer() as conn:
            observed = self.observe(
                conn, (old, displayed, generated, trusted_remark, malformed_proof, other_marker, quoted)
            )
            self.assertEqual(len(observed.request_ids), 1)
            row = self.request(conn, observed.request_ids[0])
            self.assertEqual(json.loads(row["content_snapshot_json"])["comment"]["external_id"], "4")
            active = self.admit(conn, observed)[1]
            self.assertEqual(active["capability"], "readonly")
            self.assertEqual(len(json.loads(active["input_snapshot_json"])["discussion"]), 7)
        with self.writer() as conn:
            drift = deepcopy(self.config)
            drift["projects"][0]["mentions"]["activated_after"] = "2025-01-01T00:00:00+00:00"
            replay = self.observe(conn, (self.comment("8", created="2024-01-01 00:00:00"),), config=drift, now=NEXT)
            self.assertEqual(replay.reason, "activation_drift")
            self.assertEqual(replay.request_ids, ())

    def test_unknown_publication_holds_and_resumes_only_original_eligible(self):
        owned = self.comment("1")
        prefixed = self.comment("2", body="<p>service-desk： @helper-team</p>")
        held = self.comment("3")
        old_context = self.comment("4", body="ordinary")
        with self.writer() as conn:
            initial = self.observe(conn, (owned, prefixed, held, old_context), own=(("bug", "1"),), unknown=("intent",))
            self.assertEqual(initial.reason, "publication_unknown")
            self.assertEqual(initial.request_ids, ())
        with self.writer() as conn:
            resumed = self.observe(
                conn, (owned, prefixed, held, self.comment("4", modified="2026-11-14 19:40:00")), now=NEXT
            )
            self.assertEqual(len(resumed.request_ids), 1)
            row = self.request(conn, resumed.request_ids[0])
            self.assertEqual(json.loads(row["content_snapshot_json"])["comment"]["external_id"], "3")

    def test_edits_revoke_and_reprepare_pending_sibling_after_confirmed_exit(self):
        first, sibling = self.comment("1"), self.comment("2")
        with self.writer() as conn:
            observed = self.observe(conn, (first, sibling))
            by_comment = {
                json.loads(self.request(conn, request_id)["content_snapshot_json"])["comment"][
                    "external_id"
                ]: request_id
                for request_id in observed.request_ids
            }
            request_id, sibling_id = by_comment["1"], by_comment["2"]
        updated = self.comment("2", body="@helper-team update one", modified="2026-11-14 19:40:00")
        with self.writer() as conn:
            observed = self.observe(conn, (first, updated), now=NEXT)
            claim, active = self.admit(conn, observed, now=NEXT)
            self.assertIsNotNone(active)
            self.assertTrue(
                sessions.bind_task(
                    conn,
                    claim,
                    round_id="round",
                    request_id=request_id,
                    task_id="task",
                    capability="readonly",
                    now=NEXT,
                )
            )
        self.sql("""UPDATE tasks SET lifecycle='canceled' WHERE task_id='task';
            INSERT INTO attempts(attempt_id,task_id,attempt_no,status,metadata_json)
              VALUES('old','task',1,'canceled','{"dispatch":{"pid":123}}');""")
        removed = self.comment("1", body="withdrawn", modified="2026-11-14 19:55:00")
        final_sibling = self.comment("2", body="@helper-team update two", modified="2026-11-14 19:55:00")
        with self.writer() as conn:
            observed = self.observe(conn, (removed, final_sibling), now=THIRD)
            self.assertEqual((observed.cancel_round_id, observed.cancel_task_ids), ("round", ("task",)))
            self.assertEqual(self.request(conn, request_id)["phase"], "revoked")
            self.assertFalse(sessions.pending_member(conn, "round", sibling_id))
            self.assertFalse(sessions.finish_round(conn, claim, round_id="round", outcome="canceled", now=THIRD))
            old = {row["request_id"]: row for row in source_sessions.round_requests(conn, "round")}
            self.assertIn("update one", old[sibling_id]["content_snapshot_json"])
            self.assertNotIn("update two", old[sibling_id]["content_snapshot_json"])
        self.sql("""UPDATE attempts SET metadata_json='{"dispatch":{"pid":123},
            "process_stopped_at":"2026-11-14T12:10:00+00:00"}' WHERE attempt_id='old';""")
        with self.writer() as conn:
            observed = self.observe(conn, (self.comment("1"), final_sibling), now=LAST)
            self.assertEqual(observed.request_ids, ())
            self.assertTrue(sessions.finish_round(conn, claim, round_id="round", outcome="canceled", now=LAST))
            current = intake.admit_round(
                conn, self.config, self.ticket, claim, round_id="replacement", revision=observed.revision, now=LAST
            )
            self.assertIsNotNone(current)
            rows = source_sessions.round_requests(conn, "replacement")
            self.assertEqual([row["request_id"] for row in rows], [sibling_id])
            self.assertIn("update two", rows[0]["content_snapshot_json"])
            self.assertEqual(
                [row["state"] for row in request_events.for_ingress_request(conn, sibling_id)], ["pending"]
            )
            self.assertEqual(
                [row["state"] for row in request_events.for_ingress_request(conn, request_id)], ["revoked"]
            )

    def test_traversal_absence_does_not_revoke_but_scoped_proof_does(self):
        initial = self.comment("1")
        with self.writer() as conn:
            first = self.observe(conn, (initial,))
            request = first.request_ids[0]
        with self.writer() as conn:
            no_match = self.observe(conn, now=NEXT)
            self.assertEqual(self.request(conn, request)["phase"], "held")
            self.assertIsNone(self.admit(conn, no_match, now=NEXT)[1])
        with self.writer() as conn:
            absence = facts.CommentAbsence(self.ticket, "bug", "1", "confirmed-provider-deletion")
            self.observe(conn, now=THIRD, absent=(absence,))
            self.assertEqual(self.request(conn, request)["phase"], "revoked")
        with self.writer() as conn:
            replay = self.observe(conn, (initial,), now=LAST)
            self.assertEqual(replay.request_ids, ())
            self.assertEqual(self.request(conn, request)["phase"], "revoked")

    def test_absent_ticket_facts_are_observed_without_permission(self):
        unknown = replace(self.item, status=None, title=None, archived=None)
        with self.writer() as conn:
            observed = self.observe(conn, (self.comment("1"),), item=unknown)
            self.assertEqual((observed.reason, observed.request_ids), ("ticket_state_unknown", ()))
            source = sources.find_by_identity(
                conn,
                provider="tapd",
                host=self.ticket.host,
                project_key="001",
                source_type="bug",
                external_id=self.ticket.external_id,
            )
            self.assertEqual((source["title"], source["state"]), ("", "unknown"))
            self.assertIsNone(json.loads(source["metadata_json"])["ticket"]["status"])

    def test_consumed_initial_does_not_block_later_request(self):
        tags = replace(self.item, labels=("queue-a",))
        with self.writer() as conn:
            first = self.observe(conn, item=tags)
            claim, active = self.admit(conn, first)
            self.assertIsNotNone(active)
            self.assertTrue(
                sessions.finish_round(conn, claim, round_id="round", outcome="execution_finished", now=FIRST)
            )
        # The root-result proof/consumption writer belongs to Unit 5.
        self.sql(
            "UPDATE request_events SET state='consumed',consumed_at='existing-proof' WHERE request_id IS NOT NULL;"
        )
        with self.writer() as conn:
            followup = self.comment("16", created="2026-11-14 20:01:00")
            seen = self.observe(conn, (followup,), item=tags, now=NEXT)
            self.assertEqual(len(seen.request_ids), 1)
            self.assertIsNone(
                intake.revalidate_request(
                    conn, self.config, self.ticket, first.request_ids[0], revision=seen.revision
                ).capability
            )
            current = self.admit(conn, seen, round_id="later", now=NEXT)[1]
            self.assertIsNotNone(current)
            self.assertEqual(
                [row["request_id"] for row in source_sessions.round_requests(conn, "later")], list(seen.request_ids)
            )
            self.assertEqual(
                [row["state"] for row in request_events.for_ingress_request(conn, first.request_ids[0])], ["consumed"]
            )

    def test_held_mention_is_not_released_by_disabled_or_rewound_activation(self):
        request = self.comment("1")
        with self.writer() as conn:
            self.observe(conn, (request,), unknown=("intent",))
        drift = deepcopy(self.config)
        drift["projects"][0]["mentions"]["activated_after"] = "2026-11-01T11:00:00+00:00"
        with self.writer() as conn:
            blocked = self.observe(conn, (request,), config=drift, now=NEXT)
            self.assertEqual(blocked.reason, "activation_drift")
            self.assertEqual(blocked.request_ids, ())
        disabled = deepcopy(self.config)
        disabled["projects"][0]["mentions"]["enabled"] = False
        with self.writer() as conn:
            self.assertEqual(self.observe(conn, (request,), config=disabled, now=THIRD).request_ids, ())
        with self.writer() as conn:
            self.assertEqual(len(self.observe(conn, (request,), now=LAST).request_ids), 1)

    def test_disabled_independent_mention_stays_held_after_tag_activation(self):
        comment = self.comment("1")
        tags = replace(self.item, labels=("queue-a",))
        with self.writer() as conn:
            observed = self.observe(conn, (comment,))
            mention = observed.request_ids[0]
        disabled = deepcopy(self.config)
        disabled["projects"][0]["mentions"]["enabled"] = False
        with self.writer() as conn:
            first_tag = self.observe(conn, (comment,), config=disabled, item=tags, now=NEXT)
            self.assertEqual(len(first_tag.request_ids), 1)
            self.assertEqual(self.request(conn, mention)["phase"], "held")
            self.assertEqual(
                intake.revalidate_request(conn, disabled, self.ticket, mention, revision=first_tag.revision).capability,
                None,
            )
        with self.writer() as conn:
            later = self.comment("2", created="2026-11-14 20:06:00")
            followup = self.observe(conn, (comment, later), config=disabled, item=tags, now=THIRD)
            self.assertEqual(len(followup.request_ids), 1)
            row = self.request(conn, followup.request_ids[0])
            self.assertEqual(json.loads(row["authorization_json"])["capability"], "development")
            self.assertEqual(self.request(conn, mention)["phase"], "held")

    def test_conflict_pause_restore_and_repair_config_drift(self):
        tags = replace(self.item, labels=("queue-a",))
        with self.writer() as conn:
            conflict = self.observe(conn, item=replace(tags, labels=("queue-a", "queue-b")))
            self.assertEqual(conflict.reason, "repository_conflict")
            started = self.observe(conn, item=tags, now=NEXT)
            request = started.request_ids[0]
        tighter = deepcopy(self.config)
        tighter["projects"][0]["rules"][0]["repair"]["ci"]["max_wall_minutes"] = 3
        with self.writer() as conn:
            current = self.observe(conn, item=tags, config=tighter, now=THIRD)
            decision = intake.revalidate_request(conn, tighter, self.ticket, request, revision=current.revision)
            self.assertEqual(decision.capability, "development")
        with self.writer() as conn:
            moved = self.observe(conn, item=replace(tags, labels=("queue-b",)), now=LAST)
            self.assertEqual(moved.reason, "confirmation_required")
            self.assertEqual(self.request(conn, request)["phase"], "paused")
        with self.writer() as conn:
            readonly = self.comment("9", created="2026-11-14 20:13:00")
            restored = self.observe(conn, (readonly,), item=tags, now="2026-11-14T12:14:00+00:00")
            self.assertEqual(self.request(conn, request)["phase"], "paused")
            self.assertEqual(len(restored.request_ids), 1)
            later = self.request(conn, restored.request_ids[0])
            self.assertEqual(json.loads(later["authorization_json"])["capability"], "readonly")
