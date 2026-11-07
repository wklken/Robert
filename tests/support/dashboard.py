from contextlib import closing
import json
import sqlite3
from datetime import datetime, timezone
from tests.support.summary_database import SummaryFixture


class DashboardFixture(SummaryFixture):
    def _record_knowledge_candidate(self, db_path):
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO knowledge_candidates(
                  candidate_id, repo_id, title, summary, prompt_text,
                  candidate_type, source_memory_ids_json, evidence_json,
                  confidence, status, created_at, metadata_json
                )
                VALUES (
                  'kc-1', 'repo-1', 'Prefer update-existing-pr',
                  'DD PR follow-up should update the existing branch.',
                  'When review comments arrive on a DD PR, use update-existing-pr.',
                  'rule', '["memory-1"]', '[{"type": "test", "value": "unit"}]',
                  'medium', 'pending', ?, '{"retrieval_boost": {"keywords": ["dd-pr"]}}'
                )
                """,
                (now,),
            )

    def _record_project_memory_entry(self, db_path):
        from robert_agent.context import project as project_memory

        now = datetime.now(timezone.utc).isoformat()
        payload = {
            "memory_delta": {
                "status": "has_memory",
                "entries": [
                    {
                        "kind": "rule",
                        "title": "Prefer update existing PR",
                        "short_summary": "Review follow-up should update the current DD PR branch.",
                        "long_summary": "When review comments arrive on an existing DD PR, keep work on the same PR unless the route says otherwise.",
                        "keywords": ["dd-pr", "review-follow-up"],
                        "paths": ["src/robert_agent/runtime/cycle.py"],
                    }
                ],
            }
        }
        with closing(sqlite3.connect(db_path)) as conn, conn:
            status = project_memory.record_memory_delta(
                conn,
                result_payload=payload,
                workstream_id="ws-1",
                repo_id="repo-1",
                run_now=now,
            )
        self.assertEqual(status["status"], "recorded")

    def _insert_operator_state_examples(self, db_path):
        now = datetime.now(timezone.utc).isoformat()
        audit = {
            "ok": False,
            "status": "failed",
            "safe_error": "open_pr action missing fields: ['base']",
        }
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.executemany(
                """
                INSERT INTO workstreams(
                  workstream_id, repo_id, lifecycle, active_task_id, created_at, updated_at
                )
                VALUES (?, 'repo-1', ?, ?, ?, ?)
                """,
                [
                    ("ws-worker", "active", "task-worker", now, now),
                    ("ws-stale", "active", "task-stale", now, now),
                    ("ws-timeout", "failed", None, now, now),
                    ("ws-publish", "active", "task-publish", now, now),
                    ("ws-user", "waiting_for_user", "task-user", now, now),
                    ("ws-failed", "failed", None, now, now),
                ],
            )
            conn.executemany(
                """
                INSERT INTO tasks(
                  task_id, workstream_id, lifecycle, priority, created_at, updated_at
                )
                VALUES (?, ?, ?, 'P1', ?, ?)
                """,
                [
                    ("task-worker", "ws-worker", "running", now, now),
                    ("task-stale", "ws-stale", "running", now, now),
                    ("task-timeout", "ws-timeout", "failed", now, now),
                    ("task-publish", "ws-publish", "running", now, now),
                    ("task-user", "ws-user", "waiting_for_user", now, now),
                    ("task-failed", "ws-failed", "failed", now, now),
                ],
            )
            conn.execute(
                """
                INSERT INTO attempts(
                  attempt_id, task_id, attempt_no, status, started_at, heartbeat_at
                )
                VALUES ('attempt-worker', 'task-worker', 1, 'running', ?, ?)
                """,
                (now, now),
            )
            conn.executemany(
                """
                INSERT INTO attempts(
                  attempt_id, task_id, attempt_no, status, started_at, heartbeat_at, finished_at
                )
                VALUES (?, ?, 1, ?, ?, ?, ?)
                """,
                [
                    ("attempt-stale", "task-stale", "stale", now, now, None),
                    ("attempt-timeout", "task-timeout", "failed", now, now, now),
                ],
            )
            conn.execute(
                """
                INSERT INTO worker_results(result_id, task_id, attempt_id, output_type, consumed_event_fingerprints_json, verification_json, handoff, created_at, metadata_json)
                VALUES ('result-publish', 'task-publish', 'attempt-publish', 'comment_analysis', '["comment:1"]', '[]', 'ready', ?, json_set('{}', '$.result_contract', 'compact-v1'))
                """,
                (now,),
            )
            conn.execute(
                """
                INSERT INTO github_actions(
                  action_id, result_id, task_id, action_type, target_url,
                  audit_status, publish_status, created_at, metadata_json
                )
                VALUES (
                  'action-publish', 'result-publish', 'task-publish',
                  'comment', 'https://github.com/x/y/issues/1',
                  'accepted', 'not_published', ?, '{}'
                )
                """,
                (now,),
            )
            conn.execute(
                """
                INSERT INTO notifications(
                  notification_id, task_id, notification_type, channel, status,
                  created_at, metadata_json
                )
                VALUES (
                  'notification-failed', 'task-failed', 'worker_result_rejected',
                  'local', 'recorded', ?, ?
                )
                """,
                (now, json.dumps({"audit": audit, "result_id": "result-failed"}, sort_keys=True)),
            )
            conn.execute(
                """
                INSERT INTO notifications(
                  notification_id, task_id, notification_type, channel, status,
                  created_at, metadata_json
                )
                VALUES (
                  'notification-timeout', 'task-timeout', 'worker_timeout',
                  'local', 'recorded', ?, '{"attempt_id": "attempt-timeout"}'
                )
                """,
                (now,),
            )

    def _init_operator_overview_db(self, db_path):
        from robert_agent.persistence.schema import database as storage

        storage.init_database(db_path)
        now = datetime.now(timezone.utc).isoformat()
        audit = {
            "ok": False,
            "status": "failed",
            "safe_error": "open_pr action missing fields: ['base']",
        }
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root)
                VALUES ('repo-1', 'example/backend', 'robert-bot', 'master', '/repo', '/repo/.worktrees')
                """
            )
            conn.executemany(
                """
                INSERT INTO workstreams(
                  workstream_id, repo_id, lifecycle, active_task_id, created_at, updated_at
                )
                VALUES (?, 'repo-1', ?, ?, ?, ?)
                """,
                [
                    ("ws-worker", "active", "task-worker", now, now),
                    ("ws-stale", "active", "task-stale", now, now),
                    ("ws-timeout", "failed", None, now, now),
                    ("ws-publish", "active", "task-publish", now, now),
                    ("ws-user", "waiting_for_user", "task-user", now, now),
                    ("ws-failed", "failed", None, now, now),
                ],
            )
            conn.executemany(
                """
                INSERT INTO tasks(
                  task_id, workstream_id, lifecycle, priority, created_at, updated_at
                )
                VALUES (?, ?, ?, 'P1', ?, ?)
                """,
                [
                    ("task-worker", "ws-worker", "running", now, now),
                    ("task-stale", "ws-stale", "running", now, now),
                    ("task-timeout", "ws-timeout", "failed", now, now),
                    ("task-publish", "ws-publish", "running", now, now),
                    ("task-user", "ws-user", "waiting_for_user", now, now),
                    ("task-failed", "ws-failed", "failed", now, now),
                ],
            )
            conn.executemany(
                """
                INSERT INTO attempts(
                  attempt_id, task_id, attempt_no, status, started_at, heartbeat_at, finished_at
                )
                VALUES (?, ?, 1, ?, ?, ?, ?)
                """,
                [
                    ("attempt-worker", "task-worker", "running", now, now, None),
                    ("attempt-stale", "task-stale", "stale", now, now, None),
                    ("attempt-timeout", "task-timeout", "failed", now, now, now),
                ],
            )
            conn.execute(
                """
                INSERT INTO worker_results(result_id, task_id, attempt_id, output_type, consumed_event_fingerprints_json, verification_json, handoff, created_at, metadata_json)
                VALUES ('result-publish', 'task-publish', 'attempt-publish', 'comment_analysis', '["comment:1"]', '[]', 'ready', ?, json_set('{}', '$.result_contract', 'compact-v1'))
                """,
                (now,),
            )
            conn.execute(
                """
                INSERT INTO github_actions(
                  action_id, result_id, task_id, action_type, target_url,
                  audit_status, publish_status, created_at, metadata_json
                )
                VALUES (
                  'action-publish', 'result-publish', 'task-publish',
                  'comment', 'https://github.com/x/y/issues/1',
                  'accepted', 'not_published', ?, '{}'
                )
                """,
                (now,),
            )
            conn.executemany(
                """
                INSERT INTO notifications(
                  notification_id, task_id, notification_type, channel, status,
                  created_at, metadata_json
                )
                VALUES (?, ?, ?, 'local', 'recorded', ?, ?)
                """,
                [
                    (
                        "notification-timeout",
                        "task-timeout",
                        "worker_timeout",
                        now,
                        '{"attempt_id": "attempt-timeout"}',
                    ),
                    (
                        "notification-failed",
                        "task-failed",
                        "worker_result_rejected",
                        now,
                        json.dumps({"audit": audit, "result_id": "result-failed"}, sort_keys=True),
                    ),
                ],
            )

    def _record_repeated_worker_resume_notifications(self, db_path):
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.executemany(
                """
                INSERT INTO notifications(
                  notification_id, task_id, notification_type, channel, status,
                  created_at, metadata_json
                )
                VALUES (?, 'task-worker', 'worker_resume_prepared', 'local', 'recorded', ?, ?)
                """,
                [
                    (
                        "notification-resume-1",
                        now,
                        json.dumps(
                            {
                                "attempt_id": "attempt-worker-old-1",
                                "resume_attempt_id": "attempt-worker",
                                "recovery_artifact_path": "/tmp/recovery-1.json",
                            },
                            sort_keys=True,
                        ),
                    ),
                    (
                        "notification-resume-2",
                        now,
                        json.dumps(
                            {
                                "attempt_id": "attempt-worker-old-2",
                                "resume_attempt_id": "attempt-worker",
                                "recovery_artifact_path": "/tmp/recovery-2.json",
                            },
                            sort_keys=True,
                        ),
                    ),
                ],
            )

    def _record_result_pending_audit_task(self, db_path):
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO workstreams(
                  workstream_id, repo_id, lifecycle, active_task_id, created_at, updated_at
                )
                VALUES ('ws-result-pending', 'repo-1', 'active', 'task-result-pending', ?, ?)
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO tasks(
                  task_id, workstream_id, lifecycle, priority, created_at, updated_at
                )
                VALUES ('task-result-pending', 'ws-result-pending', 'running', 'P1', ?, ?)
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO attempts(
                  attempt_id, task_id, attempt_no, status, started_at, heartbeat_at, finished_at
                )
                VALUES ('attempt-result-pending', 'task-result-pending', 1, 'completed', ?, ?, ?)
                """,
                (now, now, now),
            )
            conn.execute(
                """
                INSERT INTO worker_results(result_id, task_id, attempt_id, output_type, consumed_event_fingerprints_json, verification_json, handoff, created_at, metadata_json)
                VALUES ('result-pending-audit', 'task-result-pending', 'attempt-result-pending', 'classification_result', '["comment:1"]', '[]', 'ready', ?, json_set('{}', '$.result_contract', 'compact-v1'))
                """,
                (now,),
            )

    def _record_rejected_result(self, db_path):
        audit = {
            "ok": False,
            "status": "failed",
            "safe_error": "open_pr action missing fields: ['base']",
        }
        metadata = {
            "audit": audit,
            "recorded_by": "robert.worker",
            "used_skills": [],
        }
        notification_metadata = {
            "task_id": "task-1",
            "result_id": "result-1",
            "audit": audit,
        }
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                "UPDATE worker_results SET metadata_json=json_set(?, '$.result_contract', 'compact-v1') WHERE result_id = 'result-1'",
                (json.dumps(metadata, sort_keys=True),),
            )
            conn.execute(
                """
                UPDATE github_actions
                SET audit_status = 'failed',
                    publish_status = 'not_published'
                WHERE action_id = 'action-1'
                """,
            )
            conn.execute(
                """
                INSERT INTO notifications(
                  notification_id, task_id, notification_type, channel, status,
                  created_at, metadata_json
                )
                VALUES (
                  'notification-rejected-1', 'task-1', 'worker_result_rejected',
                  'local', 'recorded', ?, ?
                )
                """,
                (now, json.dumps(notification_metadata, sort_keys=True)),
            )

    def _record_ignored_untrusted_event(self, db_path):
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO github_events(
                  event_id, repo_id, source_id, event_fingerprint, event_type,
                  actor_login, author_association, authorization_status,
                  event_at, payload_json
                )
                VALUES (
                  'event-ignored-1', 'repo-1', 'source-1', 'comment:ignored-1',
                  'comment', 'external-user', NULL, 'ignored_untrusted_trigger',
                  ?, ?
                )
                """,
                (now, json.dumps({"intent": "bug_fix"}, sort_keys=True)),
            )

    def _record_authorized_trigger_unattached_event(self, db_path):
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO github_events(
                  event_id, repo_id, source_id, event_fingerprint, event_type,
                  actor_login, author_association, authorization_status,
                  event_at, payload_json
                )
                VALUES (
                  'event-authorized-trigger', 'repo-1', 'source-1',
                  'comment:authorized-trigger', 'comment', 'wklken', 'OWNER',
                  'authorized_trigger', ?, ?
                )
                """,
                (now, json.dumps({"intent": "bug_fix"}, sort_keys=True)),
            )

    def _record_pending_authorization_event(self, db_path):
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO github_events(
                  event_id, repo_id, source_id, event_fingerprint, event_type,
                  actor_login, author_association, authorization_status,
                  event_at, payload_json
                )
                VALUES (
                  'event-pending-authorization', 'repo-1', 'source-1',
                  'comment:pending-authorization', 'comment', 'external-user',
                  NULL, 'pending_authorization', ?, ?
                )
                """,
                (
                    now,
                    json.dumps(
                        {"authorization_lookup_complete": False, "intent": "bug_fix"},
                        sort_keys=True,
                    ),
                ),
            )

    def _record_publish_failure(self, db_path):
        metadata = {
            "body": self._dd_comment_body("ready"),
            "publish": {
                "status": "publish_failed",
                "failed_at": "2026-06-17T04:00:00+00:00",
                "safe_error": "gh: authentication failed",
                "command": ["gh", "api", "repos/x/y/issues/1/comments", "-f", "body=ready"],
            },
        }
        notification_metadata = {
            "action_id": "action-1",
            "action_type": "comment",
            "safe_error": "gh: authentication failed",
            "command": ["gh", "api", "repos/x/y/issues/1/comments", "-f", "body=ready"],
        }
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE github_actions
                SET audit_status = 'accepted',
                    publish_status = 'not_published',
                    metadata_json = ?
                WHERE action_id = 'action-1'
                """,
                (json.dumps(metadata, sort_keys=True),),
            )
            conn.execute(
                """
                INSERT INTO notifications(
                  notification_id, task_id, notification_type, channel, status,
                  created_at, metadata_json
                )
                VALUES (
                  'notification-publish-failed-1', 'task-1', 'github_publish_failed',
                  'local', 'recorded', ?, ?
                )
                """,
                (now, json.dumps(notification_metadata, sort_keys=True)),
            )

    def _record_unrelated_latest_notification(self, db_path):
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO notifications(
                  notification_id, task_id, notification_type, channel, status,
                  created_at, metadata_json
                )
                VALUES (
                  'notification-unrelated-latest', 'task-1', 'worker_stale',
                  'local', 'recorded', '2099-01-01T00:00:00+00:00',
                  '{"attempt_id": "attempt-1"}'
                )
                """
            )

    def _record_skipped_publish_action(self, db_path):
        metadata = {
            "publish": {
                "status": "skipped",
                "skipped_at": "2026-06-17T04:00:00+00:00",
                "safe_error": "publisher does not support action_type close_issue",
            },
        }
        notification_metadata = {
            "action_id": "action-1",
            "action_type": "close_issue",
            "safe_error": "publisher does not support action_type close_issue",
            "command": [],
        }
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE github_actions
                SET action_type = 'close_issue',
                    audit_status = 'accepted',
                    publish_status = 'skipped',
                    metadata_json = ?
                WHERE action_id = 'action-1'
                """,
                (json.dumps(metadata, sort_keys=True),),
            )
            conn.execute(
                """
                INSERT INTO notifications(
                  notification_id, task_id, notification_type, channel, status,
                  created_at, metadata_json
                )
                VALUES (
                  'notification-publish-skipped-1', 'task-1', 'github_publish_skipped',
                  'local', 'recorded', ?, ?
                )
                """,
                (now, json.dumps(notification_metadata, sort_keys=True)),
            )

    def _mark_summary_task_waiting_for_user(self, db_path):
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE tasks
                SET lifecycle = 'waiting_for_user',
                    updated_at = ?
                WHERE task_id = 'task-1'
                """,
                (now,),
            )
            conn.execute(
                """
                UPDATE workstreams
                SET lifecycle = 'waiting_for_user',
                    active_task_id = 'task-1',
                    updated_at = ?
                WHERE workstream_id = 'ws-1'
                """,
                (now,),
            )

    def _record_additional_skipped_publish_action(self, db_path):
        metadata = {
            "publish": {
                "status": "skipped",
                "skipped_at": "2026-06-17T04:00:00+00:00",
                "safe_error": "publisher does not support action_type close_issue",
            },
        }
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO github_actions(
                  action_id, result_id, task_id, action_type, target_url,
                  audit_status, publish_status, created_at, metadata_json
                )
                VALUES (
                  'action-skipped-1', 'result-1', 'task-1', 'close_issue',
                  'https://github.com/x/y/issues/1', 'accepted',
                  'skipped', ?, ?
                )
                """,
                (now, json.dumps(metadata, sort_keys=True)),
            )

    def _record_additional_publish_failure_action(self, db_path):
        metadata = {
            "publish": {
                "status": "publish_failed",
                "failed_at": "2026-06-17T04:00:00+00:00",
                "safe_error": "gh: authentication failed",
                "command": ["gh", "api", "repos/x/y/issues/1/comments", "-f", "body=ready"],
            },
        }
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO github_actions(
                  action_id, result_id, task_id, action_type, target_url,
                  audit_status, publish_status, created_at, metadata_json
                )
                VALUES (
                  'action-publish-failed-1', 'result-1', 'task-1', 'comment',
                  'https://github.com/x/y/issues/1', 'accepted',
                  'not_published', ?, ?
                )
                """,
                (now, json.dumps(metadata, sort_keys=True)),
            )

    def _record_recovered_publish_failure(self, db_path):
        metadata = {
            "body": self._dd_comment_body("ready"),
            "publish": {
                "status": "published",
                "published_at": "2026-06-17T04:10:00+00:00",
                "response": {"id": 987},
                "deduplicated": False,
            },
        }
        notification_metadata = {
            "action_id": "action-1",
            "action_type": "comment",
            "safe_error": "gh: authentication failed",
            "command": ["gh", "api", "repos/x/y/issues/1/comments", "-f", "body=ready"],
        }
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE github_actions
                SET audit_status = 'accepted',
                    publish_status = 'published',
                    metadata_json = ?
                WHERE action_id = 'action-1'
                """,
                (json.dumps(metadata, sort_keys=True),),
            )
            conn.execute(
                """
                INSERT INTO notifications(
                  notification_id, task_id, notification_type, channel, status,
                  created_at, metadata_json
                )
                VALUES (
                  'notification-publish-recovered-1', 'task-1', 'github_publish_failed',
                  'local', 'recorded', ?, ?
                )
                """,
                (now, json.dumps(notification_metadata, sort_keys=True)),
            )

    def _record_worker_result_evidence(self, db_path):
        metadata = {
            "audit": {
                "ok": True,
                "status": "accepted",
                "planned_github_actions": ["comment"],
            },
            "used_skills": ["fast-code-path"],
        }
        verification = [{"command": "python -m unittest", "status": "passed"}]
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE worker_results
                SET metadata_json=json_set(?, '$.result_contract', 'compact-v1'),
                    verification_json = ?
                WHERE result_id = 'result-1'
                """,
                (
                    json.dumps(metadata, sort_keys=True),
                    json.dumps(verification, sort_keys=True),
                ),
            )
