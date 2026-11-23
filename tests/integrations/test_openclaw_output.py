"""Exercise human-facing replies through generated plugin command handlers."""

import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import unittest

from robert_agent.integrations.openclaw import write_plugin


@unittest.skipUnless(shutil.which("node"), "Node is required to execute the OpenClaw plugin")
class OpenClawOutputTests(unittest.TestCase):
    def invoke(self, payload, commands, *, exit_code=0, config_path="/config with spaces.yml"):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_plugin(root / "plugin")
            package = root / "plugin/node_modules/openclaw"
            package.mkdir(parents=True)
            (package / "package.json").write_text(
                json.dumps({"type": "module", "exports": {"./plugin-sdk/plugin-entry": "./entry.js"}})
            )
            (package / "entry.js").write_text("export const definePluginEntry = p => p;")
            binary = root / "bin"
            binary.mkdir()
            (root / "payload.json").write_text(json.dumps(payload))
            executable = binary / "robert"
            executable.write_text(
                f"#!{sys.executable}\n"
                "import json, os, sys\nfrom pathlib import Path\n"
                "with open(os.environ['CALLS'], 'a') as f:\n"
                " f.write(json.dumps({'args':sys.argv[1:], 'request':json.loads(sys.stdin.read() or '{}')})+'\\n')\n"
                "print(Path(os.environ['PAYLOAD']).read_text())\n"
                f"sys.exit({exit_code})\n"
            )
            executable.chmod(0o755)
            script = root / "plugin/check.mjs"
            script.write_text("""import plugin from './index.js';
const commands = {};
plugin.register({ pluginConfig: { robertConfig: JSON.parse(process.argv[3]) },
  registerCommand: command => commands[command.name] = command });
const replies = [];
for (const [name, args, authorized = true] of JSON.parse(process.argv[2])) {
  replies.push(await commands[name].handler({ args, isAuthorizedSender: authorized,
    channelId: 'telegram', accountId: 'personal', from: 'telegram:42', senderId: '42' }));
}
console.log(JSON.stringify(replies));
""")
            result = subprocess.run(
                ["node", str(script), json.dumps(commands), json.dumps(config_path)],
                env={
                    **os.environ,
                    "PATH": str(binary) + os.pathsep + os.environ["PATH"],
                    "CALLS": str(root / "calls"),
                    "PAYLOAD": str(root / "payload.json"),
                },
                text=True,
                capture_output=True,
                timeout=10,
                check=True,
            )
            calls = [json.loads(line) for line in (root / "calls").read_text().splitlines()]
            return json.loads(result.stdout), calls

    def test_truncated_artifact_points_to_registered_file_in_summary_and_json(self):
        path = "/data/artifacts/worker '日志😀.log"
        payload = {
            "ok": True,
            "data": {
                "task_id": "task-one",
                "artifact_type": "worker_stdout",
                "path": path,
                "content": "tail 😀\n" * 10000,
                "truncated": True,
                "missing": False,
            },
        }
        replies, _ = self.invoke(
            payload,
            [("robert-artifact", "task-one worker_stdout"), ("robert-artifact", "task-one worker_stdout --json")],
        )
        for reply in replies:
            text = reply["text"]
            self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 3500)
            self.assertIn("预览", text)
            file_hint = next(line for line in text.splitlines() if line.startswith("完整产物："))
            self.assertEqual(shlex.split(file_hint.removeprefix("完整产物：在 Robert 主机打开已登记文件 ")), [path])
            self.assertNotIn("完整产物：本地执行 robert artifact show", text)

    def test_oversized_diagnostic_footer_is_omitted_without_partial_command(self):
        config_path = "/" + "/".join(["cfg'" * 35] * 18) + "/config.yml"
        payload = {"ok": True, "data": {"large_record": "😀" * 5000}}
        replies, calls = self.invoke(payload, [("robert-status", "--json")], config_path=config_path)
        text = replies[0]["text"]
        self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 3500)
        self.assertIn("操作提示过长", text)
        self.assertIn("Robert 主机", text)
        self.assertNotIn("robert status", text)
        self.assertNotIn("cfg", text)
        self.assertEqual(calls[0]["args"][-1], config_path)

    def test_diagnostic_footer_near_budget_preserves_whole_command_or_short_hint(self):
        payload = {"ok": True, "data": {"large_record": "😀" * 5000}}
        for length in range(3409, 3470, 10):
            config_path = "/" + "/".join(["x" * 140] * 24) + "/" + "x" * (length - 3383) + "/config.yml"
            with self.subTest(length=length):
                replies, _ = self.invoke(payload, [("robert-status", "--json")], config_path=config_path)
                text = replies[0]["text"]
                self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 3500)
                if "完整 JSON：本地执行 " in text:
                    command = text.split("完整 JSON：本地执行 ", 1)[1]
                    self.assertEqual(
                        shlex.split(command), ["robert", "status", "--output", "json", "--config", config_path]
                    )
                else:
                    self.assertIn("操作提示过长", text)
                    self.assertNotIn("robert status", text)

    def test_status_is_a_bounded_summary_with_alerts_and_explicit_json(self):
        payload = {
            "ok": True,
            "data": {
                "daemon": {
                    "latest_run": {"status": "running"},
                    "latest_lease": {"heartbeat_at": "2026-11-23T12:00:00Z"},
                },
                "summary": {"repos": 10, "tasks": 289},
                "operator_state_counts": {"worker_running": 2, "waiting_user": 1, "needs_attention": 1},
                "operator_alerts": [{"alert_type": "needs_attention_tasks", "count": 1}],
                "operator_next_steps": [
                    {"target_kind": "task", "target_id": "task-broken", "reason": "needs_attention"}
                ],
                "recent_tasks": [
                    {
                        "task_id": "task-running",
                        "operator_state": "worker_running",
                        "source_key": "github:owner/repo!123",
                    }
                ],
                "latest_run": {"run_id": "run-last", "status": "completed"},
                "execution_timings": {"large_internal_record": "x" * 200},
            },
        }
        replies, calls = self.invoke(payload, [("robert-status", ""), ("robert-status", "--json")])
        text = replies[0]["text"]
        self.assertIn("Robert", text)
        self.assertIn("运行中", text)
        self.assertIn("近期任务", text)
        self.assertIn("需要处理", text)
        self.assertIn("/robert-task task-broken", text)
        self.assertIn("/robert-run run-last", text)
        self.assertNotIn("large_internal_record", text)
        self.assertLess(len(text), 1500)
        self.assertEqual(json.loads(replies[1]["text"]), payload)
        self.assertTrue(
            all(call["args"] == ["status", "--output", "json", "--config", "/config with spaces.yml"] for call in calls)
        )

    def test_details_surface_failures_results_and_artifact_content(self):
        task = {
            "ok": True,
            "data": {
                "task_id": "task-one",
                "lifecycle": "failed",
                "route_id": "new-pr",
                "source_key": "github:owner/repo#12",
                "latest_attempt": {"status": "failed", "failure": {"safe_error": "Worker timed out."}},
                "latest_result": {"handoff": "Implemented the change."},
                "artifacts": [{"artifact_type": "report"}],
            },
        }
        replies, _ = self.invoke(task, [("robert-task", "task-one"), ("robert-task", "task-one --json")])
        self.assertIn("失败", replies[0]["text"])
        self.assertIn("Worker timed out.", replies[0]["text"])
        self.assertIn("Implemented the change.", replies[0]["text"])
        self.assertIn("/robert-artifact task-one report", replies[0]["text"])
        self.assertEqual(json.loads(replies[1]["text"]), task)
        run = {
            "ok": True,
            "data": {
                "run_id": "run-one",
                "status": "failed",
                "error": {"safe_error": "Scan failed."},
                "steps": [
                    {"step_key": "discover", "status": "failed", "error": {"safe_error": "Network unavailable."}}
                ],
            },
        }
        replies, _ = self.invoke(run, [("robert-run", "run-one")])
        self.assertIn("Scan failed.", replies[0]["text"])
        self.assertIn("Network unavailable.", replies[0]["text"])
        artifact = {
            "ok": True,
            "data": {
                "task_id": "task-one",
                "artifact_type": "report",
                "content": "Report body\n" * 1000,
                "truncated": False,
            },
        }
        replies, _ = self.invoke(artifact, [("robert-artifact", "task-one report")])
        self.assertIn("Report body", replies[0]["text"])
        self.assertIn("截断", replies[0]["text"])
        self.assertLessEqual(len(replies[0]["text"]), 3500)

    def test_inbox_exposes_only_permitted_versioned_actions_and_bounds_lists(self):
        message = {
            "notification_id": "notice-one",
            "version": 3,
            "task_id": "task-one",
            "summary": "Which database?",
            "required_action": "Choose an option.",
            "choices": [{"label": "SQLite"}, {"label": "Postgres"}],
            "selectable_choices": True,
            "actions": ["reply", "cancel"],
        }
        messages = [{**message, "notification_id": f"notice-{index}"} for index in range(20)]
        payload = {
            "ok": True,
            "messages": messages,
            "dispatch_help": "/robert-inbox new <owner/repo> <request>",
            "dispatch_repos": ["owner/repo"],
        }
        replies, calls = self.invoke(
            payload, [("robert-inbox", ""), ("robert-inbox", "--json"), ("robert-inbox", "page 2")]
        )
        text = replies[0]["text"]
        self.assertIn("Which database?", text)
        self.assertIn("SQLite", text)
        self.assertIn("/robert-respond notice-0 3 pick 1", text)
        self.assertIn("/robert-respond notice-0 3 cancel", text)
        self.assertNotIn("3 approve", text)
        self.assertIn("未展示", text)
        self.assertLessEqual(len(text), 3500)
        self.assertIn("截断", replies[1]["text"])
        self.assertLessEqual(len(replies[1]["text"]), 3500)
        self.assertIn("notice-3", replies[2]["text"])
        self.assertNotIn("notice-0", replies[2]["text"])
        self.assertTrue(all(call["request"]["sender_id"] == "42" for call in calls))

    def test_long_questions_can_all_be_reached_by_paging(self):
        messages = [
            {
                "notification_id": f"notice-{index}",
                "version": 3,
                "summary": "Question " * 100,
                "title": "Title " * 100,
                "required_action": "Choose " * 100,
                "choices": [{"label": "Option " * 20, "tradeoff": "Tradeoff " * 20} for _ in range(5)],
                "selectable_choices": True,
                "actions": ["reply", "cancel"],
            }
            for index in range(6)
        ]
        replies, _ = self.invoke(
            {"ok": True, "messages": messages}, [("robert-inbox", f"page {page}") for page in range(1, 7)]
        )
        text = "\n".join(reply["text"] for reply in replies)
        for index in range(6):
            self.assertIn(f"/robert-respond notice-{index} 3 cancel", text)
        self.assertTrue(all(len(reply["text"]) <= 3500 for reply in replies))

    def test_missing_artifact_is_visible_and_diagnostic_command_preserves_configuration(self):
        replies, _ = self.invoke(
            {"ok": True, "data": {"task_id": "task-one", "artifact_type": "report", "missing": True}},
            [("robert-artifact", "task-one report")],
        )
        self.assertIn("文件不可用", replies[0]["text"])
        self.assertIn("--config '/config with spaces.yml'", replies[0]["text"])

    def test_dispatch_draft_and_ack_do_not_claim_completion(self):
        request_id = "dispatch-" + "a" * 32
        draft = {
            "ok": True,
            "status": "draft",
            "request_id": request_id,
            "title": "Improve status",
            "submit_command": f"/robert-inbox submit {request_id}",
        }
        replies, calls = self.invoke(
            draft, [("robert-inbox", "new owner/repo Improve status\nKeep $(touch never) literal.")]
        )
        self.assertIn("草稿", replies[0]["text"])
        self.assertIn(f"/robert-inbox submit {request_id}", replies[0]["text"])
        self.assertEqual(calls[0]["request"]["body"], "Improve status\nKeep $(touch never) literal.")
        ack = {"ok": True, "status": "acknowledged", "notification_id": "notice-one"}
        replies, _ = self.invoke(ack, [("robert-respond", "notice-one 0 ack")])
        self.assertIn("收悉", replies[0]["text"])
        self.assertNotIn("已完成", replies[0]["text"])
        accepted = {"ok": True, "status": "accepted", "notification_id": "notice-one", "task_id": "task-one"}
        replies, _ = self.invoke(accepted, [("robert-respond", "notice-one 0 retry")])
        self.assertIn("已收到", replies[0]["text"])
        self.assertIn("/robert-task task-one", replies[0]["text"])
        self.assertNotIn("已完成", replies[0]["text"])

    def test_safe_errors_are_bounded_and_invalid_read_args_do_not_start_cli(self):
        payload = {"ok": False, "safe_error": "Unavailable. " + "x" * 10000}
        replies, calls = self.invoke(
            payload,
            [
                ("robert-task", "task-one"),
                ("robert-status", "unexpected"),
                ("robert-run", "run-one extra"),
                ("robert-status", "", False),
            ],
            exit_code=1,
        )
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(reply["isError"] for reply in replies))
        self.assertIn("Unavailable.", replies[0]["text"])
        self.assertLessEqual(len(replies[0]["text"]), 3500)

    def test_empty_inbox_and_unknown_service_state_do_not_invent_success(self):
        replies, _ = self.invoke({"ok": True, "messages": []}, [("robert-inbox", "")])
        self.assertIn("没有待处理", replies[0]["text"])
        replies, _ = self.invoke({"ok": True, "data": {"daemon": {}, "recent_tasks": []}}, [("robert-status", "")])
        self.assertIn("未知", replies[0]["text"])
        self.assertNotIn("运行中", replies[0]["text"])
