from contextlib import closing
from dataclasses import replace
from pathlib import Path
import json
import subprocess
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from robert_agent.adapters.tapd.client import TapdItem
from robert_agent.persistence import database
from robert_agent.persistence.stores import proposals as store
from robert_agent.readmodels.proposals import read
from robert_agent.work import proposals as proposal_facts
from robert_agent.work.application import proposal_scan, proposal_sources


class ProposalScanTests(unittest.TestCase):
    def setUp(self):
        workspace = TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        root = Path(workspace.name)
        self.repo = root / "checkout"
        self.repo.mkdir()

        def git(*args):
            return subprocess.run(
                ["git", "-C", self.repo, *args],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

        git("init", "-b", "main")
        git("config", "user.name", "Robert fixture")
        git("config", "user.email", "test@example.test")
        (self.repo / "router.py").write_text("def resolve_route():\n    return 'old'\n")
        git("add", "router.py")
        git("commit", "-m", "Import route fixture")
        git("remote", "add", "upstream", str(self.repo))
        self.commit = git("rev-parse", "HEAD")
        self.db = root / "state.sqlite3"
        self.project = {
            "host": "tapd.example.test",
            "workspace_id": "101",
            "label_rules": [{"label": "router", "repository": "test/repo"}],
        }
        self.config = {
            "db_path": str(self.db),
            "data_dir": str(root),
            "repos": [
                {
                    "full_name": "test/repo",
                    "repo_root": str(self.repo),
                    "github_account": "robot",
                    "worktree_root": str(root / "worktrees"),
                    "default_base_branch": "main",
                },
            ],
            "tapd": {"projects": [self.project], "mention_name": "robert-test"},
            "proposer": {
                "enabled": True,
                "github_repositories": ["test/repo"],
                "github_assignees": ["alice"],
                "tapd_projects": ["tapd.example.test/101"],
                "tapd_assignees": ["alice"],
                "gateway_metadata": str(root / "fake-gateway.json"),
                "model": "example",
            },
        }
        self.issues = [
            self._issue(1, []),
            self._issue(2, [{"login": "alice"}, {"login": "bob"}]),
        ]
        self.tapd = Mock()
        self.tapd.recent_items.side_effect = lambda *args, **kwargs: (
            [self._ticket("12", ("alice",)), self._ticket("13", None)] if args[1] == "story" else []
        )
        self.tapd.terminal_states.return_value = {"closed": "Done"}
        self.tapd.ticket_comments.return_value = []
        self.assessment = {
            "decision": "propose",
            "kind": "implementation",
            "summary": "Router has a bounded fix.",
            "scope": "Update resolve_route.",
            "exclusions": "Other handlers.",
            "verification": "Assert resolved route.",
            "risks": "Check existing routes.",
            "evidence": [{"id": "code-1", "line": 1, "reason": "Route owner."}],
        }
        for target, value in (
            ("proposal_sources.issues.list_recent", self.issues),
            ("proposal_sources.issues.discussion", ()),
            ("model_gateway.gateway_endpoint", "http://127.0.0.1:7001"),
            ("tapd_client.client_for", self.tapd),
        ):
            patcher = patch(f"robert_agent.work.application.proposal_scan.{target}", return_value=value)
            self.addCleanup(patcher.stop)
            patcher.start()
        self.model = self.enterContext(
            patch(
                "robert_agent.work.application.proposal_scan.model_gateway.request_json", return_value=self.assessment
            )
        )

    def _issue(self, number, assignees):
        return {
            "id": number,
            "number": number,
            "state": "open",
            "title": "Fix resolve_route in router",
            "html_url": f"https://github.com/test/repo/issues/{number}",
            "assignees": assignees,
            "body": "The router has a wrong route.",
            "user": {"login": "author"},
            "created_at": "2026-11-20T01:03:00Z",
        }

    def _ticket(self, identifier, owners):
        return TapdItem(
            "story",
            identifier,
            "Fix resolve_route in router",
            "open",
            "router",
            "The router has a wrong route.",
            "1",
            False,
            None,
            "2026-11-20 09:03:00",
            owners,
        )

    def test_scheduled_discovery_persists_backlog_before_evaluation_and_keeps_it_after_a_week(self):
        self.config["proposer"].update(
            github_all_registered=True,
            schedule={"enabled": True, "analysis_limit": 1},
            tapd_todo_states={"story": ["open"]},
        )
        with patch.object(proposal_sources.issues, "has_open_pull_request", return_value=False):
            stats = proposal_scan.scan_daily(self.config, now="2026-11-22T01:00:00+00:00")
        pending = read(self.db, status="pending")
        self.assertEqual((stats["discovered"], stats["evaluated"]), (3, 1))
        self.assertEqual(len(pending), 3)
        self.assertEqual(len(read(self.db, status="today")), 1)
        self.tapd.get_item.return_value = self._ticket("12", ("alice",))
        self.tapd.recent_items.side_effect = None
        self.tapd.recent_items.return_value = []
        with (
            patch.object(proposal_sources.issues, "has_open_pull_request", return_value=False),
            patch.object(proposal_sources.issues, "list_recent", return_value=[]),
            patch.object(proposal_sources.issues, "get", side_effect=lambda _repo, number: self.issues[number - 1]),
        ):
            rep = proposal_scan.scan_daily(self.config, now="2026-12-02T01:00:00+00:00")
        self.assertEqual(rep["updated"], 1)
        self.assertEqual(len(read(self.db, status="today")), 2)
        self.assertEqual(len(read(self.db, status="pending")), 3)

    def test_scheduled_tapd_label_conflict_blocks_proposal_until_labels_are_fixed(self):
        self.config["proposer"].update(
            github_repositories=[],
            schedule={"enabled": True, "analysis_limit": 1},
            tapd_todo_states={"story": ["open"]},
        )
        self.project["label_rules"].append({"label": "backend", "repository": "test/backend"})
        self.config["repos"].append({**self.config["repos"][0], "full_name": "test/backend"})
        ticket = self._ticket("12", ("alice",))
        self.tapd.recent_items.return_value = []
        self.tapd.recent_items.side_effect = lambda _workspace, kind, **kwargs: (
            [replace(ticket, labels="router|backend")] if kind == "story" else []
        )
        self.model.side_effect = lambda **kwargs: (
            self.assessment if "code" in kwargs["evidence"] else {"label": "router", "reason": "Router"}
        )
        proposal_scan.scan_daily(self.config, now="2026-11-22T01:00:00Z")
        proposals = read(self.db)
        self.assertEqual([item["status"] for item in proposals], ["blocked"])
        (blocked,) = proposals
        self.assertIn("标签冲突", blocked["blocked_reason"])
        self.assertEqual(blocked["repository_candidates"], [])
        self.assertEqual(read(self.db, status="today"), [])
        self.model.assert_not_called()

        self.tapd.recent_items.side_effect = None
        self.tapd.get_item.return_value = ticket
        proposal_scan.scan_daily(self.config, now="2026-12-02T01:00:00Z")
        (pending,) = read(self.db, status="today")
        self.assertEqual(pending["proposal_id"], blocked["proposal_id"])
        self.assertEqual(pending["repository"], "test/repo")
        self.assertEqual(pending["report"]["feasibility"], "ready")

    def test_scheduled_proposal_stays_blocked_after_existing_formal_task_finishes(self):
        self.config["proposer"].update(schedule={"enabled": True, "analysis_limit": 10}, tapd_projects=[])
        with patch.object(proposal_sources.issues, "has_open_pull_request", return_value=False):
            proposal_scan.scan_daily(self.config, now="2026-11-22T01:00:00Z")
            with closing(sqlite3.connect(self.db)) as conn, conn:
                (source_key,) = conn.execute("SELECT source_key FROM proposals").fetchone()
                conn.execute(
                    "INSERT INTO workstreams(workstream_id,repo_id,lifecycle,active_task_id,created_at,updated_at) "
                    "VALUES (?,'repo:test/repo','active','formal','2026-11-22','2026-11-22')",
                    (source_key,),
                )
                conn.execute(
                    "INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) "
                    "VALUES ('formal',?,'running','2026-11-22','2026-11-22')",
                    (source_key,),
                )
            proposal_scan.scan_daily(self.config, now="2026-11-23T01:00:00Z")
            self.assertEqual([item["status"] for item in read(self.db)], ["blocked"])
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute("UPDATE tasks SET lifecycle='completed' WHERE task_id='formal'")
                conn.execute("UPDATE workstreams SET lifecycle='completed',active_task_id=NULL")
            proposal_scan.scan_daily(self.config, now="2026-11-24T01:00:00Z")
        self.assertEqual([item["status"] for item in read(self.db)], ["blocked"])
        self.assertEqual(read(self.db, status="today"), [])

    def test_scan_records_gateway_usage_when_assessment_fails_validation(self):
        def response(**kwargs):
            kwargs["on_usage"](
                {
                    "source": "gateway",
                    "usage_available": True,
                    "usage": {"input_tokens": 12},
                    "total_cost_usd": 0,
                }
            )
            return {"schema_version": "invalid"}

        self.model.side_effect = response
        report = proposal_scan.scan(self.config, now="2026-11-22T01:00:00+00:00")
        self.assertGreater(report["failed_evaluations"], 0)
        with closing(sqlite3.connect(self.db)) as conn:
            observed = conn.execute("SELECT usage_json FROM model_usage WHERE kind='proposer'").fetchall()
        self.assertEqual(len(observed), report["failed_evaluations"])
        self.assertTrue(all(json.loads(row[0])["usage"]["input_tokens"] == 12 for row in observed))

    def test_pinned_issues_and_tapd_tickets_become_deduplicated_proposals(self):
        report = proposal_scan.scan(self.config, now="2026-11-22T01:00:00+00:00")
        self.assertEqual(report["proposed"], 2, report)
        proposals = read(self.db)
        self.assertEqual({proposal["provider"] for proposal in proposals}, {"github", "tapd"})
        self.assertEqual({proposal["report"]["commit"] for proposal in proposals}, {self.commit})
        evidence = self.model.call_args.kwargs["evidence"]
        self.assertIn("def resolve_route", evidence["code"][0]["text"])
        self.assertNotIn("token", evidence)
        self.assertEqual(
            proposal_scan.scan(self.config, now="2026-11-22T02:00:00+00:00")["status"],
            "already_scanned",
        )
        self.assertEqual(proposal_scan.scan(self.config, now="2026-11-23T01:00:00+00:00")["proposed"], 0)
        self.assertEqual(self.model.call_count, 2)

    def test_unverified_model_evidence_never_reaches_proposal_page(self):
        self.assessment["evidence"][0]["line"] = 200
        report = proposal_scan.scan(self.config, now="2026-11-22T01:00:00+00:00")
        self.assertEqual(report["failed_evaluations"], 2, report)
        self.assertEqual(read(self.db), [])

    def test_blocked_candidate_rechecks_identical_evidence_after_discovery_window(self):
        first = proposal_scan.scan(self.config, now="2026-11-22T01:00:00+00:00")
        self.assertEqual(first["proposed"], 2)
        original = next(item for item in read(self.db) if item["provider"] == "github")
        with closing(database.connect(self.db)) as conn, conn:
            store.revise(
                conn,
                original["proposal_id"],
                original["version"],
                status="blocked",
                version=original["version"] + 1,
                fingerprint="",
            )
        self.tapd.recent_items.side_effect = None
        self.tapd.recent_items.return_value = ()
        with (
            patch.object(proposal_sources.issues, "list_recent", return_value=()),
            patch.object(proposal_sources.issues, "get", return_value=self.issues[0]),
        ):
            report = proposal_scan.scan(self.config, now="2026-12-02T01:00:00+00:00")
        self.assertEqual(report["proposed"], 1, report)
        current = read(self.db, proposal_id=original["proposal_id"])[0]
        self.assertEqual((current["status"], current["revision"]), ("pending", 2))

    def test_change_beyond_display_limit_changes_approval_facts(self):
        source_before = {**self.issues[0], "body": "a" * 7000 + "Analyze"}
        source_after = {**self.issues[0], "body": "a" * 7000 + "Change the API"}
        repo = self.config["repos"][0]
        before = proposal_facts.github_source(repo, source_before, [])
        after = proposal_facts.github_source(repo, source_after, [])
        self.assertEqual(before["description"], after["description"])
        self.assertNotEqual(before["content_digest"], after["content_digest"])
        with patch.object(
            proposal_sources.issues, "discussion", side_effect=[("a" * 650 + "Analyze",), ("a" * 650 + "Change",)]
        ):
            first = proposal_sources.discussion(before, {}, self.config["tapd"])
            second = proposal_sources.discussion(before, {}, self.config["tapd"])
        self.assertEqual(first["items"], second["items"])
        self.assertTrue(first["truncated"])
        self.assertNotEqual(first["digest"], second["digest"])
