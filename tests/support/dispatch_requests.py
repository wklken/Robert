import json

from tests.support.board_control_integration import BoardControlIntegrationFixture
from robert_agent.configuration.validation import validate_config
from robert_agent.work.application import dispatch_requests


class DispatchFixture(BoardControlIntegrationFixture):
    def setUp(self):
        super().setUp()
        raw = json.loads(self.config_path.read_text())
        raw["messaging"] = {
            "enabled": True,
            "send_after": "2026-01-01T00:00:00+00:00",
            "dispatch_enabled": True,
            "provider": "openclaw",
            "channel": "telegram",
            "account": "personal",
            "target": "42",
            "conversation": "telegram:42",
            "thread": "",
            "allowed_senders": ["42", "43"],
            "repos": ["example/repo"],
        }
        self.config_path.write_text(json.dumps(raw))
        self.config = validate_config(self.config_path, skip_external=True)
        self.identity = {
            key: raw["messaging"][key] for key in ("provider", "channel", "account", "conversation", "thread")
        }
        self.identity.update(protocol="robert.message.v1", sender_id="42")

    def draft(self):
        return dispatch_requests.handle(
            self.config, {**self.identity, "action": "new", "repo": "example/repo", "body": "调研启动慢的原因"}
        )

    def command(self, request_id, action="submit", **extra):
        return dispatch_requests.handle(
            self.config, {**self.identity, "action": action, "request_id": request_id, **extra}
        )
