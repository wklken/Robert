"""Regression contracts for bounded recovery and durable user outcomes."""

import unittest


from tests.support import PACKAGE_ROOT  # noqa: F401


class DurableResultFixture(unittest.TestCase):
    def setUp(self):
        from tests.support.worker_protocol import WorkerProtocolFixture

        self.fixture = WorkerProtocolFixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db = self.fixture._init_attempt_db(
            output_type="local_result", route_id="local-result"
        )
        from robert_agent.delivery.application.submission import build_result

        self.payload = build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="local_result",
            planned_github_actions=[],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="finished work",
            used_skills=[],
        )
