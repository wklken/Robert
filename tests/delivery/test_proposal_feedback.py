from contextlib import closing
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from robert_agent.delivery.application import proposal_feedback, tapd_comments
from robert_agent.persistence import database
from robert_agent.persistence.queries.proposal_feedback import status
from robert_agent.work.application.web_setup import prepare_repositories
from robert_agent.work.proposal_feedback import record_comment, record_created


class ProposalFeedbackTests(unittest.TestCase):
    def setUp(self):
        root = TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.db_path = str(Path(root.name) / "store.db")
        self.config = {
            "data_dir": root.name,
            "db_path": self.db_path,
            "repos": [
                {
                    "full_name": "test/repo",
                    "github_account": "Robert",
                    "repo_root": root.name,
                    "worktree_root": root.name,
                    "default_base_branch": "main",
                }
            ],
            "proposer": {
                "enabled": True,
                "publish_comments": True,
                "github_repositories": ["test/repo"],
                "tapd_projects": ["tapd.example.test/101"],
            },
            "tapd": {
                "enabled": True,
                "mention_name": "robert-test",
                "projects": [{"host": "tapd.example.test", "workspace_id": "101"}],
            },
        }
        prepare_repositories(self.config)
        self.now = "2026-11-22T01:00:00+00:00"
        self.later = "2026-11-22T01:01:00+00:00"

    def _record(self, source):
        with closing(database.connect(self.db_path)) as conn, conn:
            record_created(conn, self.config["proposer"], "p-abc", source, self.now)

    def _receipt(self):
        with closing(database.connect(self.db_path)) as conn:
            return status(conn, "p-abc")[0]

    def test_github_unknown_write_is_reconciled_by_exact_comment_and_actor(self):
        url = "https://github.com/test/repo/issues/3"
        self._record({"provider": "github", "repository": "test/repo", "number": 3, "url": url})
        comments, writes = [], []

        def run(command, **kwargs):
            endpoint = command[2]
            if "--method" in command:
                writes.append(command)
                body = json.loads(kwargs["input"])["body"]
                self.assertIn("p-abc", body)
                self.assertNotIn("@Robert", body)
                comments.append({"body": body, "id": 47, "user": {"login": "robert"}})
                raise subprocess.TimeoutExpired(command, 30)
            if endpoint == "user":
                data = {"login": "robert"}
            elif endpoint.endswith("/3"):
                data = {
                    "id": 3,
                    "number": 3,
                    "html_url": url,
                    "title": "Proposal source",
                    "state": "open",
                    "created_at": self.now,
                    "assignees": [],
                    "user": {"login": "author"},
                }
            else:
                data = [comments]
            return subprocess.CompletedProcess(command, 0, json.dumps(data), "")

        self.assertEqual(proposal_feedback.deliver_pending(self.config, now=self.now, runner=run), {"pending": 1})
        self.assertEqual(proposal_feedback.deliver_pending(self.config, now=self.later, runner=run), {"sent": 1})
        self.assertEqual(len(writes), 1)
        receipt = self._receipt()
        self.assertEqual(json.loads(receipt["metadata_json"])["delivery"]["comment_id"], "47")

    def test_tapd_closed_ticket_skips_new_comment(self):
        self._record(
            {
                "provider": "tapd",
                "repository": "test/repo",
                "project_key": "tapd.example.test/101",
                "url": "https://tapd.example.test/101/prong/stories/view/31",
            }
        )
        client = Mock()
        client.ticket_comments.return_value = ()
        client.get_item.return_value = SimpleNamespace(status="done", archived=False, workitem_type_id="1")
        client.terminal_states.return_value = {"done": "Done"}
        publisher = tapd_comments.Publisher(self.config["tapd"], self.config["data_dir"], client=client)
        with patch(
            "robert_agent.delivery.application.proposal_feedback.tapd_comments.publisher_for", return_value=publisher
        ):
            result = proposal_feedback.deliver_pending(self.config, now=self.now)
        self.assertEqual(result, {"skipped": 1})
        client.add_comment.assert_not_called()

    def test_confirmed_start_updates_tapd_and_preserves_other_labels(self):
        source = {
            "provider": "tapd",
            "repository": "test/repo",
            "repository_label": "backend",
            "status": "pending",
            "owners": ["alice"],
            "project_key": "tapd.example.test/101",
            "url": "https://tapd.example.test/101/prong/stories/view/31",
        }
        self.config["tapd"]["projects"][0]["label_rules"] = [{"repository": "test/repo", "label": "backend"}]
        self.config["proposer"].update(
            tapd_started_states={"story": "progress"}, tapd_todo_states={"story": ["pending"]}
        )
        with closing(database.connect(self.db_path)) as conn, conn:
            for event in ("tag_sync", "status_sync"):
                record_comment(conn, event, event, "p-abc", 1, source, self.now)
        client = Mock()
        item = SimpleNamespace(status="pending", archived=False, labels="regression", owners=("alice",))
        client.get_item.return_value = item

        def update(*_target, status=None, labels=None):
            if status:
                item.status = status
            if labels:
                item.labels = "|".join(labels)

        client.update_proposal_ticket.side_effect = update
        publisher = tapd_comments.Publisher(self.config["tapd"], self.config["data_dir"], client=client)
        with patch.object(tapd_comments, "publisher_for", return_value=publisher):
            self.assertEqual(proposal_feedback.deliver_pending(self.config, now=self.now), {"sent": 2})
            self.assertEqual(proposal_feedback.deliver_pending(self.config, now=self.later), {})
        self.assertEqual(set(item.labels.split("|")), {"backend", "regression"})
        self.assertEqual(item.status, "progress")
        self.assertEqual(client.update_proposal_ticket.call_count, 2)
