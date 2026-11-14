"""Chat presents understandable facts while machine envelopes retain their codes."""

import copy
import json
import subprocess
import unittest
from unittest.mock import Mock

from robert_agent.adapters.notifications.messaging import render_message, send


class MessagePresentationTests(unittest.TestCase):
    def payload(self, **values):
        return {
            "type": "worker_resume_prepared",
            "task_id": "task-example",
            "notification_id": "notice-example",
            "delivery_id": "notice-example:receipt",
            "version": 0,
            "actions": ["ack"],
            "task_status": "completed",
            "workstream_status": "completed",
            "route_id": "classification-result",
            "reason_code": "failed_worker_process_exited",
            "recovery_status": "recoverable_progress",
            **values,
        }

    def test_internal_codes_are_explained_without_claiming_recovery_succeeded(self):
        payload = self.payload()
        original = copy.deepcopy(payload)
        text = render_message(payload)
        for code in (payload[key] for key in ("type", "task_status", "route_id", "reason_code", "recovery_status")):
            self.assertNotIn(code, text)
        self.assertIn("执行进程意外退出", text)
        self.assertIn("已保存的执行进展", text)
        self.assertIn("尚不代表恢复成功", text)
        self.assertIn("任务当前状态: 已完成", text)
        self.assertIn("任务类型: 请求分类", text)
        self.assertIn("确认收到", text)
        self.assertIn("/robert-respond notice-example 0 ack", text)
        self.assertEqual(payload, original)

    def test_unknown_codes_are_explicitly_unclassified_and_do_not_invent_a_cause(self):
        payload = self.payload(
            type="new_event",
            task_status="new_status",
            workstream_status="new_status",
            route_id="new_route",
            reason_code="new_cause",
            recovery_status="new_recovery",
        )
        text = render_message(payload)
        for code in ("new_event", "new_status", "new_route", "new_cause", "new_recovery"):
            self.assertNotIn(code, text)
        self.assertIn("原因暂未归类", text)
        self.assertIn("恢复状态暂未归类", text)
        self.assertIn("查看任务详情", text)
        self.assertNotIn("超时", text)
        self.assertNotIn("恢复成功", text)

    def test_waiting_for_user_and_rate_limit_are_explained(self):
        text = render_message(
            self.payload(
                type="operator_question",
                task_status="waiting_for_user",
                reason_code="rate_limited",
                recovery_status="no_progress",
                actions=["reply", "cancel"],
            )
        )
        self.assertIn("等待你回复", text)
        self.assertIn("请求频率限制", text)
        self.assertIn("未发现可用于恢复的执行进展", text)
        self.assertIn("回复问题", text)
        self.assertIn("取消任务", text)
        self.assertIn("/robert-respond notice-example 0 reply <answer>", text)

    def test_execution_delay_reasons_are_explained(self):
        for reason, explanation in (("queue_delayed", "排队"), ("publication_delayed", "发布")):
            with self.subTest(reason=reason):
                payload = self.payload(type="execution_delayed", reason_code=reason)
                original = copy.deepcopy(payload)
                text = render_message(payload)
                self.assertIn("任务进度延迟", text)
                self.assertIn(explanation, text)
                self.assertNotIn("新通知", text)
                self.assertNotIn("原因暂未归类", text)
                self.assertEqual(payload, original)

    def test_command_transport_retains_the_raw_protocol_values(self):
        payload = self.payload()
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        self.assertTrue(send(payload, command=["local-notifier"], runner=runner))
        self.assertEqual(json.loads(runner.call_args.kwargs["input"]), payload)

    def test_known_decision_failure_codes_explain_service_failure(self):
        for code in (
            "exited",
            "invalid_output",
            "timed_out",
            "stopped",
            "launch_failed",
            "process_attention",
            "provider_rate_limited",
        ):
            with self.subTest(code=code):
                text = render_message(
                    self.payload(
                        type="supervision_decision_unavailable",
                        reason_code=code,
                        decision_id="decision-example",
                        workstream_id="stream-example",
                        exit_code=7,
                    )
                )
                self.assertNotIn("原因暂未归类", text)
                self.assertIn("决策记录: decision-example", text)
                self.assertIn("工作流: stream-example", text)
                self.assertIn("退出码: 7", text)

    def test_builtin_transport_uses_the_human_readable_body(self):
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, '{"ok":true}', ""))
        config = {
            "messaging": {
                "enabled": True,
                "provider": "openclaw",
                "channel": "wecom",
                "account": "default",
                "target": "operator",
            }
        }
        self.assertTrue(send(self.payload(), config=config, runner=runner))
        argv = runner.call_args.args[0]
        body = argv[argv.index("--message") + 1]
        self.assertIn("执行进程意外退出", body)
        self.assertNotIn("failed_worker_process_exited", body)

    def test_dedicated_chat_question_explains_plain_and_quoted_reply(self):
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, '{"ok":true}', ""))
        config = {
            "messaging": {
                "enabled": True,
                "provider": "openclaw",
                "channel": "wecom",
                "account": "default",
                "target": "operator",
                "contextual_replies": True,
            }
        }
        self.assertTrue(
            send(self.payload(type="operator_question", actions=["reply", "cancel"]), config=config, runner=runner)
        )
        body = runner.call_args.args[0][runner.call_args.args[0].index("--message") + 1]
        self.assertIn("直接回复", body)
        self.assertIn("引用", body)
        self.assertIn("最近", body)
