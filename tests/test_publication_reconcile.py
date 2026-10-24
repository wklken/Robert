from unittest import mock
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from robert_agent import publication_reconcile, publish, storage, execution_store


class Completed:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


class PublicationReconcileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "robert.sqlite3"
        storage.init_database(self.db_path)
        now = "2026-08-02T00:00:00+00:00"
        scope = {
            "repo": "x/y",
            "sources": [{"source_type": "issue", "number": 1}],
        }
        metadata = {
            "audit": {"status": "accepted", "action_scope": scope}
        }
        body = "<!-- robert-comment task_id=task-1 attempt_id=attempt-1 -->\nready"
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root) VALUES ('repo-1', 'x/y', 'bot', 'main', '/repo', '/worktrees')"
            )
            conn.execute(
                "INSERT INTO workstreams(workstream_id, repo_id, lifecycle, created_at, updated_at) VALUES ('ws-1', 'repo-1', 'active', ?, ?)",
                (now, now),
            )
            conn.execute(
                "INSERT INTO tasks(task_id, workstream_id, lifecycle, created_at, updated_at) VALUES ('task-1', 'ws-1', 'queued', ?, ?)",
                (now, now),
            )
            conn.execute(
                "INSERT INTO attempts(attempt_id, task_id, attempt_no, status) VALUES ('attempt-1', 'task-1', 1, 'completed')"
            )
            conn.execute(
                "INSERT INTO worker_results(result_id, task_id, attempt_id, output_type, created_at, metadata_json) VALUES ('result-1', 'task-1', 'attempt-1', 'comment_analysis', ?, ?)",
                (now, json.dumps(metadata, sort_keys=True)),
            )
            conn.execute(
                "INSERT INTO github_actions(action_id, result_id, task_id, action_type, target_url, audit_status, publish_status, created_at, metadata_json) VALUES ('action-1', 'result-1', 'task-1', 'comment', 'https://github.com/x/y/issues/1', 'accepted', 'not_published', ?, ?)",
                (now, json.dumps({"body": body}, sort_keys=True)),
            )
        self.body = body

    def test_comment_crash_backfills_without_duplicate_write(self):
        calls = []
        crashed = {"value": False}

        def first_runner(command, **kwargs):
            calls.append(command)
            if command[2].endswith("comments?per_page=100"):
                return Completed("[]")
            crashed["value"] = True
            raise RuntimeError("simulated crash after comment creation")

        with self.assertRaises(RuntimeError):
            publish.publish_ready_actions(self.db_path, run_command=first_runner)
        self.assertTrue(crashed["value"])

        def retry_runner(command, **kwargs):
            calls.append(command)
            return Completed(
                json.dumps(
                    [
                        {
                            "id": 7,
                            "body": self.body,
                            "html_url": "https://github.com/x/y/issues/1#issuecomment-7",
                        }
                    ]
                )
            )

        result = publish.publish_ready_actions(self.db_path, run_command=retry_runner)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["deduplicated_count"], 1)
        write_command = [
            "gh",
            "api",
            "repos/x/y/issues/1/comments",
            "-f",
            f"body={self.body}",
        ]
        self.assertEqual(calls.count(write_command), 1)
        with closing(sqlite3.connect(self.db_path)) as conn:
            status = conn.execute(
                "SELECT publish_status FROM github_actions WHERE action_id = 'action-1'"
            ).fetchone()[0]
            intent = conn.execute(
                "SELECT status FROM publication_intents WHERE action_id = 'action-1'"
            ).fetchone()[0]
        self.assertEqual(status, "published")
        self.assertEqual(intent, "published")

    def test_conflicting_comment_markers_are_unknown(self):
        action = {
            "action_id": "action-1",
            "action_type": "comment",
            "target_url": "https://github.com/x/y/issues/1",
            "metadata_json": json.dumps({"body": self.body}),
        }

        result = publication_reconcile.classify_remote_state(
            action,
            {"repo": "x/y", "sources": [{"source_type": "issue", "number": 1}]},
            lambda *_args, **_kwargs: Completed(
                json.dumps(
                    [
                        {"id": 1, "body": self.body},
                        {"id": 2, "body": self.body},
                    ]
                )
            ),
        )
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason_code"], "comment_marker_conflict")

    def test_open_pr_matching_marker_and_owner_confirms_effect(self):
        body = "<!-- robert-workstream task_id=task-1 -->\nPR"
        action = {
            "action_id": "action-pr",
            "action_type": "open_pr",
            "target_url": "",
            "metadata_json": json.dumps(
                {
                    "repo": "x/y",
                    "head": "feature",
                    "base": "main",
                    "head_owner": "x",
                    "body": body,
                }
            ),
        }
        commands = []

        def runner(command, **kwargs):
            commands.append(command)
            if command[:3] == ["gh", "pr", "list"]:
                return Completed(
                    json.dumps(
                        [
                            {
                                "number": 9,
                                "url": "https://github.com/x/y/pull/9",
                                "baseRefName": "main",
                                "headRefName": "feature",
                                "headRepositoryOwner": {"login": "x"},
                            }
                        ]
                    )
                )
            return Completed(
                json.dumps(
                    {
                        "number": 9,
                        "url": "https://github.com/x/y/pull/9",
                        "baseRefName": "main",
                        "headRefName": "feature",
                        "headRepositoryOwner": {"login": "x"},
                        "body": body,
                    }
                )
            )

        result = publication_reconcile.classify_remote_state(
            action, {"repo": "x/y", "sources": []}, runner
        )
        self.assertEqual(result["status"], "confirmed_effect")
        self.assertEqual(result["reason_code"], "open_pr_marker_match")
        self.assertEqual(len(commands), 2)

    def test_new_branch_push_absence_and_existing_push_conflict(self):
        action = {
            "action_id": "action-push",
            "action_type": "push_existing_pr",
            "target_url": "",
            "metadata_json": json.dumps(
                {"worktree_path": "/worktree", "branch": "feature"}
            ),
        }
        scope = {"repo": "x/y", "branch_name": "feature", "result_head_sha": "a" * 40}
        absent = publication_reconcile.classify_remote_state(
            action,
            scope,
            lambda *_args, **_kwargs: Completed(""),
        )
        self.assertEqual(absent["status"], "confirmed_absent")
        self.assertEqual(absent["reason_code"], "remote_branch_lookup_complete_absent")

        conflict = publication_reconcile.classify_remote_state(
            action,
            {**scope, "existing_pr_head_sha": "b" * 40, "pr_number": 9, "observed_head_sha": "b" * 40, "observed_base_sha": "c" * 40},
            lambda *_args, **_kwargs: Completed(f"{'d' * 40}\trefs/heads/feature\n"),
        )
        self.assertEqual(conflict["status"], "unknown")
        self.assertEqual(conflict["reason_code"], "remote_sha_conflict")

    def test_query_failure_is_unknown_with_bounded_reason(self):
        action = {
            "action_id": "action-1",
            "action_type": "comment",
            "target_url": "https://github.com/x/y/issues/1",
            "metadata_json": json.dumps({"body": self.body}),
        }

        def fail(*_args, **_kwargs):
            raise OSError("credentials must never be retained")

        result = publication_reconcile.classify_remote_state(
            action, {"repo": "x/y", "sources": []}, fail
        )
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason_code"], "comment_query_failed")
        self.assertNotIn("credentials", json.dumps(result).lower())

    def test_unknown_retry_creates_one_attention_notification(self):
        calls = []

        class Failed:
            returncode = 1
            stdout = ""
            stderr = "network unavailable"

        def runner(command, **kwargs):
            calls.append(command)
            return Failed()

        first = publish.publish_ready_actions(self.db_path, run_command=runner)
        # Advance to the persisted retry deadline; immediate retry is now forbidden.
        with closing(sqlite3.connect(self.db_path)) as retry_conn:
            due = json.loads(retry_conn.execute("SELECT metadata_json FROM github_actions WHERE action_id='action-1'").fetchone()[0])['publish']['next_retry_at']
        retry_clock = mock.patch.object(publish, '_now', return_value=due)
        retry_clock.start()
        self.addCleanup(retry_clock.stop)
        second = publish.publish_ready_actions(self.db_path, run_command=runner)
        # Keep the durable outcomes visible when this focused regression fails.
        self.assertIn(first["status"], {"publish_failed"})
        self.assertFalse(first["ok"])
        self.assertFalse(second["ok"])
        with closing(sqlite3.connect(self.db_path)) as conn:
            count, status = conn.execute(
                "SELECT COUNT(*), MAX(status) FROM notifications WHERE notification_type = 'workflow_attention_required'"
            ).fetchone()
            intent = conn.execute(
                "SELECT status FROM publication_intents WHERE action_id = 'action-1'"
            ).fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(status, "recorded")
        self.assertEqual(intent, "attention")

    def test_open_pr_crash_backfills_without_create_retry(self):
        body = "<!-- robert-workstream task_id=task-1 -->\nPR"
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE worker_results SET metadata_json = ? WHERE result_id = 'result-1'",
                (json.dumps({"audit": {"status": "accepted", "action_scope": {"repo": "x/y", "sources": []}}}),),
            )
            conn.execute(
                "UPDATE github_actions SET action_type = 'open_pr', target_url = NULL, metadata_json = ? WHERE action_id = 'action-1'",
                (
                    json.dumps(
                        {"repo": "x/y", "head": "feature", "base": "main", "body": body, "title": "Fix"}
                    ),
                ),
            )
        calls = []

        def first_runner(command, **kwargs):
            calls.append(command)
            if command[:3] == ["gh", "pr", "list"]:
                return Completed("[]")
            raise RuntimeError("crash after PR create")

        with self.assertRaises(RuntimeError):
            publish.publish_ready_actions(self.db_path, run_command=first_runner)

        def retry_runner(command, **kwargs):
            calls.append(command)
            if command[:3] == ["gh", "pr", "list"]:
                return Completed(
                    json.dumps(
                        [
                            {
                                "number": 4,
                                "url": "https://github.com/x/y/pull/4",
                                "baseRefName": "main",
                                "headRefName": "feature",
                                "headRepositoryOwner": {"login": "x"},
                            }
                        ]
                    )
                )
            return Completed(
                json.dumps(
                    {
                        "number": 4,
                        "url": "https://github.com/x/y/pull/4",
                        "baseRefName": "main",
                        "headRefName": "feature",
                        "headRepositoryOwner": {"login": "x"},
                        "body": body,
                    }
                )
            )

        result = publish.publish_ready_actions(self.db_path, run_command=retry_runner)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["deduplicated_count"], 1)
        self.assertEqual(sum(command[:3] == ["gh", "pr", "create"] for command in calls), 1)

    def test_open_pr_scope_conflict_reconciles_to_attention_without_remote_query(self):
        body = "<!-- robert-workstream task_id=task-1 -->\nPR"
        scope = {
            "repo": "x/y",
            "sources": [],
            "base_branch": "main",
            "branch_name": "feature",
            "worktree_path": str(Path(self.tmp.name) / "worktree"),
            "remote": "origin",
        }
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE worker_results SET metadata_json = ? WHERE result_id = 'result-1'",
                (json.dumps({"audit": {"status": "accepted", "action_scope": scope}}),),
            )
            conn.execute(
                "UPDATE github_actions SET action_type = 'open_pr', target_url = NULL, "
                "metadata_json = ? WHERE action_id = 'action-1'",
                (
                    json.dumps(
                        {
                            "repo": "attacker/other",
                            "head": "feature",
                            "base": "main",
                            "body": body,
                            "title": "Fix",
                        }
                    ),
                ),
            )
        calls = []

        def runner(command, **_kwargs):
            calls.append(command)
            return Completed("[]")

        first = publish.publish_ready_actions(self.db_path, run_command=runner)
        second = publish.publish_ready_actions(self.db_path, run_command=runner)

        self.assertFalse(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(second["pending_count"], 0)
        self.assertEqual(calls, [])
        with closing(sqlite3.connect(self.db_path)) as conn:
            intent_status = conn.execute(
                "SELECT status FROM publication_intents WHERE action_id = 'action-1'"
            ).fetchone()[0]
        self.assertEqual(intent_status, "attention")

    def test_open_pr_crash_does_not_accept_matching_marker_from_wrong_head_owner(self):
        body = "<!-- robert-workstream task_id=task-1 -->\nPR"
        scope = {
            "repo": "x/y",
            "sources": [],
            "base_branch": "main",
            "branch_name": "feature",
            "worktree_path": str(Path(self.tmp.name) / "worktree"),
            "remote": "origin",
        }
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE worker_results SET metadata_json = ? WHERE result_id = 'result-1'",
                (json.dumps({"audit": {"status": "accepted", "action_scope": scope}}),),
            )
            conn.execute(
                "UPDATE github_actions SET action_type = 'open_pr', target_url = NULL, "
                "metadata_json = ? WHERE action_id = 'action-1'",
                (
                    json.dumps(
                        {
                            "repo": "x/y",
                            "head": "feature",
                            "base": "main",
                            "body": body,
                            "title": "Fix",
                        }
                    ),
                ),
            )
        calls = []

        def first_runner(command, **_kwargs):
            calls.append(command)
            if command[:3] == ["gh", "pr", "list"]:
                return Completed("[]")
            raise RuntimeError("crash after PR create")

        with self.assertRaisesRegex(RuntimeError, "after PR create"):
            publish.publish_ready_actions(self.db_path, run_command=first_runner)

        def retry_runner(command, **_kwargs):
            calls.append(command)
            candidate = {
                "number": 4,
                "url": "https://github.com/x/y/pull/4",
                "baseRefName": "main",
                "headRefName": "feature",
                "headRepositoryOwner": {"login": "attacker"},
            }
            if command[:3] == ["gh", "pr", "list"]:
                return Completed(json.dumps([candidate]))
            return Completed(json.dumps({**candidate, "body": body}))

        result = publish.publish_ready_actions(self.db_path, run_command=retry_runner)

        self.assertFalse(result["ok"])
        self.assertEqual(sum(command[:3] == ["gh", "pr", "create"] for command in calls), 1)
        with closing(sqlite3.connect(self.db_path)) as conn:
            intent_status = conn.execute(
                "SELECT status FROM publication_intents WHERE action_id = 'action-1'"
            ).fetchone()[0]
        self.assertEqual(intent_status, "attention")

    def test_new_branch_push_crash_backfills_from_result_sha(self):
        source_sha = "a" * 40
        worktree = Path(self.tmp.name) / "worktree"
        worktree.mkdir()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE worker_results SET metadata_json = ? WHERE result_id = 'result-1'",
                (
                    json.dumps(
                        {
                            "audit": {
                                "status": "accepted",
                                "action_scope": {
                                    "repo": "x/y",
                                    "sources": [],
                                    "worktree_path": str(worktree),
                                    "branch_name": "feature",
                                    "result_head_sha": source_sha,
                                },
                            }
                        }
                    ),
                ),
            )
            conn.execute(
                "UPDATE github_actions SET action_type = 'push_existing_pr', metadata_json = ? WHERE action_id = 'action-1'",
                (json.dumps({"worktree_path": str(worktree), "branch": "feature", "remote": "origin"}),),
            )
        calls = []

        def first_runner(command, **kwargs):
            calls.append(command)
            if command[:2] == ["git", "push"]:
                raise RuntimeError("crash after push")
            raise AssertionError(command)

        with self.assertRaises(RuntimeError):
            publish.publish_ready_actions(self.db_path, run_command=first_runner)

        def retry_runner(command, **kwargs):
            calls.append(command)
            return Completed(f"{source_sha}\trefs/heads/feature\n")

        result = publish.publish_ready_actions(self.db_path, run_command=retry_runner)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["deduplicated_count"], 1)
        self.assertEqual(sum(command[:2] == ["git", "push"] for command in calls), 1)
        self.assertIn(
            ["git", "push", "origin", f"{source_sha}:refs/heads/feature"],
            calls,
        )

    def test_derived_push_sha_survives_confirmed_absence_retry_crash(self):
        source_sha = "a" * 40
        worktree = Path(self.tmp.name) / "worktree"
        worktree.mkdir()
        scope = {
            "repo": "x/y",
            "sources": [],
            "worktree_path": str(worktree),
            "branch_name": "feature",
            "remote": "origin",
        }
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE worker_results SET metadata_json = ? WHERE result_id = 'result-1'",
                (json.dumps({"audit": {"status": "accepted", "action_scope": scope}}),),
            )
            conn.execute(
                "UPDATE github_actions SET action_type = 'push_existing_pr', "
                "target_url = NULL, metadata_json = ? WHERE action_id = 'action-1'",
                (
                    json.dumps(
                        {
                            "worktree_path": str(worktree),
                            "branch": "feature",
                            "remote": "origin",
                        }
                    ),
                ),
            )

        state = {"push_count": 0, "remote_effect": False}

        def runner(command, **_kwargs):
            if command == ["git", "rev-parse", "HEAD"]:
                return Completed(source_sha + "\n")
            if command == ["git", "ls-remote", "--heads", "origin", "feature"]:
                stdout = (
                    f"{source_sha}\trefs/heads/feature\n"
                    if state["remote_effect"]
                    else ""
                )
                return Completed(stdout)
            if command[:2] == ["git", "push"]:
                state["push_count"] += 1
                if state["push_count"] == 1:
                    return Completed("", returncode=1, stderr="lost response")
                state["remote_effect"] = True
                raise RuntimeError("crash after confirmed-absent retry push")
            raise AssertionError(command)

        first = publish.publish_ready_actions(self.db_path, run_command=runner)
        self.assertFalse(first["ok"])
        # Advance to the persisted retry deadline; immediate retry is now forbidden.
        with closing(sqlite3.connect(self.db_path)) as retry_conn:
            due = json.loads(retry_conn.execute("SELECT metadata_json FROM github_actions WHERE action_id='action-1'").fetchone()[0])['publish']['next_retry_at']
        retry_clock = mock.patch.object(publish, '_now', return_value=due)
        retry_clock.start()
        self.addCleanup(retry_clock.stop)
        with self.assertRaisesRegex(RuntimeError, "confirmed-absent retry"):
            publish.publish_ready_actions(self.db_path, run_command=runner)

        third = publish.publish_ready_actions(self.db_path, run_command=runner)

        self.assertTrue(third["ok"], third)
        self.assertEqual(third["deduplicated_count"], 1)
        self.assertEqual(state["push_count"], 2)

    def test_existing_pr_push_absence_retries_once_then_backfills(self):
        source_sha = "a" * 40
        original_sha = "b" * 40
        worktree = Path(self.tmp.name) / "worktree"
        worktree.mkdir()
        scope = {
            "repo": "x/y",
            "sources": [],
            "worktree_path": str(worktree),
            "branch_name": "feature",
            "remote": "origin",
            "existing_pr_head_sha": original_sha,
            "result_head_sha": source_sha,
            "task_kind": "ci_remediation",
            "episode_id": "episode-1",
            "pr_number": 4,
            "observed_head_sha": original_sha,
            "observed_base_sha": "c" * 40,
        }
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE worker_results SET metadata_json = ? WHERE result_id = 'result-1'",
                (json.dumps({"audit": {"status": "accepted", "action_scope": scope}}),),
            )
            conn.execute(
                "UPDATE tasks SET task_kind = 'ci_remediation', metadata_json = '{\"remediation_episode_id\":\"episode-1\"}' WHERE task_id = 'task-1'"
            )
            conn.execute(
                "INSERT INTO pr_remediation_episodes(episode_id, workstream_id, episode_kind, subject_key, observed_head_sha, observed_base_sha, status, first_seen_at, updated_at, last_task_id) VALUES ('episode-1', 'ws-1', 'ci', 'x/y!4', ?, ?, 'remediating', 'now', 'now', 'task-1')",
                (original_sha, "c" * 40),
            )
            conn.execute(
                "UPDATE github_actions SET action_type = 'push_existing_pr', target_url = 'https://github.com/x/y/pull/4', metadata_json = ? WHERE action_id = 'action-1'",
                (json.dumps({"worktree_path": str(worktree), "branch": "feature", "remote": "origin"}),),
            )
        calls = []
        phase = {"run": 0}

        def runner(command, **kwargs):
            calls.append(command)
            if command == ["gh", "api", "repos/x/y/pulls/4"]:
                return Completed(json.dumps({"state": "open", "merged": False, "head": {"sha": original_sha}, "base": {"sha": "c" * 40}}))
            if command[:3] == ["git", "ls-remote", "--heads"]:
                return Completed(f"{original_sha}\trefs/heads/feature\n")
            if command == ["git", "rev-parse", "HEAD"]:
                return Completed(source_sha + "\n")
            if command[:2] == ["git", "push"]:
                phase["run"] += 1
                if phase["run"] == 1:
                    return Completed("", returncode=1, stderr="lost response")
                return Completed("pushed")
            raise AssertionError(command)

        first = publish.publish_ready_actions(self.db_path, run_command=runner)
        # Advance to the persisted retry deadline; immediate retry is now forbidden.
        with closing(sqlite3.connect(self.db_path)) as retry_conn:
            due = json.loads(retry_conn.execute("SELECT metadata_json FROM github_actions WHERE action_id='action-1'").fetchone()[0])['publish']['next_retry_at']
        retry_clock = mock.patch.object(publish, '_now', return_value=due)
        retry_clock.start()
        self.addCleanup(retry_clock.stop)
        second = publish.publish_ready_actions(self.db_path, run_command=runner)
        self.assertFalse(first["ok"])
        self.assertTrue(second["ok"], second)
        self.assertEqual(phase["run"], 2)

    def test_stale_non_active_binding_blocks_confirmed_absence_retry(self):
        source_sha = "a" * 40
        worktree = Path(self.tmp.name) / "worktree"
        worktree.mkdir()
        scope = {"repo": "x/y", "sources": [], "worktree_path": str(worktree), "branch_name": "feature", "result_head_sha": source_sha}
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE worker_results SET metadata_json = ? WHERE result_id = 'result-1'", (json.dumps({"audit": {"status": "accepted", "action_scope": scope}}),))
            conn.execute("UPDATE github_actions SET action_type = 'push_existing_pr', metadata_json = ? WHERE action_id = 'action-1'", (json.dumps({"worktree_path": str(worktree), "branch": "feature"}),))
            binding = execution_store.ensure_subject(conn, 'task', 'task-1', 'human_request', 'now')
            execution_store.close_binding(conn, "task", "task-1", "now")
            execution_store.prepare_publication_intent(conn, "action-1", "k", "f", "now")
            execution_store.transition_publication_intent(conn, binding["workflow_id"] or "publication-" + "0" * 24, "executing", "now") if False else None
            publication_id = conn.execute("SELECT publication_id FROM publication_intents WHERE action_id='action-1'").fetchone()[0]
            execution_store.transition_publication_intent(conn, publication_id, "executing", "now")
        calls = []
        result = publish.publish_ready_actions(self.db_path, run_command=lambda command, **kwargs: calls.append(command) or Completed(""))
        self.assertFalse(result["ok"])
        self.assertEqual(calls, [["git", "ls-remote", "--heads", "origin", "feature"]])

    def test_missing_or_malformed_scope_performs_zero_writes(self):
        from robert_agent import execution_store

        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            execution_store.ensure_subject(conn, 'task', 'task-1', 'human_request', '2026-08-02T00:00:00+00:00')
            conn.execute("UPDATE worker_results SET metadata_json = ? WHERE result_id = 'result-1'", (json.dumps({"audit": {"status": "accepted", "action_scope": []}}),))
        calls = []
        result = publish.publish_ready_actions(self.db_path, run_command=lambda command, **kwargs: calls.append(command) or (_ for _ in ()).throw(AssertionError(command)))
        self.assertFalse(result["ok"])
        self.assertEqual(calls, [])
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM notifications WHERE notification_type='workflow_attention_required'").fetchone()[0], 1)
            binding_status = conn.execute(
                "SELECT binding_status FROM execution_subjects "
                "WHERE subject_type = 'task' AND subject_id = 'task-1'"
            ).fetchone()[0]
        self.assertEqual(binding_status, "attention")

    def test_empty_accepted_scope_performs_zero_remote_calls(self):
        from robert_agent import execution_store

        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            execution_store.ensure_subject(conn, 'task', 'task-1', 'human_request', '2026-08-02T00:00:00+00:00')
            conn.execute(
                "UPDATE worker_results SET metadata_json = ? WHERE result_id = 'result-1'",
                (json.dumps({"audit": {"status": "accepted", "action_scope": {}}}),),
            )
        calls = []

        result = publish.publish_ready_actions(
            self.db_path,
            run_command=lambda command, **_kwargs: calls.append(command)
            or Completed("[]"),
        )

        self.assertFalse(result["ok"])
        self.assertEqual(calls, [])
        with closing(sqlite3.connect(self.db_path)) as conn:
            binding_status = conn.execute(
                "SELECT binding_status FROM execution_subjects "
                "WHERE subject_type = 'task' AND subject_id = 'task-1'"
            ).fetchone()[0]
        self.assertEqual(binding_status, "attention")



if __name__ == "__main__":
    unittest.main()
