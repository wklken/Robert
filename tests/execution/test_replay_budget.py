"""Regression contracts for bounded recovery and durable user outcomes."""

from tests.support.durable_result import DurableResultFixture


from contextlib import closing


import sqlite3


from tests.support import PACKAGE_ROOT  # noqa: F401


class ReplayBudgetTests(DurableResultFixture):
    def test_busy_replay_stops_after_first_short_wait(self):
        import time
        from robert_agent.execution import spool as result_spool
        from robert_agent.delivery import contract as result_contract
        from robert_agent.delivery.application.submission import replay_pending_results

        for number in range(2):
            envelope = {**self.payload, "attempt_id": f"pending-{number}"}
            digest = result_contract.semantic_fingerprint(envelope)
            payload = {key: value for key, value in envelope.items() if key != "submission"} | envelope["submission"]
            payload.update(
                _result_contract=result_contract.CONTRACT_ID,
                _normalizer_version=1,
                _submission_fingerprint=digest,
            )
            with result_spool.locked_record(self.db, payload["attempt_id"]) as path:
                result_spool.atomic_write(
                    path,
                    {
                        "status": "pending",
                        "payload": payload,
                        "fingerprint": digest,
                        "result_contract": result_contract.CONTRACT_ID,
                        "normalizer_version": 1,
                    },
                )
        with closing(sqlite3.connect(self.db)) as lock:
            lock.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            result = replay_pending_results(self.db)
            elapsed = time.monotonic() - started
            lock.rollback()
        self.assertLess(elapsed, 1.5)
        self.assertEqual(result["deferred"], 1)
