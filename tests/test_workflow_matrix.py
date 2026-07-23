from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from tests.support import PACKAGE_ROOT


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT = PACKAGE_ROOT


def _dd_pr_body(text, task_id="task-1", issue_number="101"):
    return (
        "<!-- robert-workstream\n"
        f"origin_workstream_id: github:example/backend#{issue_number}\n"
        f"source_issue: {issue_number}\n"
        f"task_id: {task_id}\n"
        "created_by: robert\n"
        "-->\n"
        + text
    )


def _open_pr_action(task_id="task-1", issue_number="101", pr_number="909"):
    return {
        "type": "open_pr",
        "repo": "example/backend",
        "head": f"codex/dd-{issue_number}-fix",
        "base": "master",
        "title": f"Fix issue {issue_number}",
        "body": _dd_pr_body("Opened PR", task_id=task_id, issue_number=issue_number),
        "url": f"https://github.com/example/backend/pull/{pr_number}",
    }


def _new_pr_actions(task_id="task-1", issue_number="101", pr_number="909"):
    return [
        {
            "type": "push_existing_pr",
            "worktree_path": f"/tmp/.worktrees/codex__dd-{issue_number}-fix",
            "branch": f"codex/dd-{issue_number}-fix",
        },
        _open_pr_action(task_id=task_id, issue_number=issue_number, pr_number=pr_number),
    ]


def _dd_comment_body(text, task_id="task-1"):
    return (
        "<!-- robert-comment\n"
        f"task_id: {task_id}\n"
        "created_by: robert\n"
        "-->\n"
        + text
    )


def _passed_verification():
    return [
        {
            "command": "python -B -m unittest",
            "status": "passed",
            "purpose": "Verify the mocked issue-to-PR workflow result.",
            "required": True,
            "exit_code": 0,
        }
    ]


class WorkflowMatrixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo_root = self.root / "blueking-apigateway"
        self.repo_root.mkdir()
        (self.repo_root / ".git").mkdir()
        self.worktree_root = self.repo_root / ".worktrees"
        self.worktree_root.mkdir()
        self.data_dir = self.root / "data"
        self.config_path = self.root / "config.yml"
        self.config_path.write_text(
            f"""data_dir: {self.data_dir}
database: dd.sqlite3
max_concurrency: 3
stale_after_minutes: 20
hard_timeout_minutes: 90
default_max_retries: 0
repos:
  - full_name: example/backend
    github_account: robert-bot
    trusted_actors:
      - wklken
    default_base_branch: master
    repo_root: {self.repo_root}
    worktree_root: {self.worktree_root}
""",
            encoding="utf-8",
        )

    @property
    def db_path(self):
        return self.data_dir / "dd.sqlite3"

    def write_fixture(self, name, events):
        path = self.root / name
        path.write_text(json.dumps({"events": events}), encoding="utf-8")
        return path

    def run_fixture(self, events, name="events.json"):
        from robert_agent import run_once
        return run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.write_fixture(name, events),
            dry_run=True,
            skip_external=True,
        )

    def fetchone(self, sql, params=()):
        with closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute(sql, params).fetchone()

    def fetchall(self, sql, params=()):
        with closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute(sql, params).fetchall()

    def create_review_pr_workstream(self):
        result = self.run_fixture(
            [
                {
                    "id": "review-request-707",
                    "number": 707,
                    "source_type": "pull_request",
                    "event_type": "review_request",
                    "actor_login": "github",
                    "requester_login": "wklken",
                    "requested_reviewer": "robert-bot",
                    "pr_author_login": "feature-author",
                    "body": "",
                    "title": "Review feature PR",
                    "intent": "review_request",
                    "base_branch": "master",
                    "event_at": "2026-06-17T03:00:00Z",
                }
            ],
            name="review-request-pr.json",
        )
        self.assertTrue(result["ok"], result)
        return result

    def issue_assignment(self, intent="analysis", body="@robert-bot please analyze this"):
        return {
            "id": "assign-issue-101",
            "number": 101,
            "source_type": "issue",
            "event_type": "assigned",
            "actor_login": "github",
            "assignment_actor_login": "wklken",
            "assigned_to": "robert-bot",
            "body": body,
            "intent": intent,
            "event_at": "2026-06-17T01:00:00Z",
        }

    def test_issue_assignment_for_analysis_creates_comment_workflow(self):
        result = self.run_fixture([self.issue_assignment()])

        self.assertTrue(result["ok"], result)
        row = self.fetchone(
            """
            SELECT t.workstream_id, t.route_id, t.expected_output, a.worktree_path
            FROM tasks t
            JOIN attempts a ON a.task_id = t.task_id
            """
        )
        event_row = self.fetchone(
            """
            SELECT te.relationship, ge.event_fingerprint
            FROM task_events te
            JOIN github_events ge ON ge.event_id = te.event_id
            """
        )
        prompt = Path(result["prompt_paths"][0]).read_text(encoding="utf-8")

        self.assertEqual(
            row,
            (
                "github:example/backend#101",
                "comment-analysis",
                "comment_analysis",
                None,
            ),
        )
        self.assertEqual(event_row, ("trigger", "assigned:assign-issue-101"))
        self.assertIn("expected_output: comment_analysis", prompt)
        self.assertIn("assigned:assign-issue-101", prompt)

    def test_issue_assignment_bugfix_result_materializes_dd_pr_workflow(self):
        from robert_agent.worker import result

        first = self.run_fixture(
            [
                self.issue_assignment(
                    intent="bug_fix",
                    body="@robert-bot please fix this bug",
                )
            ]
        )
        self.assertTrue(first["ok"], first)
        task_id, attempt_id = self.fetchone("SELECT task_id, attempt_id FROM attempts")
        record = result.record_result(
            self.db_path,
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "output_type": "new_pr",
                "planned_github_actions": _new_pr_actions(task_id=task_id),
                "consumed_event_fingerprints": ["assigned:assign-issue-101"],
                "verification": _passed_verification(),
                "handoff": "opened PR",
                "used_skills": ["fast-small-pr"],
            },
        )
        self.assertTrue(record["ok"], record)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE github_actions SET publish_status = 'published' WHERE result_id = ?",
                (record["result_id"],),
            )

        audited = self.run_fixture([], name="empty-after-pr.json")

        self.assertTrue(audited["ok"], audited)
        pr_workstream = self.fetchone(
            """
            SELECT workstream_id, origin_workstream_id, lifecycle
            FROM workstreams
            WHERE workstream_id = 'github:example/backend!909'
            """
        )
        task_state = self.fetchone(
            "SELECT lifecycle FROM tasks WHERE task_id = ?",
            (task_id,),
        )
        self.assertEqual(
            pr_workstream,
            (
                "github:example/backend!909",
                "github:example/backend#101",
                "completed",
            ),
        )
        self.assertEqual(task_state, ("completed",))

    def test_issue_followup_comment_stays_on_existing_workflow_context(self):
        first = self.run_fixture(
            [
                self.issue_assignment(
                    intent="bug_fix",
                    body="@robert-bot please fix this bug",
                )
            ]
        )
        self.assertTrue(first["ok"], first)

        second = self.run_fixture(
            [
                {
                    "id": "issue-context-101",
                    "number": 101,
                    "source_type": "issue",
                    "event_type": "comment",
                    "actor_login": "reviewer",
                    "author_association": "MEMBER",
                    "body": "@robert-bot extra repro detail for the active task",
                    "intent": "analysis",
                    "event_at": "2026-06-17T01:10:00Z",
                }
            ],
            name="issue-followup.json",
        )

        self.assertTrue(second["ok"], second)
        task_count = self.fetchone("SELECT COUNT(*) FROM tasks")[0]
        relationships = self.fetchall(
            """
            SELECT te.relationship, ge.event_fingerprint
            FROM task_events te
            JOIN github_events ge ON ge.event_id = te.event_id
            ORDER BY te.relationship, ge.event_fingerprint
            """
        )
        self.assertEqual(task_count, 1)
        self.assertEqual(
            relationships,
            [
                ("context", "comment:issue-context-101"),
                ("trigger", "assigned:assign-issue-101"),
            ],
        )

    def test_dd_pr_followup_routes_to_existing_pr_workflow(self):
        from robert_agent.worker import result

        first = self.run_fixture(
            [
                self.issue_assignment(
                    intent="bug_fix",
                    body="@robert-bot please fix this bug",
                )
            ]
        )
        self.assertTrue(first["ok"], first)
        task_id, attempt_id = self.fetchone("SELECT task_id, attempt_id FROM attempts")
        record = result.record_result(
            self.db_path,
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "output_type": "new_pr",
                "planned_github_actions": _new_pr_actions(task_id=task_id),
                "consumed_event_fingerprints": ["assigned:assign-issue-101"],
                "verification": _passed_verification(),
                "handoff": "opened PR",
                "used_skills": ["fast-small-pr"],
            },
        )
        self.assertTrue(record["ok"], record)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE github_actions SET publish_status = 'published' WHERE result_id = ?",
                (record["result_id"],),
            )
        audited = self.run_fixture([], name="empty-after-open-pr.json")
        self.assertTrue(audited["ok"], audited)

        followup = self.run_fixture(
            [
                {
                    "id": "dd-pr-review-909",
                    "number": 909,
                    "source_type": "pull_request",
                    "event_type": "review",
                    "actor_login": "reviewer",
                    "author_association": "MEMBER",
                    "body": "@robert-bot please address this review",
                    "intent": "bug_fix",
                    "has_open_dd_pr": True,
                    "existing_pr_head_branch": "codex/dd-101-fix",
                    "metadata": {
                        "dd_workstream": {
                            "origin_workstream_id": "github:example/backend#101",
                            "source_issue": "101",
                        }
                    },
                    "workstream_id": "github:example/backend!909",
                    "origin_workstream_id": "github:example/backend#101",
                    "event_at": "2026-06-17T02:00:00Z",
                }
            ],
            name="dd-pr-followup.json",
        )

        self.assertTrue(followup["ok"], followup)
        rows = self.fetchall(
            """
            SELECT t.workstream_id, t.route_id, t.expected_output, a.branch_name
            FROM tasks t
            JOIN attempts a ON a.task_id = t.task_id
            ORDER BY t.created_at, t.task_id
            """
        )
        prompt = Path(followup["prompt_paths"][0]).read_text(encoding="utf-8")
        self.assertEqual(
            rows[-1],
            (
                "github:example/backend!909",
                "update-existing-pr",
                "update_existing_pr",
                "codex/dd-101-fix",
            ),
        )
        self.assertIn("push_existing_pr", prompt)
        self.assertIn("review:dd-pr-review-909", prompt)

    def test_third_party_pr_question_routes_to_review_comment_workflow(self):
        result = self.run_fixture(
            [
                {
                    "id": "third-party-pr-question",
                    "number": 707,
                    "source_type": "pull_request",
                    "event_type": "comment",
                    "actor_login": "wklken",
                    "body": "@robert-bot can you review this approach?",
                    "intent": "review_request",
                    "has_open_dd_pr": False,
                    "event_at": "2026-06-17T03:00:00Z",
                }
            ],
            name="third-party-pr-question.json",
        )

        self.assertTrue(result["ok"], result)
        row = self.fetchone(
            """
            SELECT t.workstream_id, t.route_id, t.expected_output, a.worktree_path
            FROM tasks t
            JOIN attempts a ON a.task_id = t.task_id
            """
        )
        prompt = Path(result["prompt_paths"][0]).read_text(encoding="utf-8")
        self.assertEqual(
            row,
            (
                "github:example/backend!707",
                "review-comment",
                "review_comment",
                None,
            ),
        )
        self.assertIn("allowed_github_actions: [\"comment\"]", prompt)
        self.assertNotIn("allowed_github_actions: [\"open_pr\"", prompt)

    def test_review_request_creates_source_review_worktree(self):
        result = self.create_review_pr_workstream()
        row = self.fetchone(
            """
            SELECT t.workstream_id, t.route_id, t.expected_output, a.branch_name, a.worktree_path
            FROM tasks t
            JOIN attempts a ON a.task_id = t.task_id
            """
        )
        prompt = Path(result["prompt_paths"][0]).read_text(encoding="utf-8")
        self.assertEqual(row[0:3], ("github:example/backend!707", "review-pr", "pr_review_comment"))
        self.assertEqual(row[3], "review/pr-707-review-feature-pr")
        self.assertTrue(row[4].endswith(".worktrees/review__pr-707-review-feature-pr"))
        self.assertIn('allowed_github_actions: ["comment"]', prompt)
        self.assertIn("required_skills: []", prompt)
        self.assertIn('"pr_author_login": "feature-author"', prompt)
        self.assertIn("@feature-author <public review summary>", prompt)
        self.assertIn("Review PR source workflow:", prompt)
        self.assertNotIn("Review-point evaluation:", prompt)
        self.assertNotIn("push_existing_pr", prompt)
        self.assertNotIn("open_pr", prompt)

    def test_review_request_records_pr_author_as_scoped_participant(self):
        self.create_review_pr_workstream()

        metadata_json = self.fetchone(
            """
            SELECT metadata_json
            FROM workstreams
            WHERE workstream_id = 'github:example/backend!707'
            """
        )[0]
        metadata = json.loads(metadata_json)
        self.assertEqual(metadata["review_participants"], ["feature-author"])
        self.assertEqual(metadata["review_authorized_by"], "wklken")
        self.assertEqual(metadata["review_authorized_event"], "review_request:review-request-707")

    def test_pr_author_contributor_followup_creates_comment_only_review_discussion(self):
        self.create_review_pr_workstream()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE tasks
                SET lifecycle = 'completed'
                WHERE workstream_id = 'github:example/backend!707'
                """
            )
            conn.execute(
                """
                UPDATE workstreams
                SET lifecycle = 'completed', active_task_id = NULL
                WHERE workstream_id = 'github:example/backend!707'
                """
            )

        followup = self.run_fixture(
            [
                {
                    "id": "pr-author-followup-707",
                    "number": 707,
                    "source_type": "pull_request",
                    "event_type": "comment",
                    "actor_login": "feature-author",
                    "author_association": "CONTRIBUTOR",
                    "body": "@robert-bot I have a question about this review.",
                    "workstream_id": "github:example/backend!707",
                    "intent": "discussion",
                    "event_at": "2026-06-17T04:00:00Z",
                }
            ],
            name="review-participant-followup.json",
        )

        self.assertTrue(followup["ok"], followup)
        rows = self.fetchall(
            """
            SELECT task_id, parent_task_id, route_id, expected_output
            FROM tasks
            WHERE workstream_id = 'github:example/backend!707'
            ORDER BY created_at, task_id
            """
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][1], rows[0][0])
        self.assertEqual(rows[1][2:], ("review-comment", "review_comment"))
        event_row = self.fetchone(
            """
            SELECT authorization_status, actor_login
            FROM github_events
            WHERE event_fingerprint = 'comment:pr-author-followup-707'
            """
        )
        self.assertEqual(event_row, ("accepted_review_participant", "feature-author"))
        prompt = Path(followup["prompt_paths"][0]).read_text(encoding="utf-8")
        self.assertIn('allowed_github_actions: ["comment"]', prompt)
        self.assertNotIn("push_existing_pr", prompt)
        self.assertNotIn("open_pr", prompt)

    def test_pr_author_fix_request_stays_comment_only(self):
        self.create_review_pr_workstream()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE tasks
                SET lifecycle = 'completed'
                WHERE workstream_id = 'github:example/backend!707'
                """
            )
            conn.execute(
                """
                UPDATE workstreams
                SET lifecycle = 'completed', active_task_id = NULL
                WHERE workstream_id = 'github:example/backend!707'
                """
            )

        followup = self.run_fixture(
            [
                {
                    "id": "pr-author-fix-707",
                    "number": 707,
                    "source_type": "pull_request",
                    "event_type": "comment",
                    "actor_login": "feature-author",
                    "author_association": "CONTRIBUTOR",
                    "body": "@robert-bot please fix this PR for me",
                    "workstream_id": "github:example/backend!707",
                    "intent": "bug_fix",
                    "event_at": "2026-06-17T04:10:00Z",
                }
            ],
            name="review-participant-fix-request.json",
        )

        self.assertTrue(followup["ok"], followup)
        row = self.fetchone(
            """
            SELECT route_id, expected_output
            FROM tasks
            WHERE task_id != (
              SELECT task_id
              FROM tasks
              ORDER BY created_at, task_id
              LIMIT 1
            )
            """
        )
        self.assertEqual(row, ("review-comment", "review_comment"))
        prompt = Path(followup["prompt_paths"][0]).read_text(encoding="utf-8")
        self.assertIn('allowed_github_actions: ["comment"]', prompt)
        self.assertNotIn("push_existing_pr", prompt)
        self.assertNotIn("open_pr", prompt)

    def test_pr_author_followup_does_not_resume_waiting_review_task(self):
        self.create_review_pr_workstream()
        task_id, attempt_id = self.fetchone("SELECT task_id, attempt_id FROM attempts")
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE tasks SET lifecycle = 'waiting_for_user', updated_at = ? WHERE task_id = ?",
                ("2026-06-17T03:30:00Z", task_id),
            )
            conn.execute(
                "UPDATE workstreams SET lifecycle = 'waiting_for_user', active_task_id = ?, updated_at = ? WHERE workstream_id = ?",
                (
                    task_id,
                    "2026-06-17T03:30:00Z",
                    "github:example/backend!707",
                ),
            )
            conn.execute(
                "UPDATE attempts SET status = 'completed', finished_at = ? WHERE attempt_id = ?",
                ("2026-06-17T03:30:00Z", attempt_id),
            )
        self.assertEqual(
            self.fetchone(
                """
                SELECT t.lifecycle, w.lifecycle, w.active_task_id
                FROM tasks t
                JOIN workstreams w ON w.workstream_id = t.workstream_id
                WHERE t.task_id = ?
                """,
                (task_id,),
            ),
            ("waiting_for_user", "waiting_for_user", task_id),
        )

        context = self.run_fixture(
            [
                {
                    "id": "pr-author-waiting-context-707",
                    "number": 707,
                    "source_type": "pull_request",
                    "event_type": "comment",
                    "actor_login": "feature-author",
                    "author_association": "CONTRIBUTOR",
                    "body": "@robert-bot I can answer the question.",
                    "workstream_id": "github:example/backend!707",
                    "intent": "bug_fix",
                    "event_at": "2026-06-17T04:20:00Z",
                }
            ],
            name="review-participant-waiting-context.json",
        )

        self.assertTrue(context["ok"], context)
        self.assertEqual(self.fetchone("SELECT COUNT(*) FROM tasks")[0], 1)
        self.assertEqual(
            self.fetchall(
                """
                SELECT te.relationship, ge.event_fingerprint
                FROM task_events te
                JOIN github_events ge ON ge.event_id = te.event_id
                WHERE te.task_id = ?
                ORDER BY ge.event_at, ge.event_id
                """,
                (task_id,),
            ),
            [
                ("trigger", "review_request:review-request-707"),
                ("context", "comment:pr-author-waiting-context-707"),
            ],
        )

    def test_third_party_pr_fix_request_stays_comment_only(self):
        result = self.run_fixture(
            [
                {
                    "id": "third-party-pr-fix-request",
                    "number": 707,
                    "source_type": "pull_request",
                    "event_type": "comment",
                    "actor_login": "wklken",
                    "body": "@robert-bot please fix this PR",
                    "intent": "bug_fix",
                    "has_open_dd_pr": False,
                    "event_at": "2026-06-17T03:10:00Z",
                }
            ],
            name="third-party-pr-fix-request.json",
        )

        self.assertTrue(result["ok"], result)
        row = self.fetchone(
            """
            SELECT t.workstream_id, t.route_id, t.expected_output, a.worktree_path
            FROM tasks t
            JOIN attempts a ON a.task_id = t.task_id
            """
        )
        prompt = Path(result["prompt_paths"][0]).read_text(encoding="utf-8")
        self.assertEqual(
            row,
            (
                "github:example/backend!707",
                "review-comment",
                "review_comment",
                None,
            ),
        )
        self.assertIn("allowed_github_actions: [\"comment\"]", prompt)
        self.assertNotIn("allowed_github_actions: [\"open_pr\"", prompt)
        self.assertNotIn("allowed_github_actions: [\"push_existing_pr\"", prompt)

    def test_trusted_reply_resumes_waiting_for_user_workflow(self):
        from robert_agent.worker import result
        from robert_agent import board

        first = self.run_fixture(
            [
                self.issue_assignment(
                    intent="waiting_for_user",
                    body="@robert-bot please ask what is missing",
                )
            ]
        )
        self.assertTrue(first["ok"], first)
        task_id, attempt_id = self.fetchone("SELECT task_id, attempt_id FROM attempts")
        record = result.record_result(
            self.db_path,
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "output_type": "waiting_for_user",
                "planned_github_actions": [
                    {
                        "type": "comment",
                        "target_url": "https://github.com/example/backend/issues/101",
                        "body": _dd_comment_body("Please confirm the desired behavior.", task_id=task_id),
                    }
                ],
                "consumed_event_fingerprints": ["assigned:assign-issue-101"],
                "verification": [],
                "handoff": "waiting for trusted user clarification",
                "used_skills": [],
            },
        )
        self.assertTrue(record["ok"], record)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE github_actions SET publish_status = 'published' WHERE result_id = ?",
                (record["result_id"],),
            )

        waiting = self.run_fixture([], name="empty-after-waiting.json")

        self.assertTrue(waiting["ok"], waiting)
        self.assertEqual(
            self.fetchone(
                """
                SELECT t.lifecycle, w.lifecycle, w.active_task_id
                FROM tasks t
                JOIN workstreams w ON w.workstream_id = t.workstream_id
                WHERE t.task_id = ?
                """,
                (task_id,),
            ),
            ("waiting_for_user", "waiting_for_user", task_id),
        )
        waiting_item = board.list_board(self.db_path)["items"][0]
        self.assertEqual(waiting_item["column"], "waiting")
        self.assertEqual(waiting_item["attention_signals"][0]["type"], "operator_question")

        context = self.run_fixture(
            [
                {
                    "id": "member-context-101",
                    "number": 101,
                    "source_type": "issue",
                    "event_type": "comment",
                    "actor_login": "reviewer",
                    "author_association": "MEMBER",
                    "body": "@robert-bot Additional detail while waiting: the bug affects POST only.",
                    "intent": "analysis",
                    "event_at": "2026-06-17T03:30:00Z",
                }
            ],
            name="member-waiting-context.json",
        )

        self.assertTrue(context["ok"], context)
        self.assertEqual(
            self.fetchone("SELECT COUNT(*) FROM tasks")[0],
            1,
        )
        self.assertEqual(
            self.fetchall(
                """
                SELECT te.relationship, ge.event_fingerprint
                FROM task_events te
                JOIN github_events ge ON ge.event_id = te.event_id
                WHERE te.task_id = ?
                ORDER BY ge.event_at, ge.event_id
                """,
                (task_id,),
            ),
            [
                ("consumed", "assigned:assign-issue-101"),
                ("context", "comment:member-context-101"),
            ],
        )

        resumed = self.run_fixture(
            [
                {
                    "id": "trusted-resume-101",
                    "number": 101,
                    "source_type": "issue",
                    "event_type": "comment",
                    "actor_login": "wklken",
                    "author_association": "OWNER",
                    "body": "@robert-bot confirmed, please implement it",
                    "intent": "bug_fix",
                    "event_at": "2026-06-17T04:00:00Z",
                }
            ],
            name="trusted-resume.json",
        )

        self.assertTrue(resumed["ok"], resumed)
        rows = self.fetchall(
            """
            SELECT task_id, parent_task_id, lifecycle, route_id, expected_output
            FROM tasks
            ORDER BY created_at, task_id
            """
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], task_id)
        self.assertEqual(rows[0][2], "completed")
        self.assertEqual(
            rows[1][1:],
            (task_id, "queued", "new-pr", "new_pr"),
        )
        relationships = self.fetchall(
            """
            SELECT te.relationship, ge.event_fingerprint
            FROM task_events te
            JOIN github_events ge ON ge.event_id = te.event_id
            WHERE te.task_id = ?
            ORDER BY te.relationship, ge.event_fingerprint
            """,
            (rows[1][0],),
        )
        self.assertEqual(
            relationships,
            [
                ("context", "comment:member-context-101"),
                ("trigger", "comment:trusted-resume-101"),
            ],
        )
        resumed_item = board.list_board(self.db_path)["items"][0]
        self.assertEqual(resumed_item["column"], "todo")
        self.assertEqual(resumed_item["attention_signals"], [])
        prompt = Path(resumed["prompt_paths"][0]).read_text(encoding="utf-8")
        self.assertIn("comment:trusted-resume-101", prompt)
        self.assertIn("comment:member-context-101", prompt)


if __name__ == "__main__":
    unittest.main()
