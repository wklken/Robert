from unittest.mock import patch

from robert_agent.delivery.application import lane
from tests.support.publication_budget import PublicationBudgetFixture


class PublicationLaneTests(PublicationBudgetFixture):
    def test_configured_batch_reaches_background_publisher(self):
        for limit in (5, 10):
            with self.subTest(limit=limit):
                config = {
                    "ok": True,
                    "db_path": self.db,
                    "max_publish_actions": limit,
                    "repos": [{"full_name": "x/y", "github_account": "bot"}],
                }
                with (
                    patch.object(lane.validate_config, "validate_config", return_value=config),
                    patch(
                        "robert_agent.delivery.application.publish.publish_ready_actions", return_value={"ok": True}
                    ) as publish,
                ):
                    result = lane.publish("unused", dry_run=True)
                self.assertTrue(result["ok"], result)
                publish.assert_called_once_with(self.db, repo_id="repo", dry_run=True, limit=limit, config=config)

    def test_persisted_provider_cooldown_skips_github_budget_probe(self):
        from contextlib import closing
        from datetime import timedelta
        import json
        import sqlite3

        due = (self.now + timedelta(seconds=1800)).isoformat()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE github_actions SET metadata_json=json_set(metadata_json,'$.publish',json(?))",
                (json.dumps({"error_class": "rate_limited", "next_retry_at": due}),),
            )
        config = {
            "ok": True,
            "db_path": self.db,
            "repos": [{"full_name": "x/y", "github_account": "bot"}],
            "tapd": {"enabled": False},
        }
        with (
            patch.object(lane.validate_config, "validate_config", return_value=config),
            patch.object(lane.publication_values, "now", return_value=self.now.isoformat()),
            patch("robert_agent.delivery.application.notifications.deliver_pending"),
            patch.object(lane.receipt_notifications, "deliver_pending"),
            patch.object(lane.tapd_notifications, "deliver_pending"),
            patch.object(lane.github_budget, "budget") as budget,
            patch.object(lane.proposal_feedback, "deliver_pending") as feedback,
        ):
            result = lane.publish("unused")
        self.assertEqual(result["status"], "rate_limited", result)
        self.assertEqual(result["next_retry_at"], due)
        budget.assert_not_called()
        feedback.assert_not_called()

    def test_exhausted_write_budget_still_preserves_provider_cooldown(self):
        from contextlib import closing
        from datetime import timedelta
        import json
        import sqlite3

        due = (self.now + timedelta(seconds=1800)).isoformat()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE github_actions SET metadata_json=json_set(metadata_json,'$.publish',json(?))",
                (
                    json.dumps(
                        {
                            "error_class": "rate_limited",
                            "attempt_count": 3,
                            "retryable": False,
                            "next_retry_at": None,
                            "retry_not_before": due,
                        }
                    ),
                ),
            )
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(lane.action_queries.publication_cooldown_until(conn, "bot", self.now.isoformat()), due)

    def test_cooldown_does_not_block_another_configured_account(self):
        from contextlib import closing
        from datetime import timedelta
        import sqlite3

        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) VALUES ('other','x/other','other-bot','main','/repo','/worktrees')"
            )
        config = {
            "ok": True,
            "db_path": self.db,
            "repos": [
                {"full_name": "x/y", "github_account": "bot"},
                {"full_name": "x/other", "github_account": "other-bot"},
            ],
            "tapd": {"enabled": False},
        }
        due = (self.now + timedelta(seconds=120)).isoformat()
        with (
            patch.object(lane.validate_config, "validate_config", return_value=config),
            patch.object(
                lane.action_queries,
                "publication_cooldown_until",
                side_effect=lambda conn, account, now: due if account == "bot" else None,
            ),
            patch("robert_agent.delivery.application.notifications.deliver_pending"),
            patch.object(lane.receipt_notifications, "deliver_pending"),
            patch.object(lane.tapd_notifications, "deliver_pending"),
            patch.object(lane.github_read, "GitHubReadSession") as session,
            patch.object(lane.github_budget, "budget", return_value=True),
            patch.object(lane.proposal_feedback, "deliver_pending"),
            patch(
                "robert_agent.delivery.application.publish.publish_ready_actions", return_value={"ok": True}
            ) as publisher,
        ):
            result = lane.publish("unused")
        self.assertEqual(result["repo_count"], 1, result)
        self.assertEqual(publisher.call_args.kwargs["repo_id"], "other")
        self.assertEqual(session.call_args.kwargs["account"], "other-bot")
