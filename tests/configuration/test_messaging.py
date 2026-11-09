"""Strict opt-in messaging settings cannot silently grant another repository."""

import json
from robert_agent.configuration.validation import validate_config
from tests.support.board_control_integration import BoardControlIntegrationFixture


class MessagingConfigTests(BoardControlIntegrationFixture):
    def configure(self, messaging):
        raw = json.loads(self.config_path.read_text())
        raw["messaging"] = messaging
        self.config_path.write_text(json.dumps(raw))
        return validate_config(self.config_path, skip_external=True)

    def settings(self):
        return {
            "enabled": True,
            "send_after": "2026-11-07T00:00:00+00:00",
            "provider": "openclaw",
            "channel": "telegram",
            "account": "personal",
            "target": "42",
            "conversation": "telegram:42",
            "allowed_senders": ["42"],
            "repos": ["example/repo"],
        }

    def test_disabled_by_default_and_valid_settings_round_trip(self):
        self.assertFalse(validate_config(self.config_path, skip_external=True)["messaging"]["enabled"])
        result = self.configure(self.settings())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["messaging"], {**self.settings(), "thread": "", "dispatch_enabled": False})

    def test_incomplete_or_unscoped_settings_fail_closed(self):
        for key in ("channel", "account", "target", "conversation", "allowed_senders", "repos"):
            settings = self.settings()
            del settings[key]
            with self.subTest(key=key):
                self.assertFalse(self.configure(settings)["ok"])
        for key, value in [
            ("repos", ["unknown/repo"]),
            ("allowed_senders", []),
            ("allowed_senders", ["42", "42"]),
            ("provider", "hermes"),
            ("channel", " telegram"),
            ("token", "example"),
        ]:
            with self.subTest(key=key, value=value):
                self.assertFalse(self.configure({**self.settings(), key: value})["ok"])

    def test_dispatch_requires_explicit_messaging_enablement(self):
        self.assertTrue(self.configure({**self.settings(), "dispatch_enabled": True})["ok"])
        self.assertFalse(self.configure({**self.settings(), "enabled": False, "dispatch_enabled": True})["ok"])
        self.assertFalse(self.configure({**self.settings(), "dispatch_enabled": "true"})["ok"])

    def test_send_after_requires_an_explicit_timezone_and_survives_reload(self):
        settings = self.settings()
        del settings["send_after"]
        self.assertFalse(self.configure(settings)["ok"])
        for value in (None, "", "yesterday", "2026-11-07", "2026-11-07T00:00:00", " 2026-11-07T00:00:00Z"):
            with self.subTest(value=value):
                self.assertFalse(self.configure({**self.settings(), "send_after": value})["ok"])
        configured = self.configure({**self.settings(), "send_after": "2026-11-07T08:00:00+08:00"})
        self.assertTrue(configured["ok"], configured)
        self.assertEqual(configured["messaging"]["send_after"], "2026-11-07T00:00:00+00:00")
        self.assertEqual(validate_config(self.config_path, skip_external=True)["messaging"], configured["messaging"])
