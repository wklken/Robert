"""Proposal sources retain unbound candidates and exclude started work."""

from datetime import datetime, timezone
import unittest
from unittest.mock import Mock, patch

from robert_agent.adapters.tapd.client import TapdItem, TapdComment
from robert_agent.work.application import proposal_sources


class ProposalSourceTests(unittest.TestCase):
    def setUp(self):
        self.repo = {"full_name": "example/repo", "github_account": "robot"}
        self.project = {
            "host": "tapd.example.test",
            "workspace_id": "101",
            "label_rules": [{"label": "gateway", "repository": "example/repo"}],
        }
        self.config = {
            "data_dir": "/tmp/proposal-source-fixture",
            "repos": [self.repo],
            "tapd": {"projects": [self.project], "mention_name": "robert-test"},
            "proposer": {
                "schedule": {"enabled": True},
                "github_all_registered": True,
                "github_repositories": [],
                "github_assignees": [],
                "github_in_progress_labels": ["in progress", "in-progress"],
                "tapd_projects": ["tapd.example.test/101"],
                "tapd_assignees": ["alice"],
                "tapd_todo_states": {"story": ["open"], "bug": ["new"]},
            },
        }
        self.since = datetime(2026, 11, 15, tzinfo=timezone.utc)
        self.until = datetime(2026, 11, 22, tzinfo=timezone.utc)
        self.client = Mock()
        self.client.terminal_states.return_value = {"done": "Done"}
        self.client.recent_items.return_value = []

    def issue(self, number=12, **extra):
        return {
            "id": number + 100,
            "number": number,
            "state": "open",
            "title": "Fix gateway",
            "html_url": f"https://github.com/example/repo/issues/{number}",
            "body": "Details",
            "user": {"login": "alice"},
            "assignees": [{"login": "someone"}],
            "labels": [],
            "created_at": "2026-11-20T10:00:00Z",
            **extra,
        }

    def ticket(self, external_id="12", **extra):
        return TapdItem(
            **{
                "kind": "story",
                "external_id": external_id,
                "title": "Gateway bug",
                "status": "open",
                "labels": None,
                "description": "Details",
                "workitem_type_id": "1",
                "archived": False,
                "url": None,
                "created": "2026-11-20 10:00:00",
                "owners": (),
                **extra,
            }
        )

    def discover(self, issues=()):
        stats = {"failed_scopes": [], "failed_evaluations": 0}
        with (
            patch.object(proposal_sources.issues, "list_recent", return_value=issues),
            patch.object(proposal_sources.issues, "has_open_pull_request", return_value=False, create=True),
            patch.object(proposal_sources.tapd_client, "client_for", return_value=self.client),
        ):
            rows = list(proposal_sources.discover(self.config, self.since, self.until, stats, {}))
        self.assertEqual(stats["failed_scopes"], [])
        return rows

    def test_all_registered_repositories_allow_assigned_issues_but_exclude_in_progress(self):
        found = self.discover([self.issue(), self.issue(13, labels=[{"name": "In Progress"}])])
        self.assertEqual([row["number"] for row in found], [12])

    def test_unlabelled_tapd_candidates_keep_identity_and_configured_repository_choices(self):
        self.client.recent_items.side_effect = lambda workspace, kind, **kwargs: (
            [self.ticket()] if kind == "story" else []
        )
        found = self.discover()
        self.assertEqual(len(found), 1)
        self.assertIsNone(found[0]["repository"])
        self.assertIsNone(found[0]["repository_label"])
        self.assertEqual(found[0]["identity"], "tapd:tapd.example.test/101/story/12")
        self.assertEqual(found[0]["repository_candidates"], [{"repository": "example/repo", "label": "gateway"}])

    def test_unknown_owners_other_owners_and_started_tapd_items_are_excluded(self):
        self.client.recent_items.side_effect = lambda workspace, kind, **kwargs: (
            [
                self.ticket("12", owners=None),
                self.ticket("13", owners=("bob",)),
                self.ticket("14", status="processing"),
                self.ticket("15", owners=("alice",)),
            ]
            if kind == "story"
            else []
        )
        self.assertEqual([row["external_id"] for row in self.discover()], ["15"])

    def test_linked_open_pull_request_excludes_issue_and_lookup_failure_marks_scope_partial(self):
        stats = {"failed_scopes": [], "failed_evaluations": 0}
        with (
            patch.object(proposal_sources.issues, "list_recent", return_value=[self.issue()]),
            patch.object(proposal_sources.issues, "has_open_pull_request", return_value=True, create=True),
            patch.object(proposal_sources.tapd_client, "client_for", return_value=self.client),
        ):
            self.assertEqual(list(proposal_sources.discover(self.config, self.since, self.until, stats, {})), [])
        with (
            patch.object(proposal_sources.issues, "list_recent", return_value=[self.issue()]),
            patch.object(
                proposal_sources.issues, "has_open_pull_request", side_effect=ValueError("partial"), create=True
            ),
            patch.object(proposal_sources.tapd_client, "client_for", return_value=self.client),
        ):
            self.assertEqual(list(proposal_sources.discover(self.config, self.since, self.until, stats, {})), [])
        self.assertEqual(stats["failed_scopes"], ["github:example/repo"])

    def test_refresh_binds_only_missing_label_and_rejects_conflicting_observed_routes(self):
        source = {"provider": "tapd", "project_key": "tapd.example.test/101", "kind": "story", "external_id": "12"}
        self.client.get_item.return_value = self.ticket()
        self.client.ticket_comments.return_value = []
        clients = {"tapd.example.test/101": self.client}
        current, snapshot = proposal_sources.refresh(
            self.config,
            self.repo,
            source,
            clients=clients,
            selected_repository="example/repo",
        )
        self.assertEqual(current["repository"], "example/repo")
        self.assertTrue(current["routing_unconfirmed"])
        self.assertEqual(snapshot["repository_label"], "gateway")
        self.project["label_rules"].append({"label": "other", "repository": "example/other"})
        for labels in ("other", "gateway|other"):
            with self.subTest(labels=labels):
                self.client.get_item.return_value = self.ticket(labels=labels)
                self.assertEqual(
                    proposal_sources.refresh(
                        self.config,
                        self.repo,
                        source,
                        clients=clients,
                        selected_repository="example/repo",
                    ),
                    (None, None),
                )

    def test_scheduler_discussion_includes_investigation_results_and_filters_proposal_receipts(self):
        source = {"provider": "tapd", "project_key": "tapd.example.test/101", "kind": "story", "external_id": "12"}
        self.client.ticket_comments.return_value = [
            TapdComment(
                "1", "story", "12", "robot", "robert-test：\n\nRoot cause found.\n\n回执 abcdef1234567890", "now", None
            ),
            TapdComment(
                "2",
                "story",
                "12",
                "robot",
                "robert-test：\n\nRobert 提案 p1 已受理。\n\n回执 abcdef1234567890",
                "now",
                None,
            ),
        ]
        result = proposal_sources.discussion(
            source,
            {"tapd.example.test/101": self.client},
            self.config["tapd"],
            include_automatic=True,
        )
        self.assertEqual(len(result["items"]), 1)
        self.assertIn("Root cause found", result["items"][0])
