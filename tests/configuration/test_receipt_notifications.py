from tests.support.config_v2 import ConfigV2Fixture


class ReceiptNotificationConfigTests(ConfigV2Fixture):
    def setUp(self):
        super().setUp()
        self.config["messaging"] = {
            "enabled": True,
            "send_after": "2026-11-01T00:00:00Z",
            "channel": "telegram",
            "account": "personal",
            "target": "123",
            "conversation": "telegram:123",
            "allowed_senders": ["123"],
            "repos": ["example/test"],
        }

    def test_receipt_notifications_are_opt_in_and_default_to_key_deliveries(self):
        self.config["messaging"]["receipts"] = {"enabled": True, "send_after": "2026-11-22T09:00:00+08:00"}
        result = self.validate()
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            result["messaging"]["receipts"],
            {
                "enabled": True,
                "send_after": "2026-11-22T01:00:00+00:00",
                "events": ["pr_opened", "report_delivered"],
                "chromium": "chromium",
            },
        )

    def test_receipt_events_can_be_changed_without_enabling_other_milestones(self):
        self.config["messaging"]["receipts"] = {
            "enabled": True,
            "send_after": "2026-11-22T09:00:00+08:00",
            "events": ["pr_opened", "pr_merged"],
        }
        result = self.validate()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["messaging"]["receipts"]["events"], ["pr_opened", "pr_merged"])
        self.config["messaging"]["receipts"]["events"] = ["typo"]
        self.assertFalse(self.validate()["ok"])

    def test_receipt_delivery_requires_an_explicit_activation_time_and_messaging(self):
        self.config["messaging"]["receipts"] = {"enabled": True}
        self.assertIn("messaging.receipts.send_after", self.validate()["safe_error"])
        self.config["messaging"]["receipts"]["send_after"] = "2026-11-22T09:00:00+08:00"
        self.config["messaging"]["enabled"] = False
        self.assertIn("messaging.receipts.enabled requires messaging.enabled", self.validate()["safe_error"])
