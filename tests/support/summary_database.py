from contextlib import closing, redirect_stdout
import io
import json
import os
import sqlite3
from datetime import datetime, timezone
from tests.support.temp_workspace import TemporaryWorkspace


class SummaryFixture(TemporaryWorkspace):
    def _run_chat_status(self, *args):
        from robert_agent.readmodels import chat as chat_status

        output = io.StringIO()
        with redirect_stdout(output):
            code = chat_status.main(list(args))
        return code, json.loads(output.getvalue())

    def _run_status(self, *args):
        from robert_agent.readmodels import status

        output = io.StringIO()
        with redirect_stdout(output):
            code = status.main(list(args))
        return code, json.loads(output.getvalue())

    def _init_summary_db(self, db_path):
        from robert_agent.adapters.sqlite import database as storage

        storage.init_database(db_path)
        now = datetime.now(timezone.utc).isoformat()
        artifact_dir = self.root / "task-artifacts"
        artifact_dir.mkdir()
        prompt_path = artifact_dir / "prompt.md"
        stdout_path = artifact_dir / "worker.stdout.log"
        stderr_path = artifact_dir / "worker.stderr.log"
        prompt_path.write_text("compact prompt path only\n", encoding="utf-8")
        stdout_path.write_text("stdout tail\n", encoding="utf-8")
        stderr_path.write_text("stderr tail\n", encoding="utf-8")
        dispatch_metadata = {
            "process_stopped_at": now,
            "dispatch": {
                "ok": True,
                "status": "running",
                "task_id": "task-1",
                "attempt_id": "attempt-1",
                "pid": os.getpid(),
                "command": ["cbc", "-p", "--input-format", "text"],
                "prompt_path": str(prompt_path),
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
            },
        }
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root)
                VALUES ('repo-1', 'example/backend', 'robert-bot', 'master', '/repo', '/repo/.worktrees')
                """
            )
            conn.execute(
                """
                INSERT INTO github_sources(
                  source_id, repo_id, source_key, source_type, number, html_url,
                  title, state, author_login
                )
                VALUES (
                  'source-1', 'repo-1',
                  'github:example/backend#1',
                  'issue', 1, 'https://github.com/x/y/issues/1',
                  'Question', 'open', 'wklken'
                )
                """
            )
            conn.execute(
                """
                INSERT INTO github_events(
                  event_id, repo_id, source_id, event_fingerprint, event_type,
                  actor_login, author_association, authorization_status,
                  event_at, payload_json
                )
                VALUES (
                  'event-1', 'repo-1', 'source-1', 'comment:1', 'comment',
                  'wklken', 'OWNER', 'authorized', ?, ?
                )
                """,
                (now, json.dumps({"intent": "analysis"}, sort_keys=True)),
            )
            conn.executemany(
                """
                INSERT INTO github_events(
                  event_id, repo_id, source_id, event_fingerprint, event_type,
                  actor_login, author_association, authorization_status,
                  event_at, payload_json
                )
                VALUES (?, 'repo-1', 'source-1', ?, 'comment', ?, ?, ?, ?, ?)
                """,
                [
                    (
                        "event-2",
                        "comment:2",
                        "wklken",
                        "OWNER",
                        "authorized",
                        now,
                        json.dumps({"intent": "bug_fix"}, sort_keys=True),
                    ),
                    (
                        "event-3",
                        "comment:3",
                        "external-user",
                        None,
                        "pending_actor_permission",
                        now,
                        json.dumps({"intent": "unclear"}, sort_keys=True),
                    ),
                ],
            )

            conn.execute(
                """
                INSERT INTO workstreams(
                  workstream_id, repo_id, primary_source_id, lifecycle,
                  created_at, updated_at
                )
                VALUES ('ws-1', 'repo-1', 'source-1', 'active', ?, ?)
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO workstream_sources(workstream_id, source_id, relationship, created_at)
                VALUES ('ws-1', 'source-1', 'primary', ?)
                """,
                (now,),
            )
            conn.execute(
                """
                INSERT INTO tasks(task_id, workstream_id, lifecycle, priority, created_at, updated_at)
                VALUES
                  ('task-1', 'ws-1', 'completed', 'P1', ?, ?),
                  ('task-2', 'ws-1', 'running', 'P0', ?, ?)
                """,
                (now, now, now, now),
            )
            conn.execute(
                """
                INSERT INTO attempts(
                  attempt_id, task_id, attempt_no, status, started_at, heartbeat_at,
                  finished_at, worktree_path, branch_name, metadata_json
                )
                VALUES ('attempt-1', 'task-1', 1, 'completed', ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    now,
                    now,
                    "/repo/.worktrees/task-1",
                    "codex/dd-1-task",
                    json.dumps(dispatch_metadata, sort_keys=True),
                ),
            )
            conn.execute(
                """
                INSERT INTO worker_phases(
                  phase_id, attempt_id, phase, status, summary, next_step, created_at
                )
                VALUES ('phase-1', 'attempt-1', 'analyze', 'completed', 'Read context', 'Verify result', ?)
                """,
                (now,),
            )
            conn.executemany(
                """
                INSERT INTO route_decisions(
                  route_decision_id, task_id, route_id, expected_output,
                  allowed_github_actions_json, required_skills_json,
                  recommended_skills_json, confidence, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        "route-1",
                        "task-1",
                        "comment-analysis",
                        "comment_analysis",
                        '["comment"]',
                        '["fast-code-path", "fast-zoom-out", "fast-verify-review-point"]',
                        '["fast-code-path", "fast-zoom-out", "fast-verify-review-point"]',
                        "high",
                        now,
                    ),
                    (
                        "route-2",
                        "task-2",
                        "new-pr",
                        "new_pr",
                        '["push_existing_pr", "open_pr", "comment"]',
                        '["fast-small-pr", "fast-code-path", "fast-add-tests", "fast-test-fix", "fast-preflight"]',
                        '["fast-small-pr", "fast-code-path", "fast-add-tests", "fast-test-fix", "fast-preflight"]',
                        "high",
                        now,
                    ),
                ],
            )
            conn.execute(
                """
                INSERT INTO task_events(task_id, event_id, relationship, created_at)
                VALUES ('task-1', 'event-1', 'trigger', ?)
                """,
                (now,),
            )
            conn.execute(
                """
                INSERT INTO agent_runs(run_id, status, started_at, finished_at, config_path, dry_run, error_json)
                VALUES ('run-1', 'failed', ?, ?, '/tmp/config.yml', 0, '{"status": "failed_dispatch"}')
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO run_steps(step_id, run_id, step_key, status, started_at, finished_at)
                VALUES
                  ('step-1', 'run-1', 'validate_config', 'succeeded', ?, ?),
                  ('step-2', 'run-1', 'dispatch', 'failed', ?, ?),
                  ('step-3', 'run-1', 'summarize', 'skipped', ?, ?)
                """,
                (now, now, now, now, now, now),
            )
            conn.execute(
                """
                INSERT INTO worker_results(result_id, task_id, attempt_id, output_type, consumed_event_fingerprints_json, verification_json, handoff, created_at, metadata_json)
                VALUES ('result-1', 'task-1', 'attempt-1', 'comment_analysis', '["comment:1"]', '[]', 'ready', ?, json_set(?, '$.result_contract', 'compact-v1'))
                """,
                (
                    now,
                    json.dumps(
                        {
                            "audit": {
                                "status": "accepted",
                                "action_scope": {
                                    "repo": "x/y",
                                    "sources": [{"source_type": "issue", "number": 1}],
                                    "result_head_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                                },
                            }
                        },
                        sort_keys=True,
                    ),
                ),
            )
            conn.execute(
                """
                INSERT INTO github_actions(
                  action_id, result_id, task_id, action_type, target_url, audit_status,
                  publish_status, created_at, metadata_json
                )
                VALUES (
                  'action-1', 'result-1', 'task-1', 'comment',
                  'https://github.com/x/y/issues/1', 'accepted',
                  'not_published', ?, ?
                )
                """,
                (
                    now,
                    json.dumps({"body": self._dd_comment_body("ready")}, sort_keys=True),
                ),
            )
            conn.execute(
                """
                INSERT INTO notifications(
                  notification_id, task_id, notification_type, channel, status,
                  created_at, metadata_json
                )
                VALUES (
                  'notification-1', 'task-1', 'task_completed',
                  'local', 'recorded', ?, '{"summary": "ready"}'
                )
                """,
                (now,),
            )
            conn.execute(
                """
                INSERT INTO wakeups(
                  wakeup_id, repo_id, reason, dedupe_key, task_id, attempt_id,
                  result_id, status, not_before_at, created_at, updated_at, metadata_json
                )
                VALUES (
                  'wakeup-1', 'repo-1', 'worker_result_ready', 'result-1',
                  'task-1', 'attempt-1', 'result-1', 'pending', ?, ?, ?,
                  '{"recorded_by": "test"}'
                )
                """,
                (now, now, now),
            )
            conn.executemany(
                """
                INSERT INTO artifacts(
                  artifact_id, task_id, attempt_id, artifact_type, path, bytes, created_at
                )
                VALUES (?, 'task-1', 'attempt-1', ?, ?, ?, ?)
                """,
                [
                    (
                        "artifact-prompt",
                        "prompt",
                        str(prompt_path),
                        prompt_path.stat().st_size,
                        now,
                    ),
                    (
                        "artifact-stdout",
                        "worker_stdout",
                        str(stdout_path),
                        stdout_path.stat().st_size,
                        now,
                    ),
                    (
                        "artifact-stderr",
                        "worker_stderr",
                        str(stderr_path),
                        stderr_path.stat().st_size,
                        now,
                    ),
                ],
            )

    def _dd_comment_body(self, text):
        return "<!-- robert-comment task_id=task-1 attempt_id=attempt-1 action_index=0 -->\n" + text
