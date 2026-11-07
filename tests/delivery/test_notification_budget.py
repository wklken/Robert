"""Regression contracts for bounded recovery and durable user outcomes."""

from contextlib import closing


import json


import sqlite3


from types import SimpleNamespace

import unittest

from unittest import mock

from tests.support import PACKAGE_ROOT  # noqa: F401


class DeliveryBudgetTests(unittest.TestCase):
    def test_operator_delivery_is_persisted_and_not_repeated(self):
        from tests.support.worker_protocol import WorkerProtocolFixture
        from robert_agent.delivery.application import notifications as notify

        fixture = WorkerProtocolFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        db = fixture._init_attempt_db()
        notify.record_notification(
            db,
            "worker_failed",
            "recorded",
            {"task_id": "task-1", "reason_code": "authentication_required"},
        )
        runner = mock.Mock(return_value=SimpleNamespace(returncode=0))
        notify.deliver_pending(db, command=["test-notifier"], runner=runner)
        notify.deliver_pending(db, command=["test-notifier"], runner=runner)
        self.assertEqual(runner.call_count, 1)
        with closing(sqlite3.connect(db)) as c:
            metadata = json.loads(
                c.execute("SELECT metadata_json FROM notifications").fetchone()[0]
            )
            self.assertEqual(metadata["delivery"]["status"], "sent")
