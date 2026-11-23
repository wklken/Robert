"""Timeline entries come from completed and audited results."""

from contextlib import closing
import json
import sqlite3

from robert_agent.readmodels.timeline import get_receipt, list_completed
from tests.support.board import BoardReadModelFixture


class CompletedWorkTests(BoardReadModelFixture):
    def setUp(self):
        super().setUp()
        self.conn = self.enterContext(closing(sqlite3.connect(self.db_path)))
        self.conn.executescript(
            """
            INSERT INTO github_sources(source_id, repo_id, source_key, source_type, provider, project_key,
                                       external_id, html_url, title)
            VALUES ('ticket', 'repo-1', 'tapd:ticket', 'tapd_story', 'tapd', 'tapd.example.internal/77',
                    '105', 'https://tapd.example.internal/77/prong/stories/view/105', 'Improve [list]');

            INSERT INTO github_sources(source_id, repo_id, source_key, source_type, number, html_url, title)
            VALUES ('pr', 'repo-1', 'github:example/repo!2', 'pull_request', 2,
                    'https://github.com/example/repo/pull/2', 'Implementation PR');

            INSERT INTO workstreams(workstream_id, repo_id, primary_source_id, created_at, updated_at)
            VALUES ('original', 'repo-1', 'ticket', '2026-10-20T00:00:00Z', '2026-10-20T00:00:00Z');

            INSERT INTO workstreams(workstream_id, repo_id, primary_source_id, origin_workstream_id, created_at, updated_at)
            VALUES ('derived', 'repo-1', 'pr', 'original', '2026-10-20T00:00:00Z', '2026-10-20T00:00:00Z');

            INSERT INTO work_items(work_item_id, repo_id, title, origin_type, origin_source_id, workstream_id,
                                   creation_idempotency_key, created_by, created_at, updated_at)
            VALUES ('item', 'repo-1', 'Improve [list]', 'tapd', 'ticket', 'original',
                    'create-item', 'operator', '2026-10-20T00:00:00Z', '2026-10-20T00:00:00Z');
            """
        )
        self.completed_at = "2026-11-01T16:00:00+00:00"

    def result(self, task, *, output="new_pr", lifecycle="completed", when=None, audited=True, attempt=1):
        self.conn.execute(
            "INSERT OR IGNORE INTO tasks(task_id, workstream_id, lifecycle, created_at, updated_at) "
            "VALUES (?, 'derived', ?, '2026-10-20T00:00:00Z', ?)",
            (task, lifecycle, when or self.completed_at),
        )
        result_id = f"{task}-{attempt}"
        self.conn.execute(
            "INSERT INTO attempts(attempt_id, task_id, attempt_no, status) VALUES (?, ?, ?, 'completed')",
            (result_id, task, attempt),
        )
        self.conn.execute(
            "INSERT INTO worker_results(result_id, task_id, attempt_id, output_type, created_at, metadata_json) "
            "VALUES (?, ?, ?, ?, '2026-10-31T00:00:00Z', ?)",
            (result_id, task, result_id, output, json.dumps({"audit": {"status": "accepted"}}) if audited else "{}"),
        )

    def action(self, task, url=None, *, action="open_pr", status="published", attempt=1, index=1):
        result_id = f"{task}-{attempt}"
        self.conn.execute(
            "INSERT INTO github_actions(action_id, result_id, task_id, action_type, target_url, "
            "audit_status, publish_status, created_at) VALUES (?, ?, ?, ?, ?, 'accepted', ?, '2026-10-31T00:00:00Z')",
            (f"{result_id}-{index}", result_id, task, action, url, status),
        )

    def timeline(self, *, view="day", anchor="2026-11-02", zone="Asia/Shanghai"):
        self.conn.commit()
        return list_completed(self.db_path, view=view, anchor_date=anchor, timezone_name=zone)

    def test_uses_task_completion_date_and_links_the_original_request_and_confirmed_deliveries(self):
        self.result("published")
        pr_url = "https://github.com/example/repo/pull/2"
        self.action("published", pr_url)
        self.action("published", pr_url + "?api_key=synthetic", index=2)
        self.action("published", pr_url + "#pullrequestreview-42", action="submit_pr_review", index=3)
        self.action("published", "https://robot:credential@example.com/receipt", index=4)
        self.action("published", "javascript:alert(1)", index=5)
        self.action("published", "https://github.com/example/repo/pull/999", status="skipped", index=6)

        self.assertEqual(
            self.timeline()["entries"],
            [
                {
                    "task_id": "published",
                    "result_id": "published-1",
                    "work_item_id": "item",
                    "repository": "example/repo",
                    "project": "77",
                    "title": "Improve [list]",
                    "source_url": "https://tapd.example.internal/77/prong/stories/view/105",
                    "output_type": "new_pr",
                    "completed_at": "2026-11-02T00:00:00+08:00",
                    "deliveries": [
                        {"type": "open_pr", "url": pr_url},
                        {"type": "submit_pr_review", "url": pr_url + "#pullrequestreview-42"},
                    ],
                },
            ],
        )
        self.assertEqual(self.timeline(zone="UTC")["entries"], [])

    def test_only_delivered_or_local_results_appear_and_repeated_work_is_retained(self):
        self.result("local", output="local_result")
        self.result("classification", output="classification_result")
        self.result("waiting", output="waiting_for_user")
        self.result("failed", lifecycle="failed")
        self.result("unaudited", audited=False)
        self.result("unpublished")
        self.result("partially-published")
        self.result("skipped")
        self.result("first")
        self.result("second")
        self.result("second", attempt=2)
        for task in ("classification", "waiting", "failed", "unaudited", "partially-published", "first", "second"):
            self.action(task, "https://github.com/example/repo/pull/2")
        self.action("partially-published", status="not_published", index=2)
        self.action("skipped", status="skipped")
        self.action("second", "https://github.com/example/repo/pull/3", attempt=2)

        entries = self.timeline()["entries"]
        self.assertEqual([entry["task_id"] for entry in entries], ["second", "local", "first"])
        self.assertEqual(
            entries[0]["deliveries"], [{"type": "open_pr", "url": "https://github.com/example/repo/pull/3"}]
        )
        self.assertEqual(entries[1]["deliveries"], [])

    def test_week_and_month_bounds_use_calendar_days_across_timezone_offset_changes(self):
        self.result("before", output="local_result", when="2026-11-01T03:59:59.999999Z")
        self.result("start", output="local_result", when="2026-11-01T04:00:00.000000Z")
        self.result("same-day", output="local_result", when="2026-11-02T03:59:59Z")
        self.result("last", output="local_result", when="2026-12-01T04:59:59.999999Z")
        self.result("after", output="local_result", when="2026-12-01T05:00:00Z")
        month = self.timeline(view="month", anchor="2026-11-15", zone="America/New_York")
        self.assertEqual(
            (month["start"], month["end"], month["previous"], month["next"]),
            ("2026-11-01", "2026-11-30", "2026-10-31", "2026-12-01"),
        )
        self.assertEqual([entry["task_id"] for entry in month["entries"]], ["last", "same-day", "start"])
        week = self.timeline(view="week", anchor="2026-11-01", zone="America/New_York")
        self.assertEqual((week["start"], week["end"]), ("2026-10-26", "2026-11-01"))
        self.assertEqual([entry["task_id"] for entry in week["entries"]], ["same-day", "start", "before"])

    def test_recorded_completion_survives_later_updates_to_the_task(self):
        for task, output in (("external", "new_pr"), ("local", "local_result")):
            self.result(task, output=output, when="2026-11-10T12:00:00Z")
        self.action("external", "https://github.com/example/repo/pull/2")
        self.conn.execute(
            "UPDATE github_actions SET metadata_json = ? WHERE task_id = 'external'",
            (json.dumps({"publish": {"published_at": "2026-11-01T16:00:00Z"}}),),
        )
        self.conn.execute(
            "INSERT INTO work_item_events(event_id, work_item_id, event_type, actor_kind, actor_identity, "
            "idempotency_key, created_at) VALUES ('event', 'item', 'result_recorded', 'system', 'robert', "
            "'result:local-1', '2026-11-01T16:01:00Z')"
        )
        self.assertEqual([entry["task_id"] for entry in self.timeline()["entries"]], ["local", "external"])
        self.assertEqual(self.timeline(anchor="2026-11-10")["entries"], [])

    def test_follow_up_pr_keeps_the_original_ticket_across_multiple_derived_streams(self):
        self.conn.execute(
            "INSERT INTO workstreams(workstream_id, repo_id, origin_workstream_id, created_at, updated_at) "
            "VALUES ('follow-up', 'repo-1', 'derived', '2026-10-20T00:00:00Z', '2026-10-20T00:00:00Z')"
        )
        self.result("follow-up")
        self.conn.execute("UPDATE tasks SET workstream_id='follow-up' WHERE task_id='follow-up'")
        self.action("follow-up", "https://github.com/example/repo/pull/3")

        entry = self.timeline()["entries"][0]
        self.assertEqual(entry["title"], "Improve [list]")
        self.assertEqual(entry["project"], "77")
        self.assertEqual(entry["source_url"], "https://tapd.example.internal/77/prong/stories/view/105")
        self.assertEqual(entry["work_item_id"], "item")
        self.assertEqual(entry["deliveries"], [{"type": "open_pr", "url": "https://github.com/example/repo/pull/3"}])

    def test_receipt_uses_the_selected_result_and_aggregates_recorded_verification(self):
        self.result("receipt", output="local_result", attempt=2)
        self.conn.execute(
            "UPDATE attempts SET started_at=?, finished_at=?, metadata_json=? WHERE attempt_id='receipt-2'",
            (
                "2026-11-01T15:48:25Z",
                "2026-11-01T15:51:15Z",
                json.dumps({"dispatch": {"worker_name": "codex", "model": "worker-model", "path": "/tmp/private"}}),
            ),
        )
        self.conn.execute(
            "UPDATE worker_results SET handoff=?, verification_json=?, metadata_json=? WHERE result_id='receipt-2'",
            (
                "Full execution handoff.",
                json.dumps([{"status": "passed", "command": "private command"}, {"status": "skipped"}]),
                json.dumps(
                    {
                        "audit": {"status": "accepted"},
                        "normalized_payload": {"receipt_summary": "Completed the request and checked its output."},
                    }
                ),
            ),
        )
        entry = self.timeline()["entries"][0]
        receipt = get_receipt(self.db_path, entry["task_id"], entry["result_id"])
        self.assertEqual(
            {field: value for field, value in receipt.items() if field != "usage"},
            {
                "summary": "Completed the request and checked its output.",
                "summary_redacted": False,
                "summary_truncated": False,
                "worker": "codex",
                "model": "worker-model",
                "attempt": 2,
                "started_at": "2026-11-01T15:48:25+00:00",
                "finished_at": "2026-11-01T15:51:15+00:00",
                "duration_seconds": 170.0,
                "verification": {"passed": 1, "failed": 0, "skipped": 1},
            },
        )

    def test_receipt_rolls_up_usage_for_the_request_and_its_model_runs(self):
        self.result("classification", output="classification_result")
        self.result("receipt", output="local_result", attempt=2)
        self.result("other-request", output="local_result")
        self.conn.execute(
            "UPDATE tasks SET metadata_json=? WHERE task_id IN ('classification', 'receipt')",
            (json.dumps({"supervision_lineage_key": "same-request"}),),
        )
        self.conn.execute(
            "UPDATE attempts SET started_at='2026-10-25T00:00:00Z', metadata_json=? WHERE attempt_id='classification-1'",
            (
                json.dumps(
                    {
                        "usage": {
                            "usage_available": True,
                            "source": "cbc_stream_json",
                            "usage": {
                                "input_tokens": 11,
                                "output_tokens": 4,
                                "cache_read_input_tokens": 6,
                                "cache_creation_input_tokens": 0,
                            },
                            "total_cost_usd": 0.012,
                        }
                    }
                ),
            ),
        )
        self.conn.execute(
            "UPDATE attempts SET started_at='2026-10-27T00:00:00Z', metadata_json=? WHERE attempt_id='other-request-1'",
            (json.dumps({"usage": {"usage_available": True, "usage": {"input_tokens": 900, "output_tokens": 100}}}),),
        )
        self.conn.execute("UPDATE attempts SET started_at='2026-10-26T00:00:00Z' WHERE attempt_id='receipt-2'")
        stdout = self.db_path.parent / "review.stdout"
        stdout.write_text(
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 30,
                        "cached_input_tokens": 8,
                        "cache_write_input_tokens": 5,
                        "output_tokens": 9,
                    },
                }
            )
            + "\n"
        )
        self.conn.execute(
            "INSERT INTO review_sessions(round_id, repo, pr_number, workstream_id, trigger_fingerprint, task_id, "
            "request_json, created_at, updated_at) VALUES ('r1', 'example/repo', 2, 'derived', 'review', 'receipt', "
            "'{}', '2026-10-26T00:00:00Z', '2026-10-26T00:00:00Z')"
        )
        self.conn.execute(
            "INSERT INTO review_runs(run_id, round_id, stage, worker_id, attempt_no, owner_attempt_id, status, "
            "output_dir, prompt_sha256, started_at, dispatch_json, created_at, updated_at) "
            "VALUES ('r1:run', 'r1', 'review', 'reviewer', 1, 'receipt-2', 'succeeded', 'output', 'hash', "
            "'2026-10-27T00:00:00Z', ?, '2026-10-27T00:00:00Z', '2026-10-27T00:00:00Z')",
            (json.dumps({"stdout_path": str(stdout)}),),
        )
        self.conn.execute(
            "INSERT INTO review_runs(run_id, round_id, stage, worker_id, attempt_no, owner_attempt_id, status, "
            "output_dir, prompt_sha256, started_at, created_at, updated_at) "
            "VALUES ('r1:failed', 'r1', 'synthesis', 'reviewer', 1, 'receipt-2', 'failed', 'failed-output', 'hash', "
            "'2026-10-28T00:00:00Z', '2026-10-28T00:00:00Z', '2026-10-28T00:00:00Z')"
        )
        self.conn.commit()

        receipt = get_receipt(self.db_path, "receipt", "receipt-2")
        self.assertEqual(
            receipt["usage"],
            {
                "scope": "request",
                "run_count": 3,
                "token_run_count": 2,
                "billing_token_run_count": 2,
                "input_tokens": 47,
                "uncached_input_tokens": 28,
                "output_tokens": 13,
                "cached_input_tokens": 14,
                "cache_creation_input_tokens": 5,
                "cost_run_count": 1,
                "reported_cost_usd": 0.012,
            },
        )

    def test_receipt_counts_each_persisted_call_once_and_excludes_unrelated_or_later_calls(self):
        from robert_agent.persistence.stores import model_usage

        self.result("receipt", output="local_result")
        self.result("other-request", output="local_result")
        self.conn.execute(
            "UPDATE attempts SET started_at='2026-10-30T00:00:00Z', metadata_json=? WHERE attempt_id='receipt-1'",
            (json.dumps({"usage": {"usage_available": True, "usage": {"input_tokens": 99}}}),),
        )
        measured = {
            "source": "gateway",
            "usage_available": True,
            "total_cost_usd": 0.001,
            "usage": {
                "input_tokens": 5,
                "output_tokens": 2,
                "cache_read_input_tokens": 4,
                "cache_creation_input_tokens": 0,
            },
        }
        self.conn.execute(
            "INSERT INTO proposals(proposal_id, source_identity, repo_id, status, version, revision, source_key, "
            "fingerprint, task_id, created_at, updated_at) VALUES ('p1', 'owned-source', 'repo-1', 'admitted', 1, 1, "
            "'source', 'evidence', 'receipt', '2026-10-28', '2026-10-30')"
        )
        for kind, call, scope, date in (
            ("worker", "receipt-1", "receipt", "2026-10-31"),
            ("worker", "receipt-1", "receipt", "2026-10-31"),
            ("decision", "eligible", "receipt", "2026-10-30"),
            ("diagnosis", "prior", "receipt", "2026-10-29"),
            ("diagnosis", "future", "receipt", "2026-11-01"),
            ("diagnosis", "unrelated", "other-request", "2026-10-29"),
            ("proposer", "same", "repo-1/owned-source", "2026-10-28"),
            ("proposer", "wrong-repo", "repo-2/owned-source", "2026-10-28"),
        ):
            model_usage.record(self.conn, kind, call, scope, date, measured)
        model_usage.record(self.conn, "diagnosis", "missing", "receipt", "2026-10-30", {})
        self.conn.commit()
        usage = get_receipt(self.db_path, "receipt", "receipt-1")["usage"]
        self.assertEqual(usage["run_count"], 5)
        self.assertEqual(usage["billing_token_run_count"], 4)
        self.assertEqual(usage["uncached_input_tokens"], 20)
        self.assertEqual(usage["cached_input_tokens"], 16)
        self.assertEqual(usage["cache_creation_input_tokens"], 0)
        self.assertEqual(usage["output_tokens"], 8)
        self.assertEqual(usage["cost_run_count"], 4)
        self.assertAlmostEqual(usage["reported_cost_usd"], 0.004)

    def test_historical_receipt_has_no_generated_summary(self):
        self.result("receipt")
        self.action("receipt", "https://github.com/example/repo/pull/2")
        self.conn.execute(
            "UPDATE github_actions SET metadata_json=? WHERE action_id='receipt-1-1'",
            (json.dumps({"body": "## Summary\nOriginal PR description."}),),
        )
        self.conn.execute(
            "UPDATE worker_results SET handoff=? WHERE result_id='receipt-1'",
            ("Original detailed handoff.",),
        )
        self.conn.commit()

        receipt = get_receipt(self.db_path, "receipt", "receipt-1")
        self.assertEqual(receipt["summary"], "")
        self.assertFalse(receipt["summary_truncated"])

    def test_receipt_hides_sensitive_summary_and_rejects_undelivered_or_stale_results(self):
        self.result("receipt")
        self.result("receipt", attempt=2)
        self.action("receipt", "https://github.com/example/repo/pull/2")
        self.conn.commit()
        self.assertIsNone(get_receipt(self.db_path, "receipt", "receipt-1"))
        self.assertIsNone(get_receipt(self.db_path, "receipt", "receipt-2"))

        self.action("receipt", "https://github.com/example/repo/pull/3", attempt=2)
        self.conn.execute(
            "UPDATE worker_results SET metadata_json=?, verification_json=? WHERE result_id='receipt-2'",
            (
                json.dumps(
                    {
                        "audit": {"status": "accepted"},
                        "normalized_payload": {"receipt_summary": "api_key=abcd"},
                    }
                ),
                "malformed old verification",
            ),
        )
        self.conn.commit()
        self.assertIsNone(get_receipt(self.db_path, "other-task", "receipt-2"))
        receipt = get_receipt(self.db_path, "receipt", "receipt-2")
        self.assertEqual(receipt["summary"], "")
        self.assertTrue(receipt["summary_redacted"])
        self.assertIsNone(receipt["duration_seconds"])
        self.assertEqual(receipt["verification"], {"passed": 0, "failed": 0, "skipped": 0})
