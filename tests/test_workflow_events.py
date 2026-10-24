import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from robert_agent import discover, run_once, storage, workflow_events, execution_operations, execution_store


NOW = "2026-08-02T00:00:00+00:00"


def _fact(**overrides):
    fact = {
        "fact_id": "event:comment-1",
        "subject_type": "task",
        "subject_id": "task-1",
        "repo": "example/repo",
        "source_type": "pull_request",
        "source_key": "github:example/repo!7",
        "number": 7,
        "event_type": "comment",
        "authorization_status": "accepted_context",
        "actor_login": "reviewer",
        "body": "please address this",
    }
    fact.update(overrides)
    return fact


class WorkflowEventClassifierTests(unittest.TestCase):
    def test_discovery_normalization_exposes_stable_event_identity_and_evidence_fields(self):
        normalized = discover.normalize_events(
            [
                {
                    "id": "snapshot-1",
                    "event_type": "snapshot",
                    "source_type": "pull_request",
                    "number": 7,
                    "old_head_sha": "a" * 40,
                    "new_head_sha": "b" * 40,
                    "source_branch_exists": True,
                }
            ],
            {"full_name": "example/repo", "github_account": "bot"},
        )[0]
        self.assertEqual(normalized["event_fingerprint"], "snapshot:snapshot-1")
        self.assertEqual(normalized["source_key"], "github:example/repo!7")
        self.assertEqual(normalized["old_head_sha"], "a" * 40)
        self.assertTrue(normalized["source_branch_exists"])

    def test_table_driven_classification(self):
        cases = [
            (_fact(), "additive"),
            (_fact(event_type="review_comment"), "additive"),
            (_fact(event_type="review", body="feedback"), "additive"),
            (_fact(event_type="review_request", authorization_status="authorized_trigger"), "additive"),
            (_fact(source_state="closed", event_type="source_state", authorization_status="accepted_context"), "terminal"),
            (_fact(source_state="closed", merged=True, event_type="source_state"), "terminal"),
            (_fact(old_head_sha="a" * 40, new_head_sha="b" * 40, event_type="snapshot"), "invalidating"),
            (_fact(old_base_sha="a" * 40, new_base_sha="b" * 40, event_type="snapshot"), "invalidating"),
            (_fact(old_base_ref="main", new_base_ref="release", event_type="snapshot"), "invalidating"),
            (_fact(source_branch_exists=False, event_type="snapshot"), "invalidating"),
            (_fact(old_permission="write", new_permission="denied", event_type="permission"), "invalidating"),
            (_fact(event_type="notification"), "irrelevant"),
            (_fact(event_type="duplicate", no_change=True), "irrelevant"),
            (_fact(event_type="unrelated", authorization_status="ignored_context"), "irrelevant"),
        ]
        for fact, expected in cases:
            with self.subTest(fact=fact):
                self.assertEqual(workflow_events.classify_subject_fact(fact), expected)

    def test_claimed_invalidation_requires_exact_evidence(self):
        with self.assertRaisesRegex(ValueError, "old_head_sha"):
            workflow_events.classify_subject_fact(
                _fact(event_type="snapshot", invalidation_reason="head_changed")
            )

    def test_fact_identity_and_bounded_evidence_are_required(self):
        with self.assertRaisesRegex(ValueError, "fact_id"):
            workflow_events.classify_subject_fact(_fact(fact_id=""))
        with self.assertRaisesRegex(ValueError, "source_key"):
            workflow_events.classify_subject_fact(_fact(source_key=""))
        with self.assertRaisesRegex(ValueError, "bounded"):
            workflow_events.normalize_fact_evidence(
                _fact(event_type="snapshot", old_head_sha="a" * 40, new_head_sha="b" * 40, reason_code="x" * 10000)
            )
        self.assertEqual(
            workflow_events.classify_subject_fact(_fact(body="x" * 10000)),
            "additive",
        )

    def test_duplicate_fact_is_irrelevant(self):
        self.assertEqual(
            workflow_events.classify_subject_fact(
                _fact(event_type="comment", duplicate_of="event:comment-0")
            ),
            "irrelevant",
        )


class WorkflowEventApplicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "robert.sqlite3"
        storage.init_database(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root) VALUES ('repo-1', 'example/repo', 'bot', 'main', '/repo', '/worktrees')"
            )
            conn.execute(
                "INSERT INTO workstreams(workstream_id, repo_id, lifecycle, active_task_id, created_at, updated_at) VALUES ('ws-1', 'repo-1', 'active', 'task-1', ?, ?)",
                (NOW, NOW),
            )
            conn.execute(
                "INSERT INTO tasks(task_id, workstream_id, task_kind, lifecycle, created_at, updated_at) VALUES ('task-1', 'ws-1', 'human_request', 'running', ?, ?)",
                (NOW, NOW),
            )

    def _bind(self, owner):
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            return execution_store.ensure_subject(conn, 'task', 'task-1', 'human_request', NOW)

    def test_dbos_invalidation_persists_attention_and_one_signal_atomically(self):
        self._bind("dbos")
        fact = _fact(old_head_sha="a" * 40, new_head_sha="b" * 40, event_type="snapshot")
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            result = execution_operations.apply_classified_fact(
                conn, "task", "task-1", fact, now=NOW
            )
            self.assertEqual(result["classification"], "invalidating")
        with closing(sqlite3.connect(self.db_path)) as conn:
            binding = execution_store.get_binding(conn, "task", "task-1")
            self.assertEqual(binding["binding_status"], "attention")
            metadata = json.loads(binding["metadata_json"])
            self.assertEqual(metadata["latest_fact"]["fact_id"], fact["fact_id"])
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM execution_fact_receipts").fetchone()[0],
                1,
            )

    def test_fact_subject_must_match_target_binding(self):
        self._bind("dbos")
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            with self.assertRaisesRegex(ValueError, "subject"):
                execution_operations.apply_classified_fact(
                    conn,
                    "task",
                    "task-1",
                    _fact(subject_id="task-other"),
                    now=NOW,
                )

    def test_old_dbos_fact_replay_does_not_replace_newer_evidence(self):
        self._bind("dbos")
        old_fact = _fact(fact_id="event:old")
        new_fact = _fact(fact_id="event:new", event_type="review_comment")
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            execution_operations.apply_classified_fact(
                conn, "task", "task-1", old_fact, now=NOW
            )
            execution_operations.apply_classified_fact(
                conn, "task", "task-1", new_fact, now=NOW
            )
            replay = execution_operations.apply_classified_fact(
                conn, "task", "task-1", old_fact, now=NOW
            )
            self.assertTrue(replay["duplicate"])
        with closing(sqlite3.connect(self.db_path)) as conn:
            binding = execution_store.get_binding(conn, "task", "task-1")
            self.assertEqual(json.loads(binding["metadata_json"])["latest_fact"]["fact_id"], "event:new")

    def test_invalidation_termination_uses_exact_attempt_operation_after_commit(self):
        self._bind("legacy")
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO attempts(attempt_id, task_id, attempt_no, status) VALUES ('attempt-1', 'task-1', 1, 'running')"
            )
            result = run_once._apply_workflow_event_fact(
                conn,
                _fact(
                    task_id="task-1",
                    old_head_sha="a" * 40,
                    new_head_sha="b" * 40,
                    event_type="snapshot",
                ),
                NOW,
            )
            conn.commit()
        self.assertEqual(result["attempt_id"], "attempt-1")
        with mock.patch.object(
            run_once.execution_operations,
            "terminate_worker",
            return_value="terminated",
        ) as terminate:
            outcomes = run_once._terminate_invalidated_attempts(
                self.db_path, ["attempt-1", "attempt-1"]
            )
        self.assertEqual(outcomes, [("attempt-1", "terminated")])
        terminate.assert_called_once_with(self.db_path, "attempt-1")

    def test_terminal_projection_records_reason(self):
        fact = _fact(source_state="open", merged=True, event_type="source_state")
        evidence = workflow_events.normalize_fact_evidence(fact, "terminal")
        self.assertEqual(evidence["reason_code"], "source_merged")

    def test_remote_terminal_reconciliation_uses_shared_classifier_for_dbos(self):
        self._bind("dbos")
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO github_sources(source_id, repo_id, source_key, source_type, number, state) VALUES ('source-1', 'repo-1', 'github:example/repo!7', 'pull_request', 7, 'open')"
            )
            result = run_once._record_remote_terminal_state(
                conn,
                "repo-1",
                "ws-1",
                "source-1",
                "github:example/repo!7",
                "pull_request",
                7,
                "task-1",
                {
                    "state": "closed",
                    "merged": True,
                    "merged_at": NOW,
                    "updated_at": NOW,
                    "title": "",
                    "html_url": "https://github.com/example/repo/pull/7",
                },
                "completed",
                "remote_pr_merged",
                NOW,
            )
            self.assertTrue(result)
        with closing(sqlite3.connect(self.db_path)) as conn:
            binding = execution_store.get_binding(conn, "task", "task-1")
            self.assertEqual(binding["binding_status"], "terminal")
            self.assertEqual(conn.execute("SELECT lifecycle FROM tasks WHERE task_id = 'task-1'").fetchone()[0], "canceled")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM execution_fact_receipts").fetchone()[0], 1)

    def test_remote_terminal_reconciliation_targets_dbos_episode_not_child_task(self):
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO github_sources(source_id, repo_id, source_key, source_type, number, state) VALUES ('source-1', 'repo-1', 'github:example/repo!7', 'pull_request', 7, 'open')"
            )
            conn.execute(
                "INSERT INTO pr_remediation_episodes(episode_id, workstream_id, episode_kind, subject_key, observed_head_sha, observed_base_sha, status, first_seen_at, updated_at, last_task_id) VALUES ('episode-1', 'ws-1', 'ci', 'example/repo!7', ?, ?, 'remediating', ?, ?, 'task-2')",
                ("a" * 40, "b" * 40, NOW, NOW),
            )
            conn.execute(
                "INSERT INTO tasks(task_id, workstream_id, task_kind, lifecycle, created_at, updated_at, metadata_json) VALUES ('task-2', 'ws-1', 'ci_remediation', 'waiting_for_user', ?, ?, ?)",
                (NOW, NOW, json.dumps({"remediation_episode_id": "episode-1"})),
            )
            binding = execution_store.ensure_subject(conn, 'remediation_episode', 'episode-1', 'pr_remediation', NOW)
            result = run_once._record_remote_terminal_state(
                conn,
                "repo-1",
                "ws-1",
                "source-1",
                "github:example/repo!7",
                "pull_request",
                7,
                "task-2",
                {"state": "closed", "merged": True, "merged_at": NOW},
                "completed",
                "remote_pr_merged",
                NOW,
            )
            self.assertTrue(result)
            self.assertIsNone(execution_store.get_binding(conn, "task", "task-2"))
            self.assertEqual(
                execution_store.get_binding(conn, "remediation_episode", "episode-1")["binding_status"],
                "terminal",
            )
            self.assertEqual(binding["subject_id"], "episode-1")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM execution_fact_receipts").fetchone()[0], 1)

    def test_duplicate_fact_is_idempotent(self):
        self._bind("dbos")
        fact = _fact()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            first = execution_operations.apply_classified_fact(
                conn, "task", "task-1", fact, now=NOW
            )
            second = execution_operations.apply_classified_fact(
                conn, "task", "task-1", fact, now=NOW
            )
        self.assertEqual(first["fact_id"], second["fact_id"])
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM execution_fact_receipts").fetchone()[0], 1)

    def test_legacy_fact_persists_evidence_without_outbox(self):
        self._bind("legacy")
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            execution_operations.apply_classified_fact(
                conn, "task", "task-1", _fact(), now=NOW
            )
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM workflow_outbox").fetchone()[0], 0)
            binding = execution_store.get_binding(conn, "task", "task-1")
            self.assertEqual(json.loads(binding["metadata_json"])["latest_fact"]["classification"], "additive")


if __name__ == "__main__":
    unittest.main()
