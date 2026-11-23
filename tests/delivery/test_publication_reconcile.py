from unittest import mock
import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path

from robert_agent.delivery import values as publication_values
from robert_agent.delivery.application import reconcile as publication_reconcile
from robert_agent.delivery.application import publish
from robert_agent.execution import retry as retry_policy
from robert_agent.persistence import execution_control as execution_store
from tests.support.publication_reconcile import Completed, PublicationReconcileFixture


class PublicationReconcileTests(PublicationReconcileFixture):
    def publication_state(self, action_id="action-1"):
        with closing(sqlite3.connect(self.db_path)) as conn:
            metadata = json.loads(
                conn.execute("SELECT metadata_json FROM github_actions WHERE action_id=?", (action_id,)).fetchone()[0]
            )
            intent = conn.execute("SELECT status FROM publication_intents WHERE action_id=?", (action_id,)).fetchone()
            attention = conn.execute(
                "SELECT COUNT(*) FROM notifications WHERE notification_type='workflow_attention_required'"
            ).fetchone()[0]
        return metadata.get("publish") or {}, intent[0] if intent else None, attention

    def publish_at(self, now, runner):
        with mock.patch.object(publication_values, "now", return_value=now):
            return publish.publish_ready_actions(self.db_path, run_command=runner)

    def test_only_failed_remote_reads_are_rechecked(self):
        for reason_code, expected in (
            ("comment_query_failed", True),
            ("open_pr_query_failed", True),
            ("remote_branch_query_failed", True),
            ("existing_pr_snapshot_query_failed", True),
            ("review_query_failed", True),
            ("vote_query_failed", True),
            ("push_target_query_failed", True),
            ("comment_marker_missing", False),
            ("open_pr_candidate_conflict", False),
            ("remote_sha_conflict", False),
            ("comment_response_malformed", False),
        ):
            with self.subTest(reason_code=reason_code):
                reconciliation = {"status": "unknown", "reason_code": reason_code, "evidence": {}}
                self.assertIs(publication_reconcile.read_failed(reconciliation), expected)

    def test_failed_reconcile_read_rechecks_after_backoff_without_another_write(self):
        comment = {
            "id": 7,
            "user": {"login": "bot"},
            "body": self.body,
            "html_url": "https://github.com/x/y/issues/1#issuecomment-7",
        }
        remote = {"lookup": "[]"}
        writes = []

        def runner(command, **kwargs):
            if command[2].endswith("comments?per_page=100"):
                if remote["lookup"] is None:
                    return Completed("", 1, "connection reset")
                return Completed(remote["lookup"])
            writes.append(command)
            return Completed("", 1, "connection reset after response")

        publish.publish_ready_actions(self.db_path, run_command=runner)
        remote["lookup"] = None
        due = self.publication_state()[0]["next_retry_at"]
        rechecked = self.publish_at(due, runner)
        early = self.publish_at(due, runner)

        publish_state, intent, attention = self.publication_state()
        self.assertFalse(rechecked["ok"])
        self.assertIn("comment_query_failed", rechecked["failures"][0]["safe_error"])
        self.assertEqual(early["pending_count"], 0)
        self.assertEqual((intent, attention), ("executing", 0))
        self.assertTrue(publish_state["reconcile_required"])
        self.assertGreater(retry_policy.timestamp(publish_state["next_retry_at"]), retry_policy.timestamp(due))

        remote["lookup"] = json.dumps([comment])
        confirmed = self.publish_at(publish_state["next_retry_at"], runner)
        self.assertEqual((confirmed["published_count"], confirmed["deduplicated_count"]), (1, 1), confirmed)
        self.assertEqual(self.publication_state()[1], "published")
        self.assertEqual(len(writes), 1)

    def test_reconcile_recheck_keeps_later_actions_ordered_until_absence_is_confirmed(self):
        later_body = "<!-- robert-comment task_id=task-1 attempt_id=attempt-1 action_index=1 -->\nnext"
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO github_actions(action_id, result_id, task_id, action_type, target_url, audit_status, "
                "publish_status, created_at, metadata_json) VALUES ('action-2', 'result-1', 'task-1', 'comment', "
                "'https://github.com/x/y/issues/1', 'accepted', 'not_published', '2026-08-02T00:00:01+00:00', ?)",
                (json.dumps({"body": later_body}),),
            )
        remote = []
        written = []
        state = {"lookup_fails": False, "crash": True}

        def runner(command, **kwargs):
            if command[2].endswith("comments?per_page=100"):
                if state["lookup_fails"]:
                    return Completed("", 1, "connection reset")
                return Completed(json.dumps(remote))
            body = command[-1].removeprefix("body=")
            written.append(body)
            if state["crash"]:
                raise RuntimeError("crash before the comment was created")
            comment = {"id": len(remote) + 1, "user": {"login": "bot"}, "body": body}
            remote.append(comment)
            return Completed(json.dumps(comment))

        with self.assertRaisesRegex(RuntimeError, "crash before"):
            publish.publish_ready_actions(self.db_path, run_command=runner)
        state.update(lookup_fails=True, crash=False)
        now = publication_values.now()
        rechecked = self.publish_at(now, runner)
        waiting = self.publish_at(now, runner)

        self.assertFalse(rechecked["ok"])
        self.assertEqual(waiting["pending_count"], 0)
        self.assertEqual(written, [self.body])
        self.assertEqual(self.publication_state()[1:], ("executing", 0))

        state["lookup_fails"] = False
        due = self.publication_state()[0]["next_retry_at"]
        self.publish_at(due, runner)
        self.publish_at(due, runner)

        self.assertEqual(written, [self.body, self.body, later_body])
        self.assertEqual([comment["body"] for comment in remote], [self.body, later_body])
        self.assertEqual(self.publication_state("action-1")[1], "published")
        self.assertEqual(self.publication_state("action-2")[1], "published")

    def test_unreadable_recheck_state_enters_attention(self):
        def runner(command, **kwargs):
            if command[2].endswith("comments?per_page=100"):
                return Completed("[]")
            return Completed("", 1, "connection reset after response")

        publish.publish_ready_actions(self.db_path, run_command=runner)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE github_actions SET metadata_json=json_set(metadata_json, '$.publish.reconcile_retry', 'corrupt')"
            )
        result = self.publish_at(
            self.publication_state()[0]["next_retry_at"],
            lambda *_args, **_kwargs: Completed("", 1, "connection reset"),
        )

        self.assertIn("comment_query_failed", result["failures"][0]["safe_error"])
        self.assertEqual(self.publication_state()[1:], ("attention", 1))

    def test_conflicting_reconcile_evidence_enters_attention_without_recheck(self):
        duplicate = [{"id": number, "body": self.body, "user": {"login": "bot"}} for number in (1, 2)]
        remote = {"lookup": "[]"}
        writes = []

        def runner(command, **kwargs):
            if command[2].endswith("comments?per_page=100"):
                return Completed(remote["lookup"])
            writes.append(command)
            return Completed("", 1, "connection reset after response")

        publish.publish_ready_actions(self.db_path, run_command=runner)
        remote["lookup"] = json.dumps(duplicate)
        result = self.publish_at(self.publication_state()[0]["next_retry_at"], runner)

        self.assertIn("comment_marker_conflict", result["failures"][0]["safe_error"])
        self.assertEqual(self.publication_state()[1:], ("attention", 1))
        self.assertEqual(len(writes), 1)

    def test_marker_requires_matching_publisher_and_exact_body(self):
        action = {
            "action_type": "comment",
            "target_url": "https://github.com/x/y/issues/1",
            "metadata_json": json.dumps({"body": self.body}),
        }
        scope = {"repo": "x/y", "github_account": "bot"}
        for login, body, expected in (
            ("outsider", self.body, "confirmed_absent"),
            ("bot", self.body + "changed", "unknown"),
            ("BOT", self.body, "confirmed_effect"),
        ):
            with self.subTest(login=login, body=body):

                def runner(*args, **kwargs):
                    return Completed(json.dumps([{"id": 7, "body": body, "user": {"login": login}}]))

                result = publication_reconcile.classify_remote_state(action, scope, runner)
                self.assertEqual(result["status"], expected)

    def test_last_ambiguous_write_reconciles_before_terminal_attention(self):
        from robert_agent.execution import retry as retry_policy

        now = "2026-10-29T00:00:00+00:00"
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            metadata = json.loads(
                conn.execute("SELECT metadata_json FROM github_actions WHERE action_id='action-1'").fetchone()[0]
            )
            metadata["publish"] = {"attempt_count": retry_policy.MAX_ATTEMPTS - 1, "first_failed_at": now}
            conn.execute(
                "UPDATE github_actions SET metadata_json=? WHERE action_id='action-1'", (json.dumps(metadata),)
            )
        writes = []

        def ambiguous(command, **kwargs):
            if command[2].endswith("comments?per_page=100"):
                return Completed("[]")
            writes.append(command)
            return Completed("", 1, "connection reset after response")

        with mock.patch.object(publication_values, "now", return_value=now):
            first = publish.publish_ready_actions(self.db_path, run_command=ambiguous)
            self.assertFalse(first["ok"])
            with closing(sqlite3.connect(self.db_path)) as conn:
                self.assertEqual(conn.execute("SELECT status FROM publication_intents").fetchone()[0], "executing")

            def reconcile(command, **kwargs):
                self.assertNotIn("-f", command)
                return Completed(
                    json.dumps(
                        [
                            {
                                "id": 7,
                                "user": {"login": "bot"},
                                "body": self.body,
                                "html_url": "https://github.com/x/y/issues/1#issuecomment-7",
                            }
                        ]
                    )
                )

            second = publish.publish_ready_actions(self.db_path, run_command=reconcile)
        self.assertEqual(second["published_count"], 1, second)
        self.assertEqual(second["deduplicated_count"], 1)
        self.assertEqual(len(writes), 1)

    def test_exhausted_crashed_write_checks_absence_without_another_write(self):
        from robert_agent.execution import retry as retry_policy

        def crash(command, **kwargs):
            if command[2].endswith("comments?per_page=100"):
                return Completed("[]")
            raise RuntimeError("crash during remote write")

        with self.assertRaises(RuntimeError):
            publish.publish_ready_actions(self.db_path, run_command=crash)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            metadata = json.loads(conn.execute("SELECT metadata_json FROM github_actions").fetchone()[0])
            metadata["publish"]["attempt_count"] = retry_policy.MAX_ATTEMPTS
            conn.execute("UPDATE github_actions SET metadata_json=?", (json.dumps(metadata),))
        reads = []

        def absent(command, **kwargs):
            self.assertNotIn("-f", command)
            reads.append(command)
            return Completed("[]")

        result = publish.publish_ready_actions(self.db_path, run_command=absent)
        self.assertFalse(result["ok"])
        self.assertTrue(reads)
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT status FROM publication_intents").fetchone()[0], "attention")
        self.assertEqual(publish.publish_ready_actions(self.db_path, run_command=absent)["pending_count"], 0)

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
                            "user": {"login": "bot"},
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
            status = conn.execute("SELECT publish_status FROM github_actions WHERE action_id = 'action-1'").fetchone()[
                0
            ]
            intent = conn.execute("SELECT status FROM publication_intents WHERE action_id = 'action-1'").fetchone()[0]
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
            {"repo": "x/y", "github_account": "bot", "sources": [{"source_type": "issue", "number": 1}]},
            lambda *_args, **_kwargs: Completed(
                json.dumps(
                    [
                        {"id": 1, "body": self.body, "user": {"login": "bot"}},
                        {"id": 2, "body": self.body, "user": {"login": "bot"}},
                    ]
                )
            ),
        )
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason_code"], "comment_marker_conflict")

    def test_comment_listing_with_an_unexpected_shape_needs_operator_evidence(self):
        action = {
            "action_id": "action-1",
            "action_type": "comment",
            "target_url": "https://github.com/x/y/issues/1",
            "metadata_json": json.dumps({"body": self.body}),
        }
        scope = {"repo": "x/y", "github_account": "bot", "sources": [{"source_type": "issue", "number": 1}]}
        for stdout in (
            "",
            '{"message": "Not Found"}',
            '[[{"id": 1}]]',
            '[["comment"]]',
            json.dumps([{"id": 1, "body": self.body, "user": "bot"}]),
            '[[{"id": 1, "body": "comment", "user": {"login": 42}}]]',
            json.dumps([[{"id": 1, "body": self.body, "user": {"login": " "}}]]),
            json.dumps([[{"body": self.body, "user": {"login": "bot"}}]]),
        ):
            with self.subTest(stdout=stdout):
                result = publication_reconcile.classify_remote_state(
                    action, scope, lambda *_args, response=stdout, **_kwargs: Completed(response)
                )
                self.assertEqual((result["status"], result["reason_code"]), ("unknown", "comment_response_malformed"))
                self.assertFalse(publication_reconcile.read_failed(result))

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

        result = publication_reconcile.classify_remote_state(action, {"repo": "x/y", "sources": []}, runner)
        self.assertEqual(result["status"], "confirmed_effect")
        self.assertEqual(result["reason_code"], "open_pr_marker_match")
        self.assertEqual(len(commands), 2)

    def test_new_branch_push_absence_and_existing_push_conflict(self):
        action = {
            "action_id": "action-push",
            "action_type": "push_existing_pr",
            "target_url": "",
            "metadata_json": json.dumps({"worktree_path": "/worktree", "branch": "feature"}),
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
            {
                **scope,
                "existing_pr_head_sha": "b" * 40,
                "pr_number": 9,
                "observed_head_sha": "b" * 40,
                "observed_base_sha": "c" * 40,
            },
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

        result = publication_reconcile.classify_remote_state(action, {"repo": "x/y", "sources": []}, fail)
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
        self.assertEqual(first["status"], "publish_failed")
        # Each failed read waits for its persisted deadline; attention follows the last allowed read.
        for _ in range(retry_policy.MAX_ATTEMPTS):
            publish_state, intent, attention = self.publication_state()
            self.assertEqual((intent, attention), ("executing", 0))
            due = publish_state["next_retry_at"]
            self.assertFalse(self.publish_at(due, runner)["ok"])
        self.assertEqual(len(calls), 1 + retry_policy.MAX_ATTEMPTS)
        self.assertEqual(self.publish_at(due, runner)["pending_count"], 0)
        with closing(sqlite3.connect(self.db_path)) as conn:
            count, status = conn.execute(
                "SELECT COUNT(*), MAX(status) FROM notifications WHERE notification_type = 'workflow_attention_required'"
            ).fetchone()
            intent = conn.execute("SELECT status FROM publication_intents WHERE action_id = 'action-1'").fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(status, "recorded")
        self.assertEqual(intent, "attention")

    def test_open_pr_crash_backfills_without_create_retry(self):
        body = "<!-- robert-workstream task_id=task-1 -->\nPR"
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE worker_results SET metadata_json=json_set(?, '$.result_contract', 'compact-v1') WHERE result_id = 'result-1'",
                (json.dumps({"audit": {"status": "accepted", "action_scope": {"repo": "x/y", "sources": []}}}),),
            )
            conn.execute(
                "UPDATE github_actions SET action_type = 'open_pr', target_url = NULL, metadata_json = ? WHERE action_id = 'action-1'",
                (json.dumps({"repo": "x/y", "head": "feature", "base": "main", "body": body, "title": "Fix"}),),
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
                "UPDATE worker_results SET metadata_json=json_set(?, '$.result_contract', 'compact-v1') WHERE result_id = 'result-1'",
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
                "UPDATE worker_results SET metadata_json=json_set(?, '$.result_contract', 'compact-v1') WHERE result_id = 'result-1'",
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
                "UPDATE worker_results SET metadata_json=json_set(?, '$.result_contract', 'compact-v1') WHERE result_id = 'result-1'",
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
                "UPDATE worker_results SET metadata_json=json_set(?, '$.result_contract', 'compact-v1') WHERE result_id = 'result-1'",
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
                stdout = f"{source_sha}\trefs/heads/feature\n" if state["remote_effect"] else ""
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
            due = json.loads(
                retry_conn.execute("SELECT metadata_json FROM github_actions WHERE action_id='action-1'").fetchone()[0]
            )["publish"]["next_retry_at"]
        retry_clock = mock.patch.object(publication_values, "now", return_value=due)
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
                "UPDATE worker_results SET metadata_json=json_set(?, '$.result_contract', 'compact-v1') WHERE result_id = 'result-1'",
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
                return Completed(
                    json.dumps(
                        {
                            "state": "open",
                            "merged": False,
                            "head": {"sha": original_sha},
                            "base": {"ref": "main", "sha": "d" * 40},
                        }
                    )
                )
            if command == ["gh", "api", "repos/x/y/git/ref/heads/main"]:
                return Completed(json.dumps({"ref": "refs/heads/main", "object": {"sha": "c" * 40}}))
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
            due = json.loads(
                retry_conn.execute("SELECT metadata_json FROM github_actions WHERE action_id='action-1'").fetchone()[0]
            )["publish"]["next_retry_at"]
        retry_clock = mock.patch.object(publication_values, "now", return_value=due)
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
        scope = {
            "repo": "x/y",
            "sources": [],
            "worktree_path": str(worktree),
            "branch_name": "feature",
            "result_head_sha": source_sha,
        }
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE worker_results SET metadata_json=json_set(?, '$.result_contract', 'compact-v1') WHERE result_id = 'result-1'",
                (json.dumps({"audit": {"status": "accepted", "action_scope": scope}}),),
            )
            conn.execute(
                "UPDATE github_actions SET action_type = 'push_existing_pr', metadata_json = ? WHERE action_id = 'action-1'",
                (json.dumps({"worktree_path": str(worktree), "branch": "feature"}),),
            )
            binding = execution_store.ensure_subject(conn, "task", "task-1", "human_request", "now")
            execution_store.close_binding(conn, "task", "task-1", "now")
            execution_store.prepare_publication_intent(conn, "action-1", "k", "f", "now")
            execution_store.transition_publication_intent(
                conn, binding["workflow_id"] or "publication-" + "0" * 24, "executing", "now"
            ) if False else None
            publication_id = conn.execute(
                "SELECT publication_id FROM publication_intents WHERE action_id='action-1'"
            ).fetchone()[0]
            execution_store.transition_publication_intent(conn, publication_id, "executing", "now")
        calls = []
        result = publish.publish_ready_actions(
            self.db_path, run_command=lambda command, **kwargs: calls.append(command) or Completed("")
        )
        self.assertFalse(result["ok"])
        self.assertEqual(calls, [["git", "ls-remote", "--heads", "origin", "feature"]])

    def test_missing_or_malformed_scope_performs_zero_writes(self):
        from robert_agent.persistence import execution_control as execution_store

        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            execution_store.ensure_subject(conn, "task", "task-1", "human_request", "2026-08-02T00:00:00+00:00")
            conn.execute(
                "UPDATE worker_results SET metadata_json=json_set(?, '$.result_contract', 'compact-v1') WHERE result_id = 'result-1'",
                (json.dumps({"audit": {"status": "accepted", "action_scope": []}}),),
            )
        calls = []
        result = publish.publish_ready_actions(
            self.db_path,
            run_command=lambda command, **kwargs: (
                calls.append(command) or (_ for _ in ()).throw(AssertionError(command))
            ),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(calls, [])
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM notifications WHERE notification_type='workflow_attention_required'"
                ).fetchone()[0],
                1,
            )
            binding_status = conn.execute(
                "SELECT binding_status FROM execution_subjects WHERE subject_type = 'task' AND subject_id = 'task-1'"
            ).fetchone()[0]
        self.assertEqual(binding_status, "attention")

    def test_empty_accepted_scope_performs_zero_remote_calls(self):
        from robert_agent.persistence import execution_control as execution_store

        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            execution_store.ensure_subject(conn, "task", "task-1", "human_request", "2026-08-02T00:00:00+00:00")
            conn.execute(
                "UPDATE worker_results SET metadata_json=json_set(?, '$.result_contract', 'compact-v1') WHERE result_id = 'result-1'",
                (json.dumps({"audit": {"status": "accepted", "action_scope": {}}}),),
            )
        calls = []

        result = publish.publish_ready_actions(
            self.db_path,
            run_command=lambda command, **_kwargs: calls.append(command) or Completed("[]"),
        )

        self.assertFalse(result["ok"])
        self.assertEqual(calls, [])
        with closing(sqlite3.connect(self.db_path)) as conn:
            binding_status = conn.execute(
                "SELECT binding_status FROM execution_subjects WHERE subject_type = 'task' AND subject_id = 'task-1'"
            ).fetchone()[0]
        self.assertEqual(binding_status, "attention")


if __name__ == "__main__":
    unittest.main()
