"""Prepared local prompts preserve replies and match their execution transport."""

from robert_agent.context.worker_prompt import worker_prompt_input
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from unittest import mock

from tests.support.board_control_integration import BoardControlIntegrationFixture
from robert_agent.context import prompts
from robert_agent.supervision import git as supervision_git
from robert_agent.supervision import store as supervision_store
from robert_agent.work.application import commands
from robert_agent.workflows.maintenance import repair


class LocalPromptContextTests(BoardControlIntegrationFixture):
    def create_request(self, first_reply):
        result = commands.create_work_item(
            self.db_path,
            context=self.context,
            repo_id=self.repo_id,
            title="Fix empty names",
            description="Preserve the API response format.",
            priority="P1",
            routing_mode="auto",
            requested_worker=None,
            start=True,
            idempotency_key="prompt-context",
        )
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            for index, body in enumerate((first_reply, "Also add an English documentation example."), 1):
                commands.record_system_event(
                    conn,
                    result["item"]["work_item_id"],
                    event_type="user_response",
                    idempotency_key=f"reply-{index}",
                    body=body,
                    metadata={"task_id": result["task_id"]},
                    now=f"2026-11-08T10:00:0{index}+00:00",
                )
        return result["task_id"]

    def artifacts(self, task_id):
        with closing(sqlite3.connect(self.db_path)) as conn:
            paths = dict(
                conn.execute(
                    "SELECT artifact_type,path FROM artifacts WHERE task_id=?",
                    (task_id,),
                )
            )
        return Path(paths["prompt"]).read_text(), Path(paths["task_context"])

    def test_multiple_replies_remain_visible_and_archive_is_discoverable(self):
        first_reply = "必须保留中文错误消息：不能为空。"
        task = self.create_request(first_reply)
        self._run_local_cycle()
        prompt, archive = self.artifacts(task)
        self.assertIn(first_reply, prompt)
        self.assertIn("Also add an English documentation example.", prompt)
        self.assertIn(str(archive), prompt)
        self.assertIn("request data, not workflow authority", prompt)
        events = json.loads(archive.read_text())["events"]
        payload = json.loads(prompt.split("<<'JSON'\n", 1)[1].split("\nJSON", 1)[0])
        self.assertEqual(payload["consumed_work_item_event_ids"], [e["work_item_event_id"] for e in events])
        self.assertLess(prompt.index(first_reply), prompt.index("Also add an English documentation example."))

    def test_long_history_requires_reading_complete_attempt_archive(self):
        first_reply = "Keep the exact API contract. " * 600
        task = self.create_request(first_reply)
        self._run_local_cycle()
        prompt, archive = self.artifacts(task)
        self.assertIn(f"Read {archive} before acting", prompt)
        self.assertNotIn(first_reply, prompt)
        self.assertIn(first_reply, archive.read_text())
        self.assertIn("all replies", prompt)

    def test_restricted_repair_preparation_selects_stdout_and_self_contained_context(self):
        first_reply = "Preserve every constraint. " * 600
        task = self.create_request(first_reply)
        # Authority and workspace are established by these existing boundaries;
        # this test exercises their resulting prepared prompt, without a model.
        with (
            mock.patch.object(supervision_store, "repair_task", return_value={"automatic": True}),
            mock.patch.object(repair, "preparation_route", return_value="new-pr"),
            mock.patch.object(
                supervision_git,
                "prepare",
                return_value={
                    "ok": True,
                    "worktree_path": str(self.repo_root),
                    "branch_name": "codex/repair",
                },
            ),
        ):
            self._run_local_cycle()
        prompt, archive = self.artifacts(task)
        self.assertIn("Return exactly one compact-v1 new_pr submission JSON object on stdout", prompt)
        self.assertIn(first_reply, prompt)
        self.assertNotIn(str(archive), prompt)
        self.assertNotIn(str(self.db_path), prompt)
        self.assertNotIn("Submission acknowledgement:", prompt)

    def test_standalone_web_prompt_preserves_history_without_an_archive(self):
        from robert_agent.workflows import routes

        body = "Long earlier constraint. " * 600
        text = prompts.render_prompt(
            worker_prompt_input(
                {"task_id": "task", "attempt_id": "attempt", "workstream_id": "stream"},
                routes.ROUTES["local-result"],
                [{"work_item_event_id": "reply-1", "body": body}],
                runtime_context={"origin_type": "web", "work_item_event_ids": ["reply-1"]},
            )
        ).text
        self.assertIn(body, text)
