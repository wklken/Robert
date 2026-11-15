"""Exercise attention through an audited native publication, using local Git."""

from contextlib import closing
import json
from pathlib import Path
import subprocess
from unittest.mock import patch
from urllib.parse import unquote


from robert_agent.delivery import native_contract, tapd_policy
from robert_agent.delivery.application.source_github_publication import publish_github_action
from robert_agent.execution import workspace
from robert_agent.execution.notifications import has_pending_local_delivery
from robert_agent.foundation.values import utc_now
from robert_agent.persistence import database
from robert_agent.persistence.stores import notifications
from robert_agent.work.application import messaging
from tests.support.source_cycle import SourceCycleFixture


class SourcePublicationAttentionTests(SourceCycleFixture):
    def test_unknown_native_push_opens_one_notification_until_independent_readback(self):
        self.config(development=True)
        repository = Path(self.configured["repos"][0]["repo_root"])
        self.git(repository, "init", "-b", "main")
        self.git(repository, "config", "user.name", "Repository Bot")
        self.git(repository, "config", "user.email", "repo-bot@example.com")
        self.git(repository, "remote", "add", "upstream", "https://github.com/org/alpha.git")
        self.git(repository, "remote", "add", "origin", "https://github.com/bot/alpha.git")
        self.git(repository, "add", "README.md")
        self.git(repository, "commit", "-m", "Prior existing commit")
        base = self.git(repository, "rev-parse", "HEAD")
        self.git(repository, "update-ref", "refs/remotes/upstream/main", base)
        self.tick("admitted", ticket=self.ticket)
        self.tick("prepared")
        self.tick("dispatched")
        self.submit("classification_result", recommended_route="new-pr")
        self.tick("waiting")
        self.tick("audited")

        def prepare(repo, purpose, *, pr=None):
            self.assertIsNone(pr)
            branch = f"codex/dd-{purpose['workspace_key']}-source-work"
            target = Path(repo["worktree_root"]) / "prepared"
            target.parent.mkdir(parents=True)
            self.git(repository, "worktree", "add", "-b", branch, str(target), base)
            return {"ok": True, "worktree_path": str(target), "branch_name": branch}

        with patch.object(workspace, "prepare_source_worktree", side_effect=prepare):
            prepared = self.tick("prepared")
        [[tree, branch]] = self.read(
            "SELECT worktree_path,branch_name FROM attempts WHERE task_id=?", (prepared.identity,)
        )
        self.tick("dispatched")
        (Path(tree) / "README.md").write_text("Corrected repository behavior")
        self.git(tree, "add", "README.md")
        self.git(tree, "commit", "-m", tapd_policy.NATIVE_COMMIT_MESSAGE)
        head = self.git(tree, "rev-parse", "HEAD")
        actions = native_contract.selectors_for("new_pr")
        actions[-1]["body"] = "Updated README.md"
        self.submit(
            "new_pr",
            planned_actions=actions,
            verification=[
                {
                    "command": ["pytest"],
                    "source": "worker_reported",
                    "exit_code": 0,
                    "status": "passed",
                    "required": True,
                    "purpose": "local test",
                },
            ],
        )
        self.tick("waiting")
        self.tick("audited")
        root = self.tick("publication_pending").identity
        [[action]] = self.read("SELECT action_id FROM github_actions WHERE action_type='push_existing_pr'")
        writes = []
        state = {"reveal": False}

        def run(args, **options):
            if args[:2] == ["git", "push"]:
                writes.append(args)
                self.assertEqual(args[-1], f"{head}:refs/heads/{branch}")
                raise subprocess.TimeoutExpired(args, 20)
            if args[:3] == ["gh", "pr", "create"]:
                writes.append(args)
                raise subprocess.TimeoutExpired(args, 20)
            if args[:4] == ["gh", "api", "--hostname", "github.com"]:
                path = unquote(args[4])
                if path == "user":
                    value = {"login": "bot"}
                elif path == "repos/org/alpha/git/matching-refs/heads/main":
                    value = [{"ref": "refs/heads/main", "object": {"type": "commit", "sha": base}}]
                elif path == f"repos/bot/alpha/git/matching-refs/heads/{branch}":
                    value = (
                        [{"ref": f"refs/heads/{branch}", "object": {"type": "commit", "sha": head}}]
                        if state["reveal"]
                        else []
                    )
                elif path.startswith("repos/org/alpha/pulls?"):
                    value = []
                else:
                    self.fail(f"Unexpected remote read {args}")
                return subprocess.CompletedProcess(args, 0, json.dumps(value), "")
            self.assertEqual(args[0], "git", args)
            return subprocess.run(args, **options)

        def publish(action_id=action):
            return publish_github_action(
                self.db,
                self.configured,
                source_result_id=root,
                action_id=action_id,
                owner="test-coordinator",
                run_command=run,
                observation_provider=self.provider,
                tapd_client_factory=self.factory,
            )

        first = publish()
        self.assertEqual(first.status, "unknown", first)
        [notice] = self.read(
            "SELECT notification_id,status FROM notifications WHERE notification_type='source_publication_attention'"
        )
        self.assertEqual(notice[1], "recorded")
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
            "allowed_senders": ["42"],
            "send_after": "2026-07-01T00:00:00Z",
            "target": "42",
            "repos": ["org/alpha"],
            "tapd_projects": [{"host": self.ticket.host, "workspace_id": self.ticket.workspace_id}],
        }
        self.configured["tapd"]["projects"][0]["operators"] = ["openclaw:telegram:test-account:42"]
        with closing(database.connect(self.db)) as conn, conn:
            for index in range(101):
                notifications.record_notification(
                    conn,
                    f"forged-{index}",
                    None,
                    "source_publication_attention",
                    "local",
                    "recorded",
                    "2029-11-17T00:00:00Z",
                    json.dumps({"source_attention_id": notice[0]}) if index % 2 else "{}",
                )
        [shown] = messaging.inbox(self.configured, actor)["messages"]
        self.assertEqual(shown["notification_id"], notice[0])
        self.assertEqual(shown["target"]["repo_id"], "repo:org/alpha")
        self.assertEqual(shown["target"]["publication_provider"], "github")
        self.assertNotIn("Corrected repository behavior", shown["summary"])
        with closing(database.connect(self.db)) as conn:
            self.assertTrue(has_pending_local_delivery(conn, ["repo:org/alpha"], utc_now(), self.configured))
            self.assertFalse(has_pending_local_delivery(conn, ["repo:org/beta"], utc_now(), self.configured))
        self.assertEqual(publish().status, "unknown")
        self.assertEqual(
            self.read("SELECT notification_id,status FROM notifications WHERE notification_id=?", (notice[0],)),
            [notice],
        )
        state["reveal"] = True
        self.assertEqual(publish().status, "published")
        self.assertEqual(len(writes), 1)
        self.assertEqual(
            self.read("SELECT notification_id,status FROM notifications WHERE notification_id=?", (notice[0],)),
            [(notice[0], "resolved")],
        )

        [[open_action]] = self.read("SELECT action_id FROM github_actions WHERE action_type='open_pr'")
        outcome = publish(open_action)
        self.assertEqual(outcome.status, "unknown", outcome)
        self.assertEqual(publish().status, "deduplicated")
        self.assertEqual(len(writes), 2)
        self.assertEqual(
            self.read("SELECT action_id,resolved_at IS NULL FROM source_publication_attentions ORDER BY created_at"),
            [(action, 0), (open_action, 1)],
        )
