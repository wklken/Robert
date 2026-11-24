from contextlib import closing
import sqlite3
import json
from unittest.mock import patch

from sqlalchemy import event
from sqlalchemy.engine import Engine

from robert_agent.readmodels import board
from robert_agent.persistence import database, transactions
from robert_agent.persistence.queries.readviews import work_items
from robert_agent.work import control_queries
from tests.support.board import BoardReadModelFixture


class BoardBatchingTests(BoardReadModelFixture):
    def seed_cards(self, start, stop):
        now = "2026-07-19T02:00:00+00:00"
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            for index in range(start, stop):
                stream, task, item = (f"{kind}-{index:03}" for kind in ("stream", "task", "item"))
                conn.execute(
                    "INSERT INTO workstreams(workstream_id,repo_id,lifecycle,active_task_id,created_at,updated_at)"
                    " VALUES (?, 'repo-1', 'active', ?, ?, ?)",
                    (stream, task, now, now),
                )
                conn.execute(
                    "INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at)"
                    " VALUES (?, ?, 'queued', ?, ?)",
                    (task, stream, now, now),
                )
                conn.execute(
                    "INSERT INTO attempts(attempt_id,task_id,attempt_no,status,branch_name)"
                    " VALUES (?, ?, 1, 'failed', ?)",
                    (f"attempt-{index:03}", task, f"codex/card-{index:03}"),
                )
                conn.execute(
                    "INSERT INTO work_items(work_item_id,repo_id,title,origin_type,workstream_id,"
                    "creation_idempotency_key,created_by,activated_at,created_at,updated_at)"
                    " VALUES (?, 'repo-1', ?, 'web', ?, ?, 'owner', ?, ?, ?)",
                    (item, item, stream, f"create-{item}", now, now, now),
                )

    def measured_board(self, **kwargs):
        executed = []

        def capture(_conn, _cursor, sql, _params, _context, _many):
            executed.append(sql)

        event.listen(Engine, "before_cursor_execute", capture)
        try:
            result = board.list_board(self.db_path, **kwargs)
        finally:
            event.remove(Engine, "before_cursor_execute", capture)
        return result, len(executed)

    def test_fifty_cards_use_bounded_queries_and_preserve_pagination_and_counts(self):
        self.seed_cards(0, 5)
        small, small_count = self.measured_board()
        self.seed_cards(5, 50)
        first, count = self.measured_board(limit=25)
        second, second_count = self.measured_board(limit=25, cursor=first["next_cursor"])
        self.assertEqual(small["counts"]["todo"], 5)
        self.assertEqual(first["counts"]["todo"], 50)
        self.assertEqual(second["counts"], first["counts"])
        self.assertEqual(
            [card["work_item_id"] for card in first["items"] + second["items"]],
            [f"item-{index:03}" for index in reversed(range(50))],
        )
        self.assertIsNone(second["next_cursor"])
        self.assertEqual(first["items"][0]["status_summary"]["attempts"], {"total": 1, "failed": 1})
        self.assertEqual(first["items"][0]["branch"], "codex/card-049")
        self.assertLessEqual(count, 100, f"5 cards: {small_count}; 50 cards: {count}")
        self.assertLessEqual(second_count, 100)
        self.assertLessEqual(count, small_count + 5)

    def assert_snapshot_equivalence(self):
        with closing(database.connect(self.db_path)) as conn:
            transactions.begin(conn)
            rows = work_items.work_items_with_repositories(conn)
            expected = [board._project_item(conn, row) for row in rows]
            self.assertEqual(board._batch_projections(conn, rows), expected)

    def test_metadata_and_supplied_controls_match_single_item_snapshot(self):
        self.seed_cards(0, 1)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            for index, metadata in enumerate(("", "{}", "[]", "null", "1", "bad-json")):
                conn.execute(
                    "INSERT INTO tasks(task_id,workstream_id,lifecycle,metadata_json,created_at,updated_at)"
                    " VALUES (?, 'stream-000', 'completed', ?, '2020', '2020')",
                    (f"old-{index}", metadata),
                )
            conn.execute(
                "UPDATE tasks SET metadata_json=? WHERE task_id='task-000'",
                (json.dumps({"remediation_episode_id": "episode-1"}),),
            )
        self.assert_snapshot_equivalence()
        # The current schema forbids NULL task metadata; the shared policy
        # still accepts it when reading historical records supplied by callers.
        facts = control_queries.lifecycle_facts_from_records(
            streams=[],
            attempts=[],
            actions=[],
            task_metadata=[None, ""],
            subjects=[],
            controls={},
            source_state=None,
            publication_retry_pending=False,
            approval_task_id=None,
            pull_requests=[],
        )
        self.assertEqual(facts["workflow_attention"], [])
        with closing(database.connect(self.db_path)) as conn:
            transactions.begin(conn)
            rows = work_items.work_items_with_repositories(conn)
            for controls in (
                {},
                {("task", "task-000"): {"binding_status": "attention", "metadata_json": '{"reason_code":"override"}'}},
                {("remediation_episode", "episode-1"): {"binding_status": "attention", "metadata_json": "{}"}},
                {("task", "unrelated"): {"binding_status": "attention", "metadata_json": "{}"}},
            ):
                with self.subTest(controls=controls):
                    expected = [board._project_item(conn, row, controls=controls) for row in rows]
                    self.assertEqual(board._batch_projections(conn, rows, controls=controls), expected)

    def test_more_than_two_hundred_related_streams_keep_publication_order_and_current_task(self):
        self.seed_cards(0, 1)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            for index in range(205):
                stream, task = f"related-{index:03}", f"child-{index:03}"
                conn.execute(
                    "INSERT INTO workstreams(workstream_id,repo_id,origin_workstream_id,lifecycle,active_task_id,created_at,updated_at)"
                    " VALUES (?, 'repo-1', 'stream-000', 'active', ?, '2020', '2020')",
                    (stream, task),
                )
                conn.execute(
                    "INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at)"
                    " VALUES (?, ?, 'completed', '2020', '2020')",
                    (task, stream),
                )
                conn.execute(
                    "INSERT INTO github_actions(action_id,task_id,action_type,audit_status,publish_status,created_at,metadata_json)"
                    " VALUES (?, ?, 'comment', 'accepted', 'not_published', ?, ?)",
                    (
                        f"action-{index:03}",
                        task,
                        f"2020-{205 - index:03}",
                        json.dumps({"publish": {"status": "publish_failed", "safe_error": f"failure-{index}"}}),
                    ),
                )
            conn.execute(
                "INSERT INTO attempts(attempt_id,task_id,attempt_no,status,metadata_json)"
                " VALUES ('running-child', 'child-003', 1, 'running', '{}')"
            )
        self.assert_snapshot_equivalence()
        card = board.list_board(self.db_path)["items"][0]
        self.assertEqual(card["task_id"], "child-003")
        self.assertEqual(card["publication"]["pending"], 205)
        self.assertEqual(card["publication"]["failures"][0]["action_id"], "action-204")

    def test_large_supplied_control_snapshot_keeps_queries_bounded(self):
        self.seed_cards(0, 1)
        with closing(database.connect(self.db_path)) as conn:
            transactions.begin(conn)
            rows = work_items.work_items_with_repositories(conn)
            controls = {
                (kind, f"unrelated-{index}"): {"binding_status": "attention", "metadata_json": "{}"}
                for kind in ("task", "remediation_episode")
                for index in range(1100)
            }
            controls[("task", "task-000")] = {
                "binding_status": "attention",
                "metadata_json": '{"reason_code":"matching-control"}',
            }
            expected = [board._project_item(conn, row, controls=controls) for row in rows]
            self.assertEqual(board._batch_projections(conn, rows, controls=controls), expected)

    def test_recovery_commands_keep_persisted_binding_with_incomplete_controls(self):
        self.seed_cards(0, 1)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE tasks SET lifecycle='failed' WHERE task_id='task-000'")
            conn.execute("UPDATE work_items SET origin_type='github' WHERE work_item_id='item-000'")
            conn.execute(
                "INSERT INTO execution_subjects(subject_type,subject_id,workflow_kind,binding_status,"
                "created_at,updated_at,metadata_json) VALUES ('task','task-000','human_request',"
                "'attention','2020','2020','{}')"
            )
        with closing(database.connect(self.db_path)) as conn:
            transactions.begin(conn)
            rows = work_items.work_items_with_repositories(conn)
            for controls in (None, {}, {("task", "task-000"): {"binding_status": "terminal", "metadata_json": "{}"}}):
                with self.subTest(controls=controls):
                    expected = [board._project_item(conn, row, controls=controls) for row in rows]
                    self.assertEqual(expected[0]["valid_commands"], ["cancel"])
                    self.assertEqual(board._batch_projections(conn, rows, controls=controls), expected)

    def test_full_board_json_matches_single_item_path_in_one_read_snapshot(self):
        self._seed_six_columns()
        with closing(database.connect(self.db_path)) as conn:
            transactions.begin(conn)

            # Keep both public requests on the same physical read snapshot.
            with (
                patch.object(board, "_connect", return_value=conn),
                patch.object(conn, "close"),
                patch.object(board.db_transactions, "begin"),
            ):
                expected = board.list_board(self.db_path, include_history=True, limit=4)

                def individual(reader, rows, **kwargs):
                    return [board._project_item(reader, row, controls=kwargs.get("controls")) for row in rows]

                with patch.object(board, "_batch_projections", side_effect=individual):
                    self.assertEqual(board.list_board(self.db_path, include_history=True, limit=4), expected)

    def test_native_round_status_and_pool_match_single_item_snapshot(self):
        self.seed_cards(0, 50)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE review_process_pool SET max_concurrency=4 WHERE singleton=1")
            for index in range(50):
                task = f"task-{index:03}"
                for kind in ("review", "vote", "pre_pr_review"):
                    round_id = f"{kind}-{index:03}"
                    if kind == "pre_pr_review":
                        conn.execute(
                            "INSERT INTO pre_pr_review_sessions(round_id,task_id,status,request_json,proof_json,"
                            "snapshot_id,created_at,updated_at) VALUES (?,?,'reviewing','{}',?,?, '2020','2020')",
                            (round_id, task, '{"head_sha":"final-head"}', f"snapshot-{index}"),
                        )
                        conn.execute(
                            "INSERT INTO pre_pr_review_snapshots(snapshot_id,round_id,head_sha,base_sha,manifest_json,created_at)"
                            " VALUES (?,?,'head','base','{\"head_sha\":\"snapshot-head\"}','2020')",
                            (f"snapshot-{index}", round_id),
                        )
                    else:
                        columns = "pr_number" if kind == "review" else "number,source_type"
                        values = "12" if kind == "review" else "12,'issue'"
                        conn.execute(
                            f"INSERT INTO {kind}_sessions(round_id,repo,{columns},workstream_id,trigger_fingerprint,"
                            "task_id,status,request_json,manifest_json,error,created_at,updated_at)"
                            f" VALUES (?,'example/repo',{values},?,? ,?,'reviewing','{{}}',?,?,'2020','2020')",
                            (
                                round_id,
                                f"stream-{index:03}",
                                round_id,
                                task,
                                '{"head_sha":"session-head","base_ref":"main"}',
                                "public error",
                            ),
                        )
                    stages = (
                        ("implement", "review")
                        if kind == "pre_pr_review"
                        else (("prepare", "review") if kind == "review" else ("prepare", "vote"))
                    )
                    for run_index, stage in enumerate(stages):
                        conn.execute(
                            f"INSERT INTO {kind}_runs(run_id,round_id,stage,worker_id,attempt_no,owner_attempt_id,"
                            "status,output_dir,prompt_sha256,slot_reserved,dispatch_json,created_at,updated_at)"
                            " VALUES (?,?,?,'worker',?,?, 'starting',?,'hash',1,?,?,'2020')",
                            (
                                f"run-{round_id}-{run_index}",
                                round_id,
                                stage,
                                2 - run_index,
                                f"attempt-{index:03}",
                                f"/private/{round_id}/{run_index}",
                                "{}" if run_index == 0 else '{"pid":123}',
                                f"2020-{run_index}",
                            ),
                        )
        self.assert_snapshot_equivalence()
        page, count = self.measured_board()
        self.assertLessEqual(count, 100)
        card = page["items"][0]
        self.assertEqual(card["pre_pr_review"]["head_sha"], "snapshot-head")
        self.assertEqual(card["pre_pr_review"]["final_sha"], "final-head")
        self.assertEqual(card["review"]["process_pool"], {"limit": 4, "in_use": 250})
        self.assertTrue(card["review"]["runs"][0]["can_abandon"])
        self.assertFalse(card["review"]["runs"][1]["can_abandon"])
        self.assertNotIn("/private/", json.dumps(page))
