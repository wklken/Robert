"""Worker protocol fixtures for a registered attempt and result actions."""

from tests.support.results import internal_result
from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


class WorkerProtocolFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _init_attempt_db(self, *, output_type="comment_analysis", route_id="comment-analysis"):
        from robert_agent.persistence.schema import database as storage
        from robert_agent.workflows import routes as route

        db_path = self.root / "dd.sqlite3"
        storage.init_database(db_path)
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root)
                VALUES ('repo-1', 'example/backend', 'robert-bot', 'master', '/repo', '/repo/.worktrees')
                """
            )
            conn.execute(
                """
                INSERT INTO workstreams(workstream_id, repo_id, lifecycle, created_at, updated_at)
                VALUES ('ws-1', 'repo-1', 'active', ?, ?)
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO tasks(task_id, workstream_id, lifecycle, priority, created_at, updated_at)
                VALUES ('task-1', 'ws-1', 'running', 'P1', ?, ?)
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO attempts(attempt_id, task_id, attempt_no, status, started_at, heartbeat_at)
                VALUES ('attempt-1', 'task-1', 1, 'running', ?, ?)
                """,
                (now, now),
            )
            conn.execute("UPDATE workstreams SET active_task_id='task-1'")
            from robert_agent.persistence import execution_control

            execution_control.ensure_subject(conn, "task", "task-1", "human_request", now)
            conn.execute("UPDATE attempts SET metadata_json=json_set(metadata_json,'$.result_contract','compact-v1')")
            conn.execute("UPDATE repos SET full_name='x/y'")
            conn.execute(
                "INSERT INTO github_sources(source_id,repo_id,source_key,source_type,number) VALUES('source-1','repo-1','x/y#1','issue',1)"
            )
            conn.execute("UPDATE workstreams SET primary_source_id='source-1'")
            conn.execute(
                "INSERT INTO route_decisions(route_decision_id,task_id,route_id,expected_output,allowed_github_actions_json,confidence,created_at) VALUES('route-1','task-1',?,?,?,'high',?)",
                (route_id, output_type, json.dumps(route.ROUTES[route_id]["allowed_github_actions"]), now),
            )
        return db_path

    def _dd_pr_body(self, text):
        return (
            "<!-- robert-workstream\n"
            "origin_workstream_id: github:x/y#1\n"
            "source_issue: 1\n"
            "task_id: task-1\n"
            "created_by: robert\n"
            "-->\n" + text
        )

    def _issue_action_scope(self):
        return {
            "repo": "x/y",
            "result_head_sha": "a" * 40,
            "worktree_status": "clean",
            "base_branch": "master",
            "worktree_path": "/repo/.worktrees/dd-1",
            "branch_name": "codex/dd-1",
            "sources": [{"source_type": "issue", "number": 1}],
        }

    def _issue_new_pr_result(
        self,
        *,
        issue_resolution,
        issue_reference,
        remaining_scope="",
    ):

        open_pr = {
            "type": "open_pr",
            "repo": "x/y",
            "head": "codex/dd-1",
            "base": "master",
            "title": "Implement issue",
            "body": self._dd_pr_body(f"{issue_reference}\n\nImplementation details."),
            "issue_resolution": issue_resolution,
        }
        if remaining_scope:
            open_pr["remaining_scope"] = remaining_scope
        payload = internal_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="new_pr",
            planned_github_actions=[
                {
                    "type": "push_existing_pr",
                    "worktree_path": "/repo/.worktrees/dd-1",
                    "branch": "codex/dd-1",
                },
                open_pr,
            ],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="opened pr",
            used_skills=[],
        )

        payload["submission_head_sha"] = "a" * 40
        return payload

    def _dd_comment_body(self, text):
        return "<!-- robert-comment task_id=task-1 attempt_id=attempt-1 action_index=0 -->\n" + text

    def _review_response_action(self, text="Responded to the review report."):
        return {
            "type": "comment",
            "target_url": "https://github.com/x/y/pull/1#pullrequestreview-9",
            "body": self._dd_comment_body(text),
        }

    def _review_point(self, verdict, action):
        return {
            "summary": "The review point asks whether this change is needed.",
            "verdict": verdict,
            "reasoning": "Checked the current code path and compared it with the requested behavior.",
            "action": action,
        }

    def _verification_entry(self, status="passed"):
        entry = {
            "command": ["python3", "-B", "-m", "unittest"],
            "status": status,
            "purpose": "Verify the worker result contract.",
            "required": True,
        }
        if status != "skipped":
            entry["exit_code"] = 0 if status == "passed" else 1
        return entry
