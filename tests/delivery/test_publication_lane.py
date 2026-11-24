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
