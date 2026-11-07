"""Fixed SQL predicates must keep the indexes used by existing databases."""

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy.dialects.sqlite import dialect

from robert_agent.persistence import statements
from robert_agent.persistence.queries import attempts, notifications, tasks
from robert_agent.persistence.schema.database import init_database


class NotificationQueryPlanTests(unittest.TestCase):
    def test_cached_readiness_uses_current_repository_and_time_parameters(self):
        fixture = Path(__file__).parents[1] / "fixtures/persistence/pre_orm_5e0e952.sql"
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.executescript(fixture.read_text())
            conn.execute(
                "UPDATE attempts SET metadata_json=json_set(metadata_json, '$.resume.not_before_at', ?)",
                ("2026-11-08T00:00:00+00:00",),
            )
            for repos, now, expected in (
                (["repo:example/backend"], "2026-11-09T00:00:00+00:00", True),
                (["missing"], "2026-11-09T00:00:00+00:00", False),
                (["repo:example/backend"], "2026-11-07T00:00:00+00:00", False),
                ([], "2026-11-09T00:00:00+00:00", True),
                ([], "2026-11-07T00:00:00+00:00", False),
            ):
                with self.subTest(repos=repos, now=now):
                    self.assertEqual(bool(attempts.has_local_work_filtered_2(conn, repos, now)), expected)

    def test_repeated_lifecycle_reads_reuse_templates_without_caching_values(self):
        fixture = Path(__file__).parents[1] / "fixtures/persistence/pre_orm_5e0e952.sql"
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.executescript(fixture.read_text())
            stream = "github:example/backend#123"
            captured = []
            original = statements.read

            def inspect(connection, statement, parameters=()):
                captured.append(statement)
                return original(connection, statement, parameters)

            with patch.object(statements, "read", side_effect=inspect):
                self.assertEqual(len(tasks.current_task_filtered(conn, [stream])), 1)
                self.assertEqual(tasks.current_task_filtered(conn, ["missing"]), [])
                self.assertEqual(tasks.current_task_filtered(conn, []), [])
                conn.execute("UPDATE tasks SET lifecycle='waiting_for_user'")
                rows = tasks.current_task_filtered(conn, ["missing", stream])
                self.assertEqual(rows[0]["lifecycle"], "waiting_for_user")
            # Template construction is the measured hot path, so repeated calls
            # must reuse the expression while binding current values each time.
            self.assertTrue(all(statement is captured[0] for statement in captured))

    def test_fixed_json_predicates_keep_pending_and_state_indexes(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "db.sqlite3"
            init_database(db)
            with closing(sqlite3.connect(db)) as conn:
                plans = []
                original = statements.read

                def inspect(connection, statement, parameters=()):
                    compiled = statement.compile(dialect=dialect(paramstyle="qmark"))
                    state = compiled.construct_expanded_state(dict(parameters))
                    plans.append(
                        " ".join(
                            row[3]
                            for row in conn.execute(
                                "EXPLAIN QUERY PLAN " + state.statement,
                                state.positional_parameters,
                            )
                        )
                    )
                    return original(connection, statement, parameters)

                with patch.object(statements, "read", side_effect=inspect):
                    notifications.find_for_deliver_pending(conn, "2026-11-07T00:00:00+00:00", 10)
                    notifications.has_pending_local_delivery_filtered(conn, ["unknown"], "2026-11-07T00:00:00+00:00")
                    notifications.find_for_collect(conn)
                self.assertIn("idx_notifications_delivery_pending", plans[0])
                self.assertIn("idx_notifications_delivery_pending", plans[1])
                self.assertIn("idx_notifications_delivery_state", plans[2])
