from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import unittest
from robert_agent.work.application import source_lifecycle
from tests.support.board import BoardReadModelFixture


class BoardReadModelTests(BoardReadModelFixture):
    def test_active_cards_precede_newer_completed_cards_across_pages(self):
        from robert_agent.readmodels import board

        seeded = self._seed_six_columns()
        seeded["done2"] = self._create("Second done card")
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE work_items SET updated_at = '2027-01-01T00:00:00Z' WHERE work_item_id = ?",
                (seeded["done"]["work_item_id"],),
            )
            conn.execute(
                "UPDATE work_items SET completed_at = '2027-01-02T00:00:00Z', "
                "updated_at = '2027-01-02T00:00:00Z' WHERE work_item_id = ?",
                (seeded["done2"]["work_item_id"],),
            )
        for column in (None, "waiting", "done"):
            with self.subTest(column=column):
                pages = []
                cursor = None
                while True:
                    page = board.list_board(self.db_path, column=column, limit=1, cursor=cursor)
                    pages.extend(page["items"])
                    cursor = page["next_cursor"]
                    if not cursor:
                        break
                expected = {
                    item["work_item_id"]
                    for key, item in seeded.items()
                    if column is None or key == column or (column == "done" and key == "done2")
                }
                self.assertEqual(len(pages), len(expected))
                self.assertEqual({item["work_item_id"] for item in pages}, expected)
                if column is None:
                    self.assertEqual(pages[-1]["column"], "done")
                    self.assertTrue(all(item["column"] != "done" for item in pages[:-2]))
                    self.assertEqual([item["title"] for item in pages[-2:]], ["Second done card", "Done card"])

        import base64

        boundary = ["2027-01-02T00:00:00Z", seeded["done2"]["work_item_id"]]
        legacy_cursor = base64.urlsafe_b64encode(json.dumps(boundary).encode()).decode().rstrip("=")
        remaining = board.list_board(self.db_path, cursor=legacy_cursor)["items"]
        self.assertEqual(len(remaining), len(seeded) - 1)
        self.assertEqual(remaining[0]["title"], "Done card")

    def test_exhausted_pr_remediation_is_visible_in_attention_and_clears_when_resolved(self):
        from robert_agent.readmodels import board

        seeded = self._seed_six_columns()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO pr_remediation_episodes(
                  episode_id, workstream_id, episode_kind, subject_key,
                  observed_head_sha, observed_base_sha, status, first_seen_at,
                  updated_at, terminal_at, metadata_json
                ) VALUES ('ci-old', 'github:example/repo!12', 'ci', 'head:base',
                          'head', 'base', 'exhausted', '2026-07-20T00:00:00Z',
                          '2026-07-20T00:01:00Z', '2026-07-20T00:01:00Z',
                          '{"last_transition_reason":"failure_evidence_unavailable"}')
                """
            )
        detail = board.get_work_item_detail(self.db_path, seeded["review"]["work_item_id"])
        self.assertEqual(detail["column"], "waiting")
        self.assertEqual(detail["reason_code"], "pr_remediation_exhausted")
        self.assertIn("failure_evidence_unavailable", detail["reason_summary"])
        self.assertIn("failure_evidence_unavailable", detail["status_summary"]["cause"]["text"])
        self.assertTrue(detail["status_summary"]["needs_action"])
        self.assertEqual(detail["valid_commands"], ["request_changes", "cancel"])
        self.assertEqual(board.list_board(self.db_path)["counts"]["needs_action"], 2)
        attention = board.list_board(self.db_path, attention=True)
        self.assertIn(seeded["review"]["work_item_id"], {item["work_item_id"] for item in attention["items"]})
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO pr_remediation_episodes(
                  episode_id, workstream_id, episode_kind, subject_key,
                  observed_head_sha, observed_base_sha, status, first_seen_at,
                  updated_at, terminal_at, metadata_json
                ) VALUES ('ci-new', 'github:example/repo!12', 'ci', 'head2:base',
                          'head2', 'base', 'resolved', '2026-07-21T00:00:00Z',
                          '2026-07-21T00:01:00Z', '2026-07-21T00:01:00Z', '{}')
                """
            )
        detail = board.get_work_item_detail(self.db_path, seeded["review"]["work_item_id"])
        self.assertEqual(detail["column"], "review")
        self.assertFalse(detail["status_summary"]["needs_action"])
        self.assertEqual(board.list_board(self.db_path)["counts"]["needs_action"], 1)

    def test_exhausted_remediation_does_not_flag_a_new_snapshot_or_green_rerun(self):
        from robert_agent.persistence import database
        from robert_agent.readmodels import board
        from robert_agent.workflows.pr_remediation import upsert_health_episodes

        item = self._seed_six_columns()["review"]
        stream = {"workstream_id": "github:example/repo!12", "repo_id": "repo-1", "number": 12}
        repo = {"pr_automation": {"ci": {"enabled": True}, "conflict": {"enabled": False}}}
        snapshot = {"state": "open", "head_sha": "head", "base_sha": "base", "mergeable": True}
        now = "2026-07-20T00:00:00Z"

        def observe(observed, ci):
            with closing(database.connect(self.db_path)) as conn, conn:
                upsert_health_episodes(conn, repo=repo, workstream=stream, snapshot=observed, ci_summary=ci, now=now)

        observe(snapshot, {"status": "manual", "reason": "failure_evidence_unavailable"})
        self.assertEqual(board.get_work_item_detail(self.db_path, item["work_item_id"])["column"], "waiting")

        for observed, ci in (
            ({**snapshot, "head_sha": "new-head"}, {"status": "waiting"}),
            (snapshot, {"status": "green"}),
        ):
            with self.subTest(head=observed["head_sha"], status=ci["status"]):
                observe(observed, ci)
                self.assertEqual(board.get_work_item_detail(self.db_path, item["work_item_id"])["column"], "review")

    def test_projection_places_each_item_in_exactly_one_of_six_columns(self):
        from robert_agent.readmodels import board

        self._seed_six_columns()
        result = board.list_board(self.db_path)

        self.assertEqual(
            [column["id"] for column in result["columns"]],
            ["backlog", "todo", "doing", "waiting", "review", "done"],
        )
        self.assertEqual({column["count"] for column in result["columns"]}, {1})
        by_title = {item["title"]: item["column"] for item in result["items"]}
        self.assertEqual(
            by_title,
            {
                "Backlog card": "backlog",
                "Todo card": "todo",
                "Doing card": "doing",
                "Waiting card": "waiting",
                "Review card": "review",
                "Done card": "done",
            },
        )

    def test_resolved_old_attention_does_not_return_card_to_waiting(self):
        from robert_agent.readmodels import board
        from robert_agent.work.application.commands import record_system_event

        item = self._create("Resolved card", start=True)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            question = record_system_event(
                conn,
                item["work_item_id"],
                event_type="publication_failed",
                idempotency_key="failure-1",
                body="Publish failed.",
            )
            record_system_event(
                conn,
                item["work_item_id"],
                event_type="retry_requested",
                idempotency_key="resolve-1",
                resolves_event_id=question["event_id"],
            )

        result = board.list_board(self.db_path)
        self.assertEqual(result["items"][0]["column"], "todo")
        self.assertEqual(result["items"][0]["attention_signals"], [])

    def test_unmerged_closed_pr_remains_in_review_with_attention(self):
        from robert_agent.readmodels import board
        from robert_agent.work.application.commands import record_system_event

        item = self._create("Closed PR card", start=True)
        now = "2026-07-19T03:00:00+00:00"
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            task_id = conn.execute(
                "SELECT active_task_id FROM workstreams WHERE workstream_id = ?",
                (item["workstream_id"],),
            ).fetchone()[0]
            conn.execute("UPDATE tasks SET lifecycle = 'completed' WHERE task_id = ?", (task_id,))
            conn.execute(
                "UPDATE workstreams SET lifecycle = 'completed', active_task_id = NULL WHERE workstream_id = ?",
                (item["workstream_id"],),
            )
            conn.execute(
                "INSERT INTO github_sources(source_id, repo_id, source_key, source_type, number, html_url, title, state) VALUES ('pr-closed', 'repo-1', 'github:example/repo!44', 'pull_request', 44, 'https://github.com/example/repo/pull/44', 'Closed PR', 'closed')"
            )
            conn.execute(
                "INSERT INTO workstreams(workstream_id, repo_id, primary_source_id, origin_workstream_id, lifecycle, created_at, updated_at) VALUES ('github:example/repo!44', 'repo-1', 'pr-closed', ?, 'completed', ?, ?)",
                (item["workstream_id"], now, now),
            )
            record_system_event(
                conn,
                item["work_item_id"],
                event_type="unmerged_pr_closed",
                idempotency_key="closed-pr-44",
                body="PR #44 closed without merge.",
                now=now,
            )

        projected = board.list_board(self.db_path)["items"][0]
        self.assertEqual(projected["column"], "review")
        self.assertEqual(projected["reason_code"], "unmerged_pr_closed")
        self.assertIn("request_changes", projected["valid_commands"])

    def test_direct_closed_pr_stays_in_review_until_operator_cancels(self):
        from robert_agent.readmodels import board
        from robert_agent.work.application import commands as work_items

        now = "2026-10-13T06:35:18+00:00"
        source_key = "github:example/repo!3353"
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute(
                "INSERT INTO github_sources(source_id, repo_id, source_key, source_type, number, html_url, title, state) VALUES ('pr-direct', 'repo-1', ?, 'pull_request', 3353, 'https://github.com/example/repo/pull/3353', 'Direct PR', 'open')",
                (source_key,),
            )
            conn.execute(
                "INSERT INTO workstreams(workstream_id, repo_id, primary_source_id, lifecycle, created_at, updated_at) VALUES (?, 'repo-1', 'pr-direct', 'active', ?, ?)",
                (source_key, now, now),
            )
            item = work_items.ensure_github_work_item(
                conn,
                repo_id="repo-1",
                source_id="pr-direct",
                workstream_id=source_key,
                actor_identity="owner",
                route_confidence="high",
                now=now,
            )
            conn.execute(
                "INSERT INTO tasks(task_id, workstream_id, lifecycle, priority, route_id, expected_output, created_at, updated_at) VALUES ('task-direct', ?, 'completed', 'P1', 'review', 'review', ?, ?)",
                (source_key, now, now),
            )
            source_lifecycle.record_remote_terminal_state(
                conn,
                "repo-1",
                source_key,
                "pr-direct",
                source_key,
                "pull_request",
                3353,
                "task-direct",
                {
                    "state": "closed",
                    "merged": False,
                    "closed_at": "2026-10-13T06:30:26Z",
                },
                "canceled",
                "remote_pr_closed",
                now,
            )

        projected = board.list_board(self.db_path)["items"][0]
        self.assertEqual(projected["column"], "review")
        self.assertEqual(projected["reason_code"], "unmerged_pr_closed")
        self.assertEqual(projected["pr"]["number"], 3353)
        self.assertEqual(projected["pr"]["state"], "closed")
        self.assertEqual(projected["pr"]["url"], "https://github.com/example/repo/pull/3353")
        self.assertIn("cancel", projected["valid_commands"])

        canceled = work_items.execute_command(
            self.db_path,
            item["work_item_id"],
            context=self.context,
            command="cancel",
            expected_version=projected["version"],
            idempotency_key="cancel-direct-closed-pr",
        )
        self.assertTrue(canceled["ok"])
        archived = board.get_work_item_detail(self.db_path, item["work_item_id"])
        self.assertEqual(archived["column"], "history")
        self.assertEqual(archived["valid_commands"], [])
        self.assertEqual(board.list_board(self.db_path)["items"], [])

    def test_issue_origin_is_linked_separately_from_its_derived_pr(self):
        from robert_agent.readmodels import board

        item = self._seed_six_columns()["review"]
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO github_sources(source_id, repo_id, source_key, source_type, number, html_url, title, state) VALUES ('issue-origin', 'repo-1', 'github:example/repo#11', 'issue', 11, 'https://github.com/example/repo/issues/11', 'Original issue', 'open')"
            )
            conn.execute(
                "UPDATE work_items SET origin_source_id = 'issue-origin' WHERE work_item_id = ?",
                (item["work_item_id"],),
            )

        detail = board.get_work_item_detail(self.db_path, item["work_item_id"])
        self.assertEqual(
            detail["source"],
            {
                "type": "issue",
                "number": 11,
                "url": "https://github.com/example/repo/issues/11",
            },
        )
        self.assertEqual(detail["pr"]["number"], 12)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE github_sources SET html_url = 'javascript:alert(1)' WHERE source_id = 'issue-origin'")
        self.assertIsNone(board.get_work_item_detail(self.db_path, item["work_item_id"])["source"]["url"])

    def test_projection_does_not_expose_non_http_pull_request_urls(self):
        from robert_agent.readmodels import board

        items = self._seed_six_columns()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE github_sources SET html_url = 'javascript:alert(1)' WHERE source_id = 'pr-review'")

        detail = board.get_work_item_detail(
            self.db_path,
            items["review"]["work_item_id"],
        )
        self.assertIsNone(detail["pr"]["url"])
        self.assertNotIn("javascript:", json.dumps(detail))

    def _add_failed_followup(self, item, *, task_id="task-followup", failure=None):
        now = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO tasks(task_id, workstream_id, lifecycle, priority, route_id, expected_output, created_at, updated_at) VALUES (?, ?, 'failed', 'P2', 'review', 'multi_agent_review_report', ?, ?)",
                (task_id, item["workstream_id"], now, now),
            )
            for attempt_no in (1, 2):
                conn.execute(
                    "INSERT INTO attempts(attempt_id, task_id, attempt_no, status, started_at, finished_at, failure_json) VALUES (?, ?, ?, 'failed', ?, ?, ?)",
                    (
                        f"{task_id}-attempt-{attempt_no}",
                        task_id,
                        attempt_no,
                        now,
                        now,
                        json.dumps(
                            failure or {"safe_error": "PR snapshot changed while fetching; recapture PR metadata"}
                        ),
                    ),
                )

    def test_completed_item_with_a_later_failed_task_needs_attention(self):
        from robert_agent.readmodels import board

        done = self._seed_six_columns()["done"]
        self._add_failed_followup(done)

        projected = next(
            item for item in board.list_board(self.db_path)["items"] if item["work_item_id"] == done["work_item_id"]
        )
        self.assertEqual(projected["column"], "waiting")
        self.assertEqual(projected["reason_code"], "execution_failed")
        self.assertEqual(projected["valid_commands"], ["reopen"])
        summary = projected["status_summary"]
        self.assertEqual(summary["state"], "failed")
        self.assertEqual(summary["tone"], "danger")
        self.assertEqual(summary["owner"], "operator")
        self.assertTrue(summary["needs_action"])
        self.assertEqual(summary["stage"]["task"], "multi_agent_review_report")
        self.assertEqual(summary["attempts"], {"total": 2, "failed": 2})
        self.assertEqual(
            summary["cause"]["text"],
            "PR snapshot changed while fetching; recapture PR metadata",
        )
        self.assertEqual(summary["cause"]["source"], "attempt")
        self.assertEqual(summary["primary"], "reopen")
        overview = board.list_board(self.db_path)
        self.assertEqual((overview["counts"]["waiting"], overview["counts"]["done"]), (2, 0))
        self.assertEqual(overview["counts"]["needs_action"], 2)
        self.assertEqual(board.list_board(self.db_path, column="done")["items"], [])

    def _completed_github_item_with_failed_followup(self):
        from robert_agent.work.application import commands as work_items

        now = "2026-10-13T06:35:18+00:00"
        source_key = "github:example/repo!10"
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute(
                "INSERT INTO github_sources(source_id, repo_id, source_key, source_type, number, html_url, title, state) VALUES ('pr-10', 'repo-1', ?, 'pull_request', 10, 'https://github.com/example/repo/pull/10', 'PR 10', 'open')",
                (source_key,),
            )
            conn.execute(
                "INSERT INTO workstreams(workstream_id, repo_id, primary_source_id, lifecycle, created_at, updated_at) VALUES (?, 'repo-1', 'pr-10', 'completed', ?, ?)",
                (source_key, now, now),
            )
            item = work_items.ensure_github_work_item(
                conn,
                repo_id="repo-1",
                source_id="pr-10",
                workstream_id=source_key,
                actor_identity="owner",
                route_confidence="high",
                now=now,
            )
            conn.execute(
                "INSERT INTO tasks(task_id, workstream_id, lifecycle, priority, route_id, expected_output, created_at, updated_at) VALUES ('task-review', ?, 'completed', 'P2', 'review', 'pr_review_comment', ?, ?)",
                (source_key, now, now),
            )
            conn.execute(
                "UPDATE work_items SET completed_at = ? WHERE work_item_id = ?",
                (now, item["work_item_id"]),
            )
        self._add_failed_followup({"workstream_id": source_key})
        return item

    def test_completed_github_item_with_a_later_failed_task_offers_retrigger(self):
        from robert_agent.readmodels import board
        from robert_agent.work.application import commands as work_items
        from robert_agent.work.errors import WorkItemConflictError

        item = self._completed_github_item_with_failed_followup()
        projected = board.list_board(self.db_path)["items"][0]
        self.assertEqual(projected["column"], "waiting")
        self.assertEqual(projected["valid_commands"], ["retrigger", "reopen"])
        self.assertEqual(projected["status_summary"]["primary"], "retrigger")
        self.assertEqual(projected["task_id"], "task-followup")

        work_items.retrigger_failed_github_task(
            self.db_path,
            "task-followup",
            context=self.context,
            idempotency_key="rerun-10",
        )
        requested = board.list_board(self.db_path)["items"][0]
        self.assertEqual(requested["column"], "todo")
        self.assertEqual(requested["reason_code"], "rerun_requested")
        self.assertEqual(requested["valid_commands"], [])
        self.assertEqual(requested["status_summary"]["state"], "queued")
        self.assertEqual(requested["status_summary"]["owner"], "robert")
        self.assertFalse(requested["status_summary"]["needs_action"])

        with self.assertRaisesRegex(WorkItemConflictError, "retrigger request is already pending"):
            work_items.execute_command(
                self.db_path,
                item["work_item_id"],
                context=self.context,
                command="reopen",
                expected_version=projected["version"],
                idempotency_key="stale-reopen",
            )

    def test_completed_github_item_offers_reopen_for_failed_operator_followup(self):
        from robert_agent.readmodels import board
        from robert_agent.work.application import commands as work_items

        item = self._completed_github_item_with_failed_followup()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute(
                "UPDATE tasks SET metadata_json=? WHERE task_id='task-followup'",
                (json.dumps({"trigger_work_item_event_id": "operator-reply"}),),
            )
            work_items.record_system_event(
                conn,
                item["work_item_id"],
                event_type="execution_failed",
                idempotency_key="operator-followup-failed",
                metadata={"task_id": "task-followup"},
            )

        projected = board.get_work_item_detail(self.db_path, item["work_item_id"])
        self.assertEqual(projected["valid_commands"], ["reopen"])
        self.assertEqual(projected["status_summary"]["primary"], "reopen")
        result = work_items.execute_command(
            self.db_path,
            item["work_item_id"],
            context=self.context,
            command="reopen",
            expected_version=projected["version"],
            idempotency_key="reopen-failed-followup",
        )
        self.assertTrue(result["ok"])

    def test_completed_item_with_a_closed_source_stays_done(self):
        from robert_agent.readmodels import board

        done = self._seed_six_columns()["done"]
        self._add_failed_followup(done)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO github_sources(source_id, repo_id, source_key, source_type, number, html_url, title, state) VALUES ('closed-src', 'repo-1', 'github:example/repo#99', 'issue', 99, 'https://github.com/example/repo/issues/99', 'Closed', 'closed')"
            )
            conn.execute(
                "UPDATE work_items SET origin_source_id = 'closed-src' WHERE work_item_id = ?",
                (done["work_item_id"],),
            )

        detail = board.get_work_item_detail(self.db_path, done["work_item_id"])
        self.assertEqual(detail["column"], "done")
        self.assertEqual(detail["status_summary"]["state"], "completed")
        self.assertFalse(detail["status_summary"]["needs_action"])

    def test_status_summary_names_state_owner_and_primary_action_per_column(self):
        from robert_agent.readmodels import board

        self._seed_six_columns()
        summaries = {
            item["title"].split()[0].lower(): item["status_summary"] for item in board.list_board(self.db_path)["items"]
        }
        expected = {
            "backlog": ("draft", "neutral", "operator", False, "start"),
            "todo": ("queued", "neutral", "robert", False, None),
            "doing": ("running", "running", "robert", False, None),
            "waiting": ("needs_reply", "attention", "initiator", True, "reply"),
            "review": ("in_review", "info", "github", False, "open_pr"),
            "done": ("completed", "success", "none", False, None),
        }
        for name, (state, tone, owner, needs_action, primary) in expected.items():
            summary = summaries[name]
            self.assertEqual(
                (summary["state"], summary["tone"], summary["owner"], summary["needs_action"], summary["primary"]),
                (state, tone, owner, needs_action, primary),
                name,
            )
        needing_action = board.list_board(self.db_path, attention="true")["items"]
        self.assertEqual([item["title"] for item in needing_action], ["Waiting card"])
        overview = board.list_board(self.db_path, limit=1)
        self.assertEqual(overview["counts"]["needs_action"], 1)
        self.assertEqual([item["title"] for item in overview["attention_items"]], ["Waiting card"])
        self.assertEqual(summaries["waiting"]["cause"]["text"], "Choose the compatibility target.")
        self.assertEqual(summaries["waiting"]["cause"]["source"], "attention")
        self.assertEqual(summaries["waiting"]["owner_identity"], "owner")

    def test_filters_cursor_detail_and_timeline_are_safe(self):
        from robert_agent.readmodels import board

        for index in range(55):
            self._create(f"Card {index:02d}", priority="P1" if index % 2 else "P2")
        first = board.list_board(self.db_path, repo="repo-1", priority="P1", limit=10)
        second = board.list_board(
            self.db_path,
            repo="repo-1",
            priority="P1",
            limit=10,
            cursor=first["next_cursor"],
        )
        first_ids = {item["work_item_id"] for item in first["items"]}
        second_ids = {item["work_item_id"] for item in second["items"]}
        detail = board.get_work_item_detail(self.db_path, first["items"][0]["work_item_id"])
        timeline = board.list_work_item_events(
            self.db_path,
            first["items"][0]["work_item_id"],
            limit=10,
        )
        serialized = json.dumps({"first": first, "detail": detail, "timeline": timeline})

        self.assertFalse(first_ids & second_ids)
        self.assertTrue(all(item["priority"] == "P1" for item in first["items"]))
        self.assertIn("edit", detail["valid_commands"])
        for forbidden in (
            "metadata_json",
            "payload_json",
            "failure_json",
            "error_json",
            "csrf_token",
        ):
            self.assertNotIn(forbidden, serialized)
        with self.assertRaises(board.BoardQueryError):
            board.list_board(self.db_path, cursor="not-a-cursor")


if __name__ == "__main__":
    unittest.main()
