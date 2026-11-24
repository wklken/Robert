"""Generated WeCom RPC, durable queue and authenticated quoted replies agree."""

from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import unittest
from unittest.mock import patch

from robert_agent.delivery.application import notifications
from robert_agent.execution import notifications as execution_notifications
from robert_agent.integrations.openclaw import write_plugin
from robert_agent.persistence import database
from robert_agent.persistence.stores import notifications as notification_store
from robert_agent.work.application import messaging
from robert_agent.work.errors import WorkItemValidationError
from tests.support.work_items import WorkItemCommandFixture, example_choices


@unittest.skipUnless(shutil.which("node"), "Node is required to execute the OpenClaw plugin")
class WeComQuestionDeliveryTests(WorkItemCommandFixture):
    def setUp(self):
        super().setUp()
        self.identity = {
            "protocol": "robert.message.v1",
            "provider": "openclaw",
            "channel": "wecom",
            "account": "default",
            "conversation": "wecom:operator",
            "thread": "",
            "sender_id": "owner",
        }
        self.config = {
            "db_path": str(self.db_path),
            "workers": [{"name": "default"}],
            "repos": [{"full_name": "example/repo"}],
            "messaging": {
                **{key: value for key, value in self.identity.items() if key not in {"protocol", "sender_id"}},
                "enabled": True,
                "contextual_replies": True,
                "target": "operator",
                "send_after": "2026-07-18T00:00:00+00:00",
                "allowed_senders": ["owner"],
                "repos": ["example/repo"],
            },
        }
        item = self._create(start=True)["item"]
        question = self._question(item["work_item_id"], 1)
        self.notice = "attention-" + question["event_id"]
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE work_item_events SET metadata_json=json_set(metadata_json, '$.choices', json(?)) WHERE event_id=?",
                (json.dumps(example_choices()), question["event_id"]),
            )
        root = Path(self.tmp.name)
        self.node = shutil.which("node")
        write_plugin(root / "plugin")
        self.effects = root / "effects.jsonl"
        self.script = root / "plugin/send.mjs"
        self.script.write_text("""import { appendFileSync } from 'node:fs';
import { registerWeComQuestionTransport } from './wecom.js';
const params = JSON.parse(process.argv[2]), outcome = process.argv[3];
const effect = value => appendFileSync(process.argv[4], JSON.stringify(value) + '\\n');
let handler;
registerWeComQuestionTransport({ registerGatewayMethod: (_, fn) => handler = fn,
  runtime: { channel: { outbound: { loadAdapter: async () => ({ sendText: async p => {
    effect({ kind: 'text', text: p.text, target: p.to, account: p.accountId });
    return { messageId: 'body-1' }; } }) } } } });
globalThis[Symbol.for('wecom-openclaw-plugin:shared-state:v1')] = { wsClientInstances: new Map([
  ...['default', 'another'].map(account => [account, {
    isConnected: outcome !== 'disconnected', sendMessage: async (target, card) => {
    effect({ kind: 'card', target, account, card });
    if (outcome === 'failed') throw Object.assign(new Error('rejected'), { errcode: 40008 });
    if (outcome === 'rejected') return { errcode: 40008, headers: { req_id: 'card-1' } };
    if (outcome === 'unknown') throw new Error('timeout');
    if (outcome === 'missing') return {};
    return { headers: { req_id: 'card-1' } };
  } }])
]) };
await handler({ params, respond: (ok, payload, error) => console.log(JSON.stringify(
  ok ? { payload } : { ok: false, error })) });
""")
        self.outcome = "sent"

    def state(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            return json.loads(
                conn.execute(
                    "SELECT metadata_json FROM notifications WHERE notification_id=?", (self.notice,)
                ).fetchone()[0]
            )["delivery"]

    def runner(self, argv, **kwargs):
        params = json.loads(argv[argv.index("--params") + 1])
        if params.get("phase") == "card":
            # The real queue has committed the body receipt before the card effect.
            self.assertEqual(self.state()["message_id"], "body-1")
            self.assertEqual(self.state()["status"], "sent")
        return subprocess.run(
            [self.node, str(self.script), json.dumps(params), self.outcome, str(self.effects)],
            text=True,
            capture_output=True,
            timeout=10,
        )

    def deliver(self, now="2026-07-19T02:00:00+00:00", runner=None, limit=100):
        with patch.dict(os.environ, {}, clear=True):
            return notifications.deliver_pending(
                self.db_path,
                config=self.config,
                runner=runner or self.runner,
                now=now,
                limit=limit,
            )

    def kinds(self):
        return [json.loads(line)["kind"] for line in self.effects.read_text().splitlines()]

    def quote(self, message_id="body-1"):
        return messaging.reply(
            self.config,
            {
                **self.identity,
                "message_id": "reply-1",
                "body": "Use the existing behavior.",
                "quoted": True,
                "quote_message_id": message_id,
                "quote_body": f"Delivery: {self.state()['delivery_id']}",
            },
        )

    def test_success_preserves_body_quote_identity_and_separate_card_receipt(self):
        self.assertEqual(self.deliver()["sent"], 1)
        self.assertEqual(self.kinds(), ["text", "card"])
        self.assertEqual(self.state()["message_id"], "body-1")
        self.assertEqual(self.state()["card"]["message_id"], "card-1")
        with self.assertRaises(WorkItemValidationError):
            self.quote("card-1")
        result = self.quote()
        self.assertEqual(result["notification_id"], self.notice)
        self.assertEqual(self.quote(), result)

    def test_known_card_failure_retries_only_card_after_backoff(self):
        self.outcome = "failed"
        self.deliver()
        self.assertEqual(self.state()["message_id"], "body-1")
        self.assertEqual(self.state()["card"]["status"], "failed")
        self.deliver("2026-07-19T02:00:01+00:00")
        self.assertEqual(self.kinds(), ["text", "card"])
        self.outcome = "sent"
        self.deliver("2026-07-19T02:00:06+00:00")
        self.assertEqual(self.kinds(), ["text", "card", "card"])
        self.assertEqual(self.state()["card"]["status"], "sent")
        self.assertEqual(self.quote()["notification_id"], self.notice)

    def test_unknown_card_outcome_withholds_retries_but_body_remains_answerable(self):
        self.outcome = "unknown"
        self.deliver()
        self.assertEqual(self.state()["card"]["status"], "unknown")
        self.deliver("2026-07-19T02:00:06+00:00")
        self.assertEqual(self.kinds(), ["text", "card"])
        self.assertEqual(self.quote()["notification_id"], self.notice)

    def test_missing_card_receipt_withholds_retries_but_body_remains_answerable(self):
        self.outcome = "missing"
        self.deliver()
        self.assertEqual(self.state()["card"]["status"], "unknown")
        self.deliver("2026-07-19T02:00:06+00:00")
        self.assertEqual(self.kinds(), ["text", "card"])
        self.assertEqual(self.quote()["notification_id"], self.notice)

    def test_card_cli_timeout_does_not_repeat_body_or_card(self):
        def runner(argv, **kwargs):
            params = json.loads(argv[argv.index("--params") + 1])
            result = self.runner(argv, **kwargs)
            if params.get("phase") == "card":
                raise subprocess.TimeoutExpired(argv, 90)
            return result

        self.deliver(runner=runner)
        self.assertEqual(self.state()["card"]["status"], "unknown")
        self.deliver("2026-07-19T02:00:06+00:00")
        self.assertEqual(self.kinds(), ["text", "card"])
        self.assertEqual(self.quote()["notification_id"], self.notice)

    def test_crash_after_card_effect_keeps_durable_body_and_withholds_card_replay(self):
        def runner(argv, **kwargs):
            params = json.loads(argv[argv.index("--params") + 1])
            result = self.runner(argv, **kwargs)
            if params.get("phase") == "card":
                raise SystemExit("simulate process interruption")
            return result

        with self.assertRaises(SystemExit):
            self.deliver(runner=runner)
        self.assertEqual(self.state()["message_id"], "body-1")
        self.deliver("2026-07-19T02:00:06+00:00")
        self.assertEqual(self.state()["card"]["status"], "unknown")
        self.assertEqual(self.kinds(), ["text", "card"])

    def test_card_retry_budget_is_bounded_without_repeating_body(self):
        self.outcome = "failed"
        for now in ("02:00:00", "02:00:06", "02:00:17", "02:01:00"):
            self.deliver(f"2026-07-19T{now}+00:00")
        self.assertEqual(self.kinds(), ["text", "card", "card", "card"])
        self.assertEqual(self.state()["card"]["status"], "exhausted")
        self.assertEqual(self.quote()["notification_id"], self.notice)

    def test_card_retry_elapsed_budget_prevents_late_send(self):
        self.outcome = "failed"
        self.deliver()
        self.deliver("2026-07-19T02:11:00+00:00")
        self.assertEqual(self.kinds(), ["text", "card"])
        self.assertEqual(self.state()["card"]["status"], "exhausted")

    def test_negative_card_ack_is_failed_even_with_request_id(self):
        self.outcome = "rejected"
        self.deliver()
        self.assertEqual(self.state()["card"]["status"], "failed")
        self.assertNotIn("message_id", self.state()["card"])

    def test_text_fallback_without_bot_connection_stays_answerable(self):
        self.outcome = "disconnected"
        self.deliver()
        self.assertEqual(self.kinds(), ["text"])
        self.assertEqual(self.state()["card"]["status"], "skipped")
        self.assertEqual(self.quote()["notification_id"], self.notice)

    def test_resolution_prevents_pending_card_retry(self):
        self.outcome = "failed"
        self.deliver()
        self.quote()
        self.deliver("2026-07-19T02:00:06+00:00")
        self.assertEqual(self.kinds(), ["text", "card"])

    def test_changed_endpoint_or_revoked_repository_withholds_card_retry(self):
        self.outcome = "failed"
        self.deliver()
        self.config["messaging"]["account"] = "another"
        self.deliver("2026-07-19T02:00:06+00:00")
        self.config["messaging"]["account"] = "default"
        self.config["messaging"]["repos"] = []
        self.deliver("2026-07-19T02:00:06+00:00")
        self.assertEqual(self.kinds(), ["text", "card"])

    def test_changed_target_withholds_old_card_and_restored_target_can_retry(self):
        self.outcome = "failed"
        self.deliver()
        self.config["messaging"]["target"] = "another-operator"
        self.outcome = "sent"
        self.deliver("2026-07-19T02:00:06+00:00")
        self.assertEqual(self.kinds(), ["text", "card"])
        self.assertEqual(self.state()["endpoint"]["target"], "operator")
        self.config["messaging"]["target"] = "operator"
        self.deliver("2026-07-19T02:00:06+00:00")
        effects = [json.loads(line) for line in self.effects.read_text().splitlines()]
        self.assertEqual(self.kinds(), ["text", "card", "card"])
        self.assertEqual([effect["target"] for effect in effects], ["operator"] * 3)
        self.assertEqual(self.quote()["notification_id"], self.notice)

    def test_target_changed_after_body_receipt_withholds_initial_card(self):
        def runner(argv, **kwargs):
            result = self.runner(argv, **kwargs)
            self.config["messaging"]["target"] = "another-operator"
            return result

        self.assertEqual(self.deliver(runner=runner)["sent"], 1)
        self.assertEqual(self.kinds(), ["text"])
        self.assertEqual(self.state()["endpoint"]["target"], "operator")
        self.assertEqual(self.quote()["notification_id"], self.notice)

    def test_changed_account_card_does_not_block_new_questions(self):
        self._assert_changed_destination_does_not_block_new_questions("account", "another")

    def test_changed_target_card_does_not_block_new_questions(self):
        self._assert_changed_destination_does_not_block_new_questions("target", "another-operator")

    def test_environment_command_does_not_let_old_cards_block_new_bodies(self):
        self._assert_custom_command_does_not_block_new_bodies("custom-notifier", None)

    def test_explicit_command_does_not_let_old_cards_block_new_bodies(self):
        self._assert_custom_command_does_not_block_new_bodies(None, ["custom-notifier"])

    def test_explicit_command_overrides_environment_without_old_card_work(self):
        self._assert_custom_command_does_not_block_new_bodies("other-notifier", ["custom-notifier"])

    def _assert_custom_command_does_not_block_new_bodies(self, environment_command, command):
        self.outcome = "failed"
        self.deliver()
        old_state = self.state()
        self.assertEqual(old_state["card"]["status"], "failed")
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            # Keep queue ordering independent of the fixture's wall clock.
            conn.execute(
                "UPDATE notifications SET created_at=? WHERE notification_id=?",
                ("2026-07-19T01:00:00+00:00", self.notice),
            )
        with closing(database.connect(self.db_path)) as conn, conn:
            for identifier, created, repo_id in (
                ("history-body", "2026-07-17T01:00:00+00:00", "repo-1"),
                ("denied-body", "2026-07-19T01:01:00+00:00", "denied"),
            ):
                notification_store.record_notification(
                    conn,
                    identifier,
                    None,
                    "worker_stale",
                    "local",
                    "recorded",
                    created,
                    json.dumps({"repo_id": repo_id}),
                )
        payloads = []

        def custom_runner(argv, **kwargs):
            self.assertEqual(argv, ["custom-notifier"])
            payloads.append(json.loads(kwargs["input"]))
            return subprocess.CompletedProcess(argv, 0, "", "")

        environment = {"ROBERT_NOTIFICATION_COMMAND": environment_command} if environment_command else {}
        with patch.dict(os.environ, environment, clear=True):
            for sequence, now in (
                (1, "2026-07-19T02:00:06+00:00"),
                (2, "2026-07-19T02:11:00+00:00"),
            ):
                with self.subTest(now=now):
                    with closing(database.connect(self.db_path)) as conn, conn:
                        self.assertFalse(
                            execution_notifications.has_pending_local_delivery(
                                conn, ["repo-1"], now, config=self.config, command=command
                            )
                        )
                        notification_store.record_notification(
                            conn,
                            f"new-body-{sequence}",
                            None,
                            "worker_stale",
                            "local",
                            "recorded",
                            f"2026-07-19T01:0{sequence + 1}:00+00:00",
                            json.dumps({"repo_id": "repo-1"}),
                        )
                        self.assertTrue(
                            execution_notifications.has_pending_local_delivery(
                                conn, ["repo-1"], now, config=self.config, command=command
                            )
                        )
                    self.assertEqual(
                        notifications.deliver_pending(
                            self.db_path,
                            config=self.config,
                            command=command,
                            runner=custom_runner,
                            now=now,
                            limit=1,
                            repo_ids=["repo-1"],
                        )["sent"],
                        1,
                    )
                    self.assertEqual(self.state(), old_state)
                    with closing(database.connect(self.db_path)) as conn:
                        self.assertFalse(
                            execution_notifications.has_pending_local_delivery(
                                conn, ["repo-1"], now, config=self.config, command=command
                            )
                        )
        self.assertEqual([payload["notification_id"] for payload in payloads], ["new-body-1", "new-body-2"])
        self.assertEqual(self.kinds(), ["text", "card"])
        # Disabling the command restores the existing native card retry path.
        self.outcome = "sent"
        self.deliver("2026-07-19T02:00:06+00:00", limit=1)
        self.assertEqual(self.kinds(), ["text", "card", "card"])
        self.assertEqual(self.quote()["notification_id"], self.notice)

    def _assert_changed_destination_does_not_block_new_questions(self, field, value):
        self.outcome = "failed"
        self.deliver()
        old_state = self.state()
        self.config["messaging"][field] = value
        self.outcome = "sent"
        for sequence, now in (
            (2, "2026-07-19T02:00:06+00:00"),
            (3, "2026-07-19T02:11:00+00:00"),
        ):
            with self.subTest(now=now):
                with closing(database.connect(self.db_path)) as conn:
                    self.assertFalse(
                        execution_notifications.has_pending_local_delivery(conn, None, now, config=self.config)
                    )
                item = self._create(start=True, idempotency_key=f"create-{sequence}")["item"]
                question = self._question(item["work_item_id"], sequence)
                with closing(sqlite3.connect(self.db_path)) as conn, conn:
                    conn.execute(
                        "UPDATE work_item_events SET metadata_json=json_set(metadata_json, '$.choices', json(?)) "
                        "WHERE event_id=?",
                        (json.dumps(example_choices()), question["event_id"]),
                    )
                self.assertEqual(self.deliver(now, limit=1)["sent"], 1)
                self.assertEqual(self.state(), old_state)
                with closing(database.connect(self.db_path)) as conn:
                    self.assertFalse(
                        execution_notifications.has_pending_local_delivery(conn, None, now, config=self.config)
                    )
        effects = [json.loads(line) for line in self.effects.read_text().splitlines()]
        self.assertEqual(self.kinds(), ["text", "card"] * 3)
        self.assertEqual([effect[field] for effect in effects[2:]], [value] * 4)
