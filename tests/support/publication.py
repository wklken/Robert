from contextlib import closing
import json
import sqlite3
from datetime import datetime, timezone
from tests.support.summary_database import SummaryFixture


class PublicationFixture(SummaryFixture):
    def _init_remediation_publish_db(self):
        db_path = self.root / "remediation.sqlite3"
        self._init_summary_db(db_path)
        worktree = self.root / "remediation-worktree"
        worktree.mkdir()
        now = datetime.now(timezone.utc).isoformat()
        action_scope = {
            "repo": "x/y",
            "base_branch": "main",
            "worktree_path": str(worktree),
            "branch_name": "codex/fix",
            "remote": "origin",
            "sources": [{"source_type": "pull_request", "number": 42}],
            "task_kind": "ci_remediation",
            "episode_id": "episode-ci",
            "pr_number": 42,
            "observed_head_sha": "head-1",
            "observed_base_sha": "base-1",
            "result_head_sha": "repair-1",
        }
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE tasks
                SET task_kind = 'ci_remediation',
                    metadata_json = '{"remediation_episode_id":"episode-ci"}'
                WHERE task_id = 'task-1'
                """
            )
            conn.execute(
                """
                UPDATE worker_results
                SET metadata_json=json_set(?, '$.result_contract', 'compact-v1')
                WHERE result_id = 'result-1'
                """,
                (
                    json.dumps(
                        {
                            "audit": {
                                "ok": True,
                                "status": "accepted",
                                "action_scope": action_scope,
                            }
                        },
                        sort_keys=True,
                    ),
                ),
            )
            conn.execute(
                """
                INSERT INTO pr_remediation_episodes(
                  episode_id, workstream_id, episode_kind, subject_key,
                  observed_head_sha, observed_base_sha, status, attempt_count,
                  failure_signature, first_seen_at, updated_at, last_task_id,
                  metadata_json
                )
                VALUES (
                  'episode-ci', 'ws-1', 'ci', 'head-1',
                  'head-1', 'base-1', 'remediating', 1,
                  'failure-1', ?, ?, 'task-1', '{"pr_number":42}'
                )
                """,
                (now, now),
            )
            conn.execute(
                """
                UPDATE github_actions
                SET action_type = 'push_existing_pr',
                    target_url = 'https://github.com/x/y/pull/42',
                    metadata_json = ?
                WHERE action_id = 'action-1'
                """,
                (
                    json.dumps(
                        {
                            "worktree_path": str(worktree),
                            "remote": "origin",
                            "branch": "codex/fix",
                        },
                        sort_keys=True,
                    ),
                ),
            )
            conn.execute(
                """
                INSERT INTO github_actions(
                  action_id, result_id, task_id, action_type, target_url,
                  audit_status, publish_status, created_at, metadata_json
                )
                VALUES (
                  'action-2', 'result-1', 'task-1', 'comment',
                  'https://github.com/x/y/pull/42', 'accepted',
                  'not_published', ?, ?
                )
                """,
                (
                    now,
                    json.dumps(
                        {"body": self._dd_comment_body("CI repair published")},
                        sort_keys=True,
                    ),
                ),
            )
        return db_path, worktree

    def _dd_pr_body(self, text, task_id="task-1"):
        return (
            "<!-- robert-workstream\n"
            "origin_workstream_id: github:x/y#1\n"
            "source_issue: 1\n"
            f"task_id: {task_id}\n"
            "created_by: robert\n"
            "-->\n"
            + text
        )
