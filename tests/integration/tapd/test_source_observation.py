"""Exercise TAPD discovery against real durable intake state."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
import unittest

from robert_agent.adapters.tapd.client import TapdClient, TapdError
from robert_agent.foundation import values
from robert_agent.persistence import database, transactions
from robert_agent.persistence.schema.database import init_database
from robert_agent.work import tapd_authority as facts
from robert_agent.work.application import source_observation as scanning, tapd_intake

WORKSPACE = "001"
HOST = "tapd.example"
STORY = "90000000000000000001"
BUG = "90000000000000000002"
REMARK = "90000000000000000003"


class SourceObservationTests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.db = self.root / "native.sqlite"
        init_database(self.db)
        self.project = {
            "workspace_id": WORKSPACE,
            "trusted_author": "trusted-fixture",
            "repositories": ["org/fixture"],
            "mentions": {"enabled": True, "activated_after": "2026-11-01T00:00:00+00:00"},
            "rules": [
                {
                    "id": "alpha",
                    "assignee": "team-fixture",
                    "label": "label-fixture",
                    "repo": "org/fixture",
                    "kinds": ["bug"],
                },
            ],
        }
        self.config = {
            "repos": [{"full_name": "org/fixture"}],
            "tapd": {
                "enabled": True,
                "host": HOST,
                "projects": [self.project],
                "executable": "/configured/tapd",
                "cwd": str(self.root),
                "bot_name": "rbt-custom",
                "reply_name": "reply-custom",
                "api_timezone": "UTC",
            },
        }
        self.sets = {"stories": STORY, "bug": BUG, "bug_remark": REMARK}
        self.shown = self.sets.copy()
        self.calls = []
        self.error = None
        self.raw_history = []
        self.created = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("""
                INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root)
                VALUES('repo','org/fixture','bot','main','/repo','/trees')
            """)

    def read(self, sql, params=()):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute(sql, params).fetchall()

    def ticket(self, key):
        return facts.TicketIdentity(HOST, WORKSPACE, "story" if key == "stories" else "bug", self.sets[key])

    def comment(self, entry_type, ticket, identifier="01", *, created=None, body=None):
        return {
            "id": identifier,
            "workspace_id": WORKSPACE,
            "entry_type": entry_type,
            "entry_id": ticket,
            "description": body or "<p>@rbt-custom User asks for analysis</p>",
            "author": self.project["trusted_author"],
            "created": created or self.created,
            "modified": None,
            "root_id": "0",
            "reply_id": "0",
        }

    def item(self, identifier):
        return {
            "id": identifier,
            "workspace_id": WORKSPACE,
            "name": "Story",
            "title": "Bug",
            "description": "Full ticket body",
            "status": "open",
            "owner": self.project["rules"][0]["assignee"] if self.project["rules"] else None,
            "label": "label-fixture",
            "workitem_type_id": "052",
        }

    def factory(self, **kwargs):
        return TapdClient(**kwargs, runner=self.cli)

    def cli(self, command, **opts):
        args = command[4:]
        self.calls.append((args, opts))
        entity, operation = args[:2]
        if self.error:
            self.error(entity, operation, args, opts)
        if operation == "list":
            page = int(args[-3])
            limit = int(args[-1])
            if entity == "comment":
                entry = next(flag.split("=", 1)[1] for flag in args if flag.startswith("--entry-type="))
                requested = next((flag.split("=", 1)[1] for flag in args if flag.startswith("--entry-id=")), None)
                owner = self.shown.get(entry)
                rows = [self.comment(entry, owner)] if page == 1 and owner and requested in {None, owner} else []
                if requested is not None and page == 1:
                    rows.extend(
                        row for row in self.raw_history if row["entry_type"] == entry and row["entry_id"] == requested
                    )
            else:
                rows = [self.item(BUG)] if page == 1 else []
            response = {"page": page, "limit": limit, "items": rows, "total": 999}
        elif operation == "show":
            response = self.item(args[2])
        else:
            self.assertEqual(args[:2], ["workflow", "last-steps"])
            response = {"closed": "Closed"}
        return subprocess.CompletedProcess(command, 0, json.dumps(response).encode(), b"")

    def provider(self, client, ticket, *, project, budget, limits, stop):
        # The test's independent provenance is explicit and binds these exact
        # fixture bytes, never a display-author promotion in production.
        item = client.get_item(ticket.workspace_id, ticket.kind, ticket.external_id, timeout=budget.remaining())
        comments = client.list_comments(
            ticket.workspace_id, ticket.kind, ticket.external_id, **limits.pages(budget, stop)
        )
        workflow = client.terminal_states(ticket.workspace_id, ticket.kind, workitem_type_id=item.workitem_type_id)
        label = ("label-fixture",) if ticket.external_id == BUG else ()
        materialized = []
        for row in comments.items:
            raw = facts.CommentObservation.from_cli(ticket, row)
            creator = facts.CreatorEvidence(raw.version, project["trusted_author"], "fixture-creator")
            own = facts.OwnBodyEvidence(raw.version, project["trusted_author"], "fixture-own")
            materialized.append(facts.CommentObservation(raw.version, raw.body_html, raw.display_author, creator, own))
        return facts.TicketObservation(
            facts.TicketFacts(
                ticket, item.title, item.description, item.status, ("team-fixture",), label, False, tuple(workflow)
            ),
            values.utc_now(),
            tuple(materialized),
            "complete",
        )

    def scan(self, *, provider=True, **kw):
        return scanning.scan_sources(
            self.db,
            self.config,
            client_factory=self.factory,
            observation_provider=self.provider if provider else None,
            **kw,
        )

    def test_disabled_and_dry_run_skip_provider(self):
        def unexpected(**_kwargs):
            self.fail("TAPD provider must not be called")

        for enabled, dry_run in ((False, False), (False, True), (True, True)):
            with self.subTest(enabled=enabled, dry_run=dry_run):
                outcome = scanning.scan_sources(
                    self.db,
                    {"tapd": {"enabled": enabled, "projects": []}},
                    dry_run=dry_run,
                    client_factory=unexpected,
                )
                self.assertEqual(outcome.status, "dry_run" if dry_run else "disabled")
        self.assertEqual(self.read("SELECT COUNT(*) FROM sources"), [(0,)])

    def test_paginated_tag_and_all_comment_streams_drive_full_real_intake(self):
        self.raw_history.append(
            self.comment("bug", BUG, "02", created="2020-01-01 00:00:00", body="Historical tag discussion")
        )
        outcome = self.scan()
        self.assertEqual(
            (outcome.status, outcome.probes_completed, outcome.candidates, outcome.tickets_checked),
            ("observed", 2, 3, 3),
        )
        self.assertEqual(outcome.requests, 3)
        rows = self.read("SELECT external_id,source_type FROM sources ORDER BY external_id")
        self.assertEqual(rows, [(STORY, "story"), (BUG, "bug"), (REMARK, "bug")])
        self.assertEqual(
            self.read("SELECT auth_type, COUNT(*) FROM ingress_requests GROUP BY auth_type ORDER BY auth_type"),
            [("label_queue", 1), ("tapd_mention", 2)],
        )
        snapshot = self.read(
            "SELECT payload_json FROM source_events WHERE source_id=?", (self.ticket("bug").source_id,)
        )
        self.assertTrue(any("Historical tag discussion" in row[0] for row in snapshot))
        tag_args = [args for args, _ in self.calls if args[:2] == ["bug", "list"]]
        self.assertEqual([args[-3] for args in tag_args], ["1", "2"])
        self.assertTrue(all({"--owner=team-fixture", "--label=label-fixture"} <= set(args) for args in tag_args))
        discovery = [args for args, _ in self.calls if "--author=trusted-fixture" in args]
        self.assertEqual(
            {flag for args in discovery for flag in args if flag.startswith("--entry-type=")},
            {"--entry-type=stories", "--entry-type=bug", "--entry-type=bug_remark"},
        )
        self.assertEqual([args[-3] for args in discovery], ["1", "2"] * 3)

    def test_default_raw_display_and_body_do_not_grant_authority(self):
        outcome = self.scan(provider=False)
        self.assertEqual(outcome.status, "attention")
        self.assertEqual(outcome.requests, 0)
        self.assertTrue(any(item.reason == "provider_capability_unverified" for item in outcome.attention))
        for (document,) in self.read("SELECT metadata_json FROM sources"):
            source = json.loads(document)
            self.assertEqual(source["coverage"], "incomplete")
            self.assertIsNone(source["ticket"]["archived"])
        for (document,) in self.read("SELECT payload_json FROM source_events"):
            event = json.loads(document)["latest"]
            self.assertEqual(event["display_author"], self.project["trusted_author"])
            self.assertIsNone(event["creator"])
            self.assertIsNone(event["own_body_evidence"])

    def test_limit_records_attention_and_known_root_refreshes_fairly(self):
        first = self.scan(limits=scanning.ScanLimits(tickets=1))
        self.assertEqual((first.candidates, first.tickets_checked), (3, 1))
        known = self.read("SELECT source_id FROM tapd_observation_progress")[0][0]
        self.calls.clear()
        later = self.scan(limits=scanning.ScanLimits(tickets=2, max_pages=1))
        self.assertEqual((later.probes_completed, later.tickets_checked), (0, 2))
        self.assertTrue(any(item.reason == "scan_limit" for item in later.attention))
        # Both unrefreshed candidates are visited before the earlier ticket even
        # though discovery itself could not complete.
        self.assertEqual(len(self.read("SELECT source_id FROM tapd_observation_progress")), 3)
        self.assertTrue(known in {row[0] for row in self.read("SELECT source_id FROM tapd_observation_progress")})
        self.assertEqual(
            len(self.read("SELECT completed_at FROM tapd_discovery_progress WHERE completed_at IS NOT NULL")), 2
        )

    def test_known_root_remains_visible_without_tag_or_comment_discovery_and_missing_is_not_absence(self):
        self.scan()
        self.shown.clear()

        def no_tag(entity, operation, _args, _opts):
            if entity == "bug" and operation == "list":
                raise TapdError("timeout")

        self.error = no_tag
        result = self.scan()
        self.assertEqual(result.tickets_checked, 3)
        self.assertEqual(
            self.read("SELECT phase FROM ingress_requests WHERE source_id=?", (self.ticket("stories").source_id,)),
            [("held",)],
        )
        self.assertEqual(self.read("SELECT COUNT(*) FROM request_events WHERE state='revoked'"), [(0,)])

    def test_removed_project_pauses_existing_round_without_provider_io(self):
        self.scan()
        ticket = self.ticket("bug")
        callback = scanning.make_source_refresh(
            self.db,
            self.config,
            owner="test",
            client_factory=self.factory,
            observation_provider=self.provider,
        )
        refresh = callback(ticket)
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            now = values.utc_now()
            observed = tapd_intake.observe_ticket(
                conn, self.config["tapd"], refresh.observation, scanning.publication_facts(conn, ticket), now=now
            )
            self.assertIsNotNone(
                tapd_intake.admit_round(
                    conn,
                    self.config["tapd"],
                    ticket,
                    refresh.claim,
                    round_id="round",
                    revision=observed.revision,
                    now=now,
                )
            )
        self.config["tapd"]["projects"] = []
        self.calls.clear()
        result = self.scan()
        self.assertEqual(self.calls, [])
        self.assertEqual(result.paused_sources, 3)
        self.assertEqual(self.read("SELECT reason FROM source_round_cancellations"), [("source_project_removed",)])
        self.assertEqual(self.read("SELECT DISTINCT phase FROM ingress_requests"), [("paused",)])
        self.assertEqual(self.read("SELECT DISTINCT status FROM source_rounds"), [("active",)])
        self.assertEqual(self.read("SELECT DISTINCT state FROM request_events"), [("pending",)])

    def test_removed_rule_refresher_has_no_io_and_claim_callback_rejects_stale_packet(self):
        self.scan()
        start = values.utc_now()
        callback = scanning.make_source_refresh(
            self.db,
            self.config,
            owner="test",
            client_factory=self.factory,
            observation_provider=self.provider,
        )
        story = self.ticket("stories")
        refreshed = callback(story)
        self.assertGreaterEqual(refreshed.observation.observed_at, start)
        self.assertEqual(refreshed.claim.source_id, story.source_id)
        self.assertEqual(
            self.read("SELECT owner FROM source_sessions WHERE source_id=?", (story.source_id,)), [("test",)]
        )
        old = scanning.make_source_refresh(
            self.db,
            self.config,
            owner="test",
            client_factory=self.factory,
            observation_provider=lambda *a, **kw: facts.TicketObservation(
                refreshed.observation.facts, "2000-01-01T00:00:00Z"
            ),
        )
        with self.assertRaisesRegex(ValueError, "stale_observation"):
            old(story)
        self.project["rules"][0]["kinds"] = ["story"]
        self.calls.clear()
        with self.assertRaisesRegex(ValueError, "outside current configuration"):
            callback(self.ticket("bug"))
        self.assertEqual(self.calls, [])

    def test_first_failed_snapshot_cannot_authorize_and_recovers(self):
        def unavailable(_client, _ticket, **_kwargs):
            raise TapdError("timeout")

        held = scanning.scan_sources(
            self.db,
            self.config,
            client_factory=self.factory,
            observation_provider=unavailable,
        )
        self.assertEqual(held.requests, 0)
        self.assertEqual(self.read("SELECT count(*) FROM sources"), [(3,)])
        with closing(database.connect(self.db)) as conn:
            authority = tapd_intake.current_label_authority(
                conn,
                self.config["tapd"],
                self.ticket("bug"),
                revision=None,
            )
        self.assertEqual(authority.reason, "context_unavailable")
        self.assertEqual(self.scan().requests, 3)

    def test_slow_discovery_cannot_exhaust_known_root_budget(self):
        from unittest.mock import patch

        self.scan()
        self.calls.clear()
        real_client = self.factory(
            executable="/configured/tapd",
            cwd=self.root,
            environment={},
        )
        clock = [50]

        def exhaust(*_args, **options):
            clock[0] += options["scan_timeout"] + 0.01
            raise TapdError("timeout")

        with (
            patch("robert_agent.work.application.source_observation.time.monotonic", side_effect=lambda: clock[0]),
            patch.object(real_client, "list_items", side_effect=exhaust),
            patch.object(real_client, "discover_comments", side_effect=exhaust),
        ):
            outcome = scanning.scan_sources(
                self.db,
                self.config,
                client_factory=lambda **_kwargs: real_client,
                observation_provider=self.provider,
                limits=scanning.ScanLimits(seconds=10),
            )
        self.assertEqual((outcome.probes_completed, outcome.tickets_checked), (0, 3))
        self.assertTrue(any(args[1] == "show" for args, _ in self.calls))

    def test_late_visible_comment_after_activation_is_eligible(self):
        self.scan()
        self.raw_history.append(self.comment("stories", STORY, "0123"))
        self.assertEqual(self.scan().requests, 1)
        self.assertEqual(
            self.read("SELECT count(*) FROM ingress_requests WHERE source_id=?", (self.ticket("stories").source_id,)),
            [(2,)],
        )
