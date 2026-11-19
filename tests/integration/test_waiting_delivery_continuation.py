"""A reply to a delivery question continues the original implementation."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import time
from unittest.mock import patch

from tests.support.repository_cycle import RepositoryCycleFixture, _verification_evidence
from robert_agent.delivery.application import submission
from robert_agent.execution.application import dispatch
from robert_agent.runtime import cycle
from robert_agent.configuration import command as configuration
from robert_agent.work.application import completion
from robert_agent.foundation import values
from robert_agent.work.application import commands
from robert_agent.work import operator_queries
from robert_agent.adapters.github import client


class WaitingDeliveryContinuationTests(RepositoryCycleFixture):
    def prepare_question(self):
        prepared = cycle.run_once(
            self.config_path, fixture_path=self.fixture_path, dry_run=True, skip_external=True, skip_publish=True
        )
        self.assertTrue(prepared["ok"], prepared)
        self.db = prepared["db_path"]
        with closing(sqlite3.connect(self.db)) as conn:
            self.parent, self.previous_attempt, path, self.branch = conn.execute(
                "SELECT task_id,attempt_id,worktree_path,branch_name FROM attempts"
            ).fetchone()
        self.tree = Path(path)
        subprocess.run(
            ["git", "worktree", "add", str(self.tree), "-b", self.branch, "HEAD"],
            cwd=self.repo_root,
            check=True,
            capture_output=True,
        )
        (self.tree / "pending.txt").write_text("preserved implementation\n")
        recorded = submission.record_result(
            self.db,
            submission.build_result(
                self.parent,
                self.previous_attempt,
                "waiting_for_user",
                ["comment:comment-1"],
                [_verification_evidence("git diff --check")],
                "Implementation is ready; no commit or push yet.",
                planned_github_actions=[{"type": "comment", "body": "Allow native verification?"}],
                operator_question={"kind": "completion_acceptance", "summary": "Allow native verification?"},
            ),
            notifier=lambda _: None,
        )
        self.assertTrue(recorded["ok"], recorded)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE attempts SET status='completed',finished_at=datetime('now')")
        audited = cycle.run_once(self.config_path, dry_run=True, skip_external=True, skip_publish=True)
        self.assertTrue(audited["ok"], audited)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE github_actions SET publish_status='published'")
        config = configuration.validate_config(self.config_path, skip_external=True)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            completion.finalize_published_results(
                conn,
                "repo:example/backend",
                self.data_dir,
                self.db,
                config["repos"][0],
                "test-finalize",
                values.utc_now(),
                True,
                [],
                None,
                config_result=config,
            )
            self.assertEqual(conn.execute("SELECT lifecycle FROM tasks").fetchone()[0], "waiting_for_user")

    def reply(self, *, actor="wklken"):
        reply_path = self.root / "reply.json"
        reply_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "acceptance",
                            "number": 123,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": actor,
                            "body": "@robert-bot 允许",
                            "intent": "discussion",
                            "state": "open",
                        }
                    ]
                }
            )
        )
        result = cycle.run_once(
            self.config_path, fixture_path=reply_path, dry_run=True, skip_external=True, skip_publish=True
        )
        self.assertTrue(result["ok"], result)
        return result

    def test_trusted_acceptance_keeps_delivery_route_context_and_owned_edits(self):
        self.prepare_question()
        self.reply()
        with closing(sqlite3.connect(self.db)) as conn:
            child, route, output, lifecycle, child_attempt, path, branch = conn.execute(
                "SELECT t.task_id,t.route_id,t.expected_output,t.lifecycle,a.attempt_id,a.worktree_path,a.branch_name "
                "FROM tasks t JOIN attempts a ON a.task_id=t.task_id WHERE t.parent_task_id=?",
                (self.parent,),
            ).fetchone()
            self.assertEqual((route, output, lifecycle), ("new-pr", "new_pr", "queued"))
            self.assertEqual((path, branch), (str(self.tree), self.branch))
            self.assertIsNone(conn.execute("SELECT completed_at FROM work_items").fetchone()[0])
            self.assertEqual(
                conn.execute("SELECT lifecycle FROM tasks WHERE task_id=?", (self.parent,)).fetchone()[0], "completed"
            )
        prompt = (self.data_dir / "artifacts" / child / "prompt.md").read_text()
        archive = json.loads(next((self.data_dir / "artifacts" / child).glob("*.github-context.json")).read_text())
        self.assertIn("comment:comment-1", json.dumps(archive))
        self.assertIn("comment:acceptance", json.dumps(archive))
        self.assertIn("Implementation is ready", prompt)
        worktree = dispatch.ensure_dispatch_worktree(
            self.db,
            {"repo_root": str(self.repo_root), "worktree_root": str(self.worktree_root)},
            {"task_id": child, "attempt_id": child_attempt},
            {"ok": True, "worktree_path": path, "branch_name": branch},
        )
        self.assertTrue(worktree["ok"], worktree)
        self.assertEqual((self.tree / "pending.txt").read_text(), "preserved implementation\n")
        self.reply()
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 2)

    def test_untrusted_reply_does_not_resume_delivery(self):
        self.prepare_question()
        self.reply(actor="outsider")
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT lifecycle FROM tasks").fetchone()[0], "waiting_for_user")

    def test_continuation_preserves_the_selected_base_for_future_replies(self):
        self.prepare_question()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE tasks SET metadata_json=json_set(metadata_json, '$.target_base_branch', 'release/1.0') WHERE task_id=?",
                (self.parent,),
            )
        self.reply()
        with closing(sqlite3.connect(self.db)) as conn:
            child, metadata = conn.execute(
                "SELECT task_id,metadata_json FROM tasks WHERE parent_task_id=?", (self.parent,)
            ).fetchone()
        self.assertEqual(json.loads(metadata)["target_base_branch"], "release/1.0")
        self.assertIn("release/1.0", (self.data_dir / "artifacts" / child / "prompt.md").read_text())

    def test_changed_branch_blocks_owned_workspace_reuse(self):
        self.prepare_question()
        subprocess.run(["git", "checkout", "-b", "other-work"], cwd=self.tree, check=True, capture_output=True)
        self.reply()
        with closing(sqlite3.connect(self.db)) as conn:
            lifecycle = conn.execute(
                "SELECT t.lifecycle FROM tasks t JOIN attempts a ON a.task_id=t.task_id WHERE t.parent_task_id=?",
                (self.parent,),
            ).fetchone()[0]
            self.assertEqual(lifecycle, "failed")
            self.assertEqual(conn.execute("SELECT count(*) FROM attempts WHERE status='prepared'").fetchone()[0], 0)
        self.assertEqual((self.tree / "pending.txt").read_text(), "preserved implementation\n")

    def test_dispatch_rejects_continuation_with_unrelated_previous_attempt(self):
        self.prepare_question()
        self.reply()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            child, attempt = conn.execute(
                "SELECT t.task_id,a.attempt_id FROM tasks t JOIN attempts a ON a.task_id=t.task_id "
                "WHERE t.parent_task_id=?",
                (self.parent,),
            ).fetchone()
            conn.execute(
                "UPDATE attempts SET metadata_json=json_set(metadata_json,'$.continuation.previous_attempt_id','unrelated') "
                "WHERE attempt_id=?",
                (attempt,),
            )
        (self.tree / "pending.txt").unlink()
        worktree = dispatch.ensure_dispatch_worktree(
            self.db,
            {"repo_root": str(self.repo_root), "worktree_root": str(self.worktree_root)},
            {"task_id": child, "attempt_id": attempt},
            {"ok": True, "worktree_path": str(self.tree), "branch_name": self.branch},
        )
        self.assertFalse(worktree["ok"], worktree)
        self.assertEqual(worktree["status"], "blocked_changed_worktree")

    def test_dispatch_does_not_replace_a_missing_continuation_worktree(self):
        self.prepare_question()
        self.reply()
        with closing(sqlite3.connect(self.db)) as conn:
            child, attempt = conn.execute(
                "SELECT t.task_id,a.attempt_id FROM tasks t JOIN attempts a ON a.task_id=t.task_id "
                "WHERE t.parent_task_id=?",
                (self.parent,),
            ).fetchone()
        self.tree.rename(self.tree.with_name("moved-checkout"))
        with patch.object(dispatch.assigned_workspace, "prepare_worktree", return_value={"ok": True}) as prepare:
            worktree = dispatch.ensure_dispatch_worktree(
                self.db,
                {"repo_root": str(self.repo_root), "worktree_root": str(self.worktree_root)},
                {"task_id": child, "attempt_id": attempt},
                {"worktree_path": str(self.tree), "branch_name": self.branch},
            )
        self.assertEqual(worktree["status"], "blocked_unavailable_worktree")
        self.assertFalse(worktree["ok"])
        prepare.assert_not_called()

    def test_board_reply_continues_delivery_after_fresh_source_read(self):
        self.prepare_question()
        with closing(sqlite3.connect(self.db)) as conn:
            conn.row_factory = sqlite3.Row
            item = dict(conn.execute("SELECT * FROM work_items").fetchone())
        context = commands.CommandContext(
            "operator", "wklken", frozenset({"repo:example/backend"}), frozenset({"default"})
        )
        reply = commands.execute_command(
            self.db,
            item["work_item_id"],
            context=context,
            command="reply",
            expected_version=item["version"],
            idempotency_key="native-approval",
            body="允许，继续提交 PR",
            notifier=lambda _: None,
        )
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "INSERT OR REPLACE INTO github_scan_reads VALUES (?,?,?,?)",
                (
                    "repo:example/backend",
                    client.command_key(["gh", "api", "repos/example/backend/issues/123"]),
                    json.dumps({"state": "open", "title": "Original feature", "body": "Implement original feature"}),
                    time.time(),
                ),
            )
            self.assertTrue(operator_queries.source_read(conn, operator_queries.pending_tasks(conn)[0])[0])
        result = cycle.run_once(self.config_path, dry_run=True, skip_external=True, skip_publish=True)
        self.assertTrue(result["ok"], result)
        with closing(sqlite3.connect(self.db)) as conn:
            route, path, branch = conn.execute(
                "SELECT t.route_id,a.worktree_path,a.branch_name FROM tasks t JOIN attempts a ON a.task_id=t.task_id "
                "WHERE t.task_id=?",
                (reply["task_id"],),
            ).fetchone()
            self.assertEqual(route, "new-pr")
            self.assertEqual((path, branch), (str(self.tree), self.branch))
            self.assertEqual(
                conn.execute("SELECT status FROM attempts WHERE task_id=?", (reply["task_id"],)).fetchone()[0],
                "prepared",
            )
            attempt = conn.execute("SELECT attempt_id FROM attempts WHERE task_id=?", (reply["task_id"],)).fetchone()[0]
        worktree = dispatch.ensure_dispatch_worktree(
            self.db,
            {"repo_root": str(self.repo_root), "worktree_root": str(self.worktree_root)},
            {"task_id": reply["task_id"], "attempt_id": attempt},
            {"ok": True, "worktree_path": path, "branch_name": branch},
        )
        self.assertTrue(worktree["ok"], worktree)

    def test_reopen_after_analysis_acknowledgement_reuses_original_delivery(self):
        self.prepare_question()
        self.reply()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.row_factory = sqlite3.Row
            analysis = dict(conn.execute("SELECT * FROM tasks WHERE parent_task_id=?", (self.parent,)).fetchone())
            conn.execute(
                "UPDATE tasks SET route_id='comment-analysis',expected_output='comment_analysis',lifecycle='completed' WHERE task_id=?",
                (analysis["task_id"],),
            )
            conn.execute("UPDATE attempts SET status='completed' WHERE task_id=?", (analysis["task_id"],))
            conn.execute("UPDATE workstreams SET lifecycle='completed',active_task_id=NULL")
            conn.execute("UPDATE work_items SET completed_at=?", (values.utc_now(),))
            from robert_agent.persistence import execution_control

            execution_control.close_binding(conn, "task", analysis["task_id"], values.utc_now())
            item = dict(conn.execute("SELECT * FROM work_items").fetchone())
        context = commands.CommandContext(
            "operator", "wklken", frozenset({"repo:example/backend"}), frozenset({"default"})
        )
        reopened = commands.execute_command(
            self.db,
            item["work_item_id"],
            context=context,
            command="reopen",
            expected_version=item["version"],
            idempotency_key="resume-undelivered",
            body="继续原实现并提交 PR",
            notifier=lambda _: None,
        )
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "INSERT OR REPLACE INTO github_scan_reads VALUES (?,?,?,?)",
                (
                    "repo:example/backend",
                    client.command_key(["gh", "api", "repos/example/backend/issues/123"]),
                    json.dumps({"state": "open", "title": "Original feature", "body": "Implement original feature"}),
                    time.time(),
                ),
            )
        prepared = cycle.run_once(self.config_path, dry_run=True, skip_external=True, skip_publish=True)
        self.assertTrue(prepared["ok"], prepared)
        with closing(sqlite3.connect(self.db)) as conn:
            route, attempt, path, branch = conn.execute(
                "SELECT t.route_id,a.attempt_id,a.worktree_path,a.branch_name FROM tasks t JOIN attempts a ON a.task_id=t.task_id WHERE t.task_id=?",
                (reopened["task_id"],),
            ).fetchone()
        self.assertEqual(route, "new-pr")
        self.assertEqual((path, branch), (str(self.tree), self.branch))
        worktree = dispatch.ensure_dispatch_worktree(
            self.db,
            {"repo_root": str(self.repo_root), "worktree_root": str(self.worktree_root)},
            {"task_id": reopened["task_id"], "attempt_id": attempt},
            {"ok": True, "worktree_path": path, "branch_name": branch},
        )
        self.assertTrue(worktree["ok"], worktree)
