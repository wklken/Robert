"""Regression contracts for bounded recovery and durable user outcomes."""

import time

from contextlib import closing


import json


import sqlite3


import unittest

from unittest import mock

from tests.support import PACKAGE_ROOT  # noqa: F401


from robert_agent.execution import journal as run_journal


from robert_agent.foundation import values


class ObservabilityTests(unittest.TestCase):
    def test_codex_turn_usage_is_counted(self):
        from robert_agent.readmodels import usage

        parsed = usage.parse_cbc_stream_json(
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                }
            )
        )
        self.assertTrue(parsed["usage_available"])
        self.assertEqual(parsed["usage"]["input_tokens"], 100)

    def test_phase_records_actual_finish_instead_of_cycle_time(self):

        with closing(sqlite3.connect(":memory:")) as c:
            c.execute(
                "CREATE TABLE run_steps(step_id TEXT,run_id TEXT,step_key TEXT,status TEXT,started_at TEXT,finished_at TEXT,output_json TEXT,error_json TEXT)"
            )
            run_journal.insert_run_step(c, "r", "publish")
            with (
                mock.patch.object(
                    values,
                    'utc_now',
                    side_effect=[
                        "2026-10-23T00:00:00+00:00",
                        "2026-10-23T00:01:00+00:00",
                    ],
                ),
                mock.patch.object(time, "monotonic", side_effect=[100, 160]),
            ):
                run_journal.mark_run_step(
                    c, "r", "publish", "running", "2026-10-23T00:00:00+00:00"
                )
                run_journal.mark_run_step(
                    c, "r", "publish", "succeeded", "2026-10-23T00:00:00+00:00"
                )
            row = c.execute("SELECT started_at,finished_at FROM run_steps").fetchone()
            self.assertNotEqual(row[0], row[1])
            self.assertEqual(
                json.loads(
                    c.execute("SELECT output_json FROM run_steps").fetchone()[0]
                )["duration_seconds"],
                60,
            )
