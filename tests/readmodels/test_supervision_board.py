from tests.support import board as fixtures
import unittest
from robert_agent.readmodels import board
from robert_agent.supervision import store as supervision_store
import sqlite3


class SupervisionBoardTests(unittest.TestCase):
    setUp = fixtures.BoardReadModelFixture.setUp
    _create = fixtures.BoardReadModelFixture._create

    def test_incident_is_visible_on_affected_item_only(self):
        first = self._create("supervised", start=True)
        second = self._create("unrelated", start=True)
        task = board.get_work_item_detail(self.db_path, first["work_item_id"])["tasks"][
            0
        ]["task_id"]
        with sqlite3.connect(self.db_path) as conn:
            supervision_store.apply_observations(
                conn,
                [
                    {
                        "scope_type": "task",
                        "scope_id": task,
                        "rule_id": "worker_no_progress",
                        "classification": "confirmed",
                        "reason_code": "no_progress",
                        "severity": "warning",
                        "evidence": {},
                        "expected_progress": {"milestone": "accepted result"},
                    }
                ],
                "2026-11-06T10:00:00+00:00",
            )
        detail = board.get_work_item_detail(self.db_path, first["work_item_id"])
        self.assertEqual(
            detail["supervision_incidents"][0]["reason_code"], "no_progress"
        )
        self.assertEqual(
            board.get_work_item_detail(self.db_path, second["work_item_id"])[
                "supervision_incidents"
            ],
            [],
        )
