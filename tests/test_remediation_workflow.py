import unittest
from unittest import mock


def _transition(transition_id, operation, *, state=None, wait_seconds=None, terminal=False):
    return {
        "transition_id": transition_id,
        "operation": operation,
        "wait_seconds": wait_seconds,
        "terminal": terminal,
        "state": state or {},
    }


class FakeRemediationDriver:
    def __init__(self, transitions, *, apply_status="applied", consume_status="consumed"):
        self.transitions = list(transitions)
        self.apply_status = apply_status
        self.consume_status = consume_status
        self.calls = []

    def next_transition(self, episode_id):
        self.calls.append(("next", episode_id))
        return self.transitions.pop(0)

    def apply_transition(self, episode_id, transition):
        self.calls.append(("apply", episode_id, transition["operation"], transition["transition_id"]))
        return {
            "applied_transition_id": transition["transition_id"],
            "status": self.apply_status,
            "state": {},
        }

    def consume_signal(self, episode_id, message):
        self.calls.append(("consume", episode_id, message["signal_id"]))
        return {
            "signal_id": message["signal_id"],
            "status": self.consume_status,
            "state": {},
        }

    def mark_attention(self, episode_id, reason_code):
        self.calls.append(("attention", episode_id, reason_code))
        return {"state": {"reason_code": reason_code}}


class RemediationWorkflowTests(unittest.TestCase):
    def setUp(self):
        from robert_agent import workflow_operations

        workflow_operations.reset_remediation_driver()
        self.addCleanup(workflow_operations.reset_remediation_driver)

    def _run(self, driver, *, recv=None):
        from robert_agent import durable_workflows, workflow_operations

        workflow_operations.configure_remediation_driver(driver)
        with mock.patch.object(durable_workflows.DBOS, "recv", side_effect=recv):
            return durable_workflows.pr_remediation_workflow_v1.__wrapped__("episode-1")

    def _repair(self, *, terminal_status="resolved", action_count=1):
        transitions = [
            _transition(
                "snapshot-1",
                "validate_snapshot",
                state={
                    "snapshot_status": "current",
                    "observed_head_sha": "head-1",
                    "observed_base_sha": "base-1",
                },
            ),
            _transition(
                "budget-1",
                "enforce_budget",
                state={"budget_ok": True, "usage_available": True, "reason_code": "ok"},
            ),
            _transition("prepare-1", "prepare"),
            _transition("dispatch-1", "dispatch"),
            _transition("supervise-1", "supervise"),
            _transition("audit-1", "audit", state={"action_count": action_count}),
        ]
        if action_count:
            transitions.extend(
                [
                    _transition("publish-1", "publish"),
                    _transition(
                        "health-1",
                        "wait_health",
                        state={
                            "result_head_sha": "head-2",
                            "health_baseline_id": "health-1",
                        },
                    ),
                    _transition("wait-1", "wait", wait_seconds=1),
                    _transition("resolve-1", "resolve"),
                ]
            )
        transitions.append(
            _transition(
                "terminal-1",
                "terminal",
                state={"status": terminal_status},
                terminal=True,
            )
        )
        return transitions

    def test_ci_repair_requires_fresh_health_signal_before_resolved(self):
        message = {
            "signal_id": "signal-1",
            "topic": "subject-events",
            "subject_type": "remediation_episode",
            "subject_id": "episode-1",
            "fact_id": "health-1",
        }
        driver = FakeRemediationDriver(self._repair())

        result = self._run(driver, recv=[message])

        self.assertEqual(result["status"], "resolved")
        self.assertEqual(
            [call[2] for call in driver.calls if call[0] == "apply"],
            [
                "validate_snapshot",
                "enforce_budget",
                "prepare",
                "dispatch",
                "supervise",
                "audit",
                "publish",
                "wait_health",
                "resolve",
            ],
        )
        self.assertEqual([call[0] for call in driver.calls].count("consume"), 1)

    def test_confirmed_push_without_fresh_health_cannot_resolve(self):
        driver = FakeRemediationDriver(self._repair())
        result = self._run(driver, recv=[None])

        self.assertEqual(result["status"], "attention")
        self.assertEqual(result["reason_code"], "transition_out_of_order")
        self.assertNotIn("resolve", [call[2] for call in driver.calls if call[0] == "apply"])

    def test_stale_snapshot_supersedes_without_dispatch(self):
        driver = FakeRemediationDriver(
            [
                _transition(
                    "snapshot-1",
                    "validate_snapshot",
                    state={
                        "snapshot_status": "superseded",
                        "observed_head_sha": "head-1",
                        "observed_base_sha": "base-1",
                    },
                ),
                _transition("supersede-1", "supersede"),
                _transition(
                    "terminal-1",
                    "terminal",
                    state={"status": "superseded"},
                    terminal=True,
                ),
            ]
        )

        result = self._run(driver)

        self.assertEqual(result["status"], "superseded")
        self.assertNotIn("dispatch", [call[2] for call in driver.calls if call[0] == "apply"])

    def test_budget_false_exhausts_without_dispatch(self):
        driver = FakeRemediationDriver(
            [
                _transition(
                    "snapshot-1",
                    "validate_snapshot",
                    state={
                        "snapshot_status": "current",
                        "observed_head_sha": "head-1",
                        "observed_base_sha": "base-1",
                    },
                ),
                _transition(
                    "budget-1",
                    "enforce_budget",
                    state={
                        "budget_ok": False,
                        "usage_available": False,
                        "reason_code": "missing_usage",
                    },
                ),
                _transition("exhaust-1", "exhaust"),
                _transition(
                    "terminal-1",
                    "terminal",
                    state={"status": "exhausted"},
                    terminal=True,
                ),
            ]
        )

        result = self._run(driver)

        self.assertEqual(result["status"], "exhausted")
        self.assertNotIn("dispatch", [call[2] for call in driver.calls if call[0] == "apply"])

    def test_each_budget_limit_exhausts_without_dispatch(self):
        for reason in (
            "max_attempts",
            "max_wall_minutes",
            "max_total_tokens",
            "max_cost_usd",
        ):
            with self.subTest(reason=reason):
                from robert_agent import workflow_operations

                workflow_operations.reset_remediation_driver()
                driver = FakeRemediationDriver(
                    [
                        _transition(
                            "snapshot-1",
                            "validate_snapshot",
                            state={
                                "snapshot_status": "current",
                                "observed_head_sha": "head-1",
                                "observed_base_sha": "base-1",
                            },
                        ),
                        _transition(
                            "budget-1",
                            "enforce_budget",
                            state={
                                "budget_ok": False,
                                "usage_available": True,
                                "reason_code": reason,
                            },
                        ),
                        _transition("exhaust-1", "exhaust"),
                        _transition(
                            "terminal-1",
                            "terminal",
                            state={"status": "exhausted"},
                            terminal=True,
                        ),
                    ]
                )
                result = self._run(driver)
                self.assertEqual(result["status"], "exhausted")
                self.assertNotIn(
                    "dispatch", [call[2] for call in driver.calls if call[0] == "apply"]
                )

    def test_still_failing_ci_exhausts_after_fresh_health(self):
        message = {
            "signal_id": "signal-failing",
            "topic": "subject-events",
            "subject_type": "remediation_episode",
            "subject_id": "episode-1",
            "fact_id": "health-failing",
        }
        transitions = self._repair()[:-4] + [
            _transition(
                "health-1",
                "wait_health",
                state={
                    "result_head_sha": "head-2",
                    "health_baseline_id": "health-1",
                },
            ),
            _transition("wait-1", "wait", wait_seconds=1),
            _transition("exhaust-1", "exhaust"),
            _transition(
                "terminal-1",
                "terminal",
                state={"status": "exhausted"},
                terminal=True,
            ),
        ]
        driver = FakeRemediationDriver(transitions)

        result = self._run(driver, recv=[message])

        self.assertEqual(result["status"], "exhausted")
        self.assertEqual(
            [call[2] for call in driver.calls if call[0] == "apply"].count("publish"),
            1,
        )

    def test_conflict_repair_resolves_only_after_fresh_mergeability(self):
        message = {
            "signal_id": "mergeability-1",
            "topic": "subject-events",
            "subject_type": "remediation_episode",
            "subject_id": "episode-1",
            "fact_id": "mergeability-fresh",
        }
        transitions = self._repair()
        transitions[0]["state"]["observed_head_sha"] = "conflict-head"
        transitions[0]["state"]["observed_base_sha"] = "conflict-base"
        transitions[7]["state"]["result_head_sha"] = "conflict-head-fixed"
        transitions[7]["state"]["health_baseline_id"] = "mergeability-baseline"
        driver = FakeRemediationDriver(transitions)

        result = self._run(driver, recv=[message])

        self.assertEqual(result["status"], "resolved")

    def test_missing_failure_evidence_exhausts_without_dispatch(self):
        driver = FakeRemediationDriver(
            [
                _transition(
                    "snapshot-1",
                    "validate_snapshot",
                    state={
                        "snapshot_status": "current",
                        "observed_head_sha": "head-1",
                        "observed_base_sha": "base-1",
                    },
                ),
                _transition(
                    "budget-1",
                    "enforce_budget",
                    state={
                        "budget_ok": False,
                        "usage_available": False,
                        "reason_code": "missing_failure_evidence",
                    },
                ),
                _transition("exhaust-1", "exhaust"),
                _transition(
                    "terminal-1",
                    "terminal",
                    state={"status": "exhausted"},
                    terminal=True,
                ),
            ]
        )

        result = self._run(driver)

        self.assertEqual(result["status"], "exhausted")
        self.assertNotIn("dispatch", [call[2] for call in driver.calls if call[0] == "apply"])

    def test_pending_human_context_does_not_change_episode_order(self):
        transitions = self._repair()
        transitions[0]["state"]["human_pending"] = True
        driver = FakeRemediationDriver(transitions)
        message = {
            "signal_id": "signal-human-context",
            "topic": "subject-events",
            "subject_type": "remediation_episode",
            "subject_id": "episode-1",
            "fact_id": "health-1",
        }

        result = self._run(driver, recv=[message])

        self.assertEqual(result["status"], "resolved")
        self.assertEqual(
            [call[2] for call in driver.calls if call[0] == "apply"],
            [
                "validate_snapshot",
                "enforce_budget",
                "prepare",
                "dispatch",
                "supervise",
                "audit",
                "publish",
                "wait_health",
                "resolve",
            ],
        )

    def test_driver_attention_ends_without_controller_attention_retry(self):
        driver = FakeRemediationDriver(
            [
                _transition(
                    "snapshot-1",
                    "validate_snapshot",
                    state={
                        "snapshot_status": "current",
                        "observed_head_sha": "head-1",
                        "observed_base_sha": "base-1",
                    },
                )
            ],
            apply_status="attention",
        )

        result = self._run(driver)

        self.assertEqual(result["reason_code"], "driver_attention")
        self.assertEqual([call for call in driver.calls if call[0] == "attention"], [])

    def test_duplicate_signal_does_not_establish_fresh_health(self):
        message = {
            "signal_id": "signal-1",
            "topic": "subject-events",
            "subject_type": "remediation_episode",
            "subject_id": "episode-1",
            "fact_id": "health-1",
        }
        driver = FakeRemediationDriver(self._repair(), consume_status="duplicate")

        result = self._run(driver, recv=[message])

        self.assertEqual(result["status"], "attention")
        self.assertEqual(result["reason_code"], "transition_out_of_order")

    def test_repeated_actionable_transition_marks_attention_once(self):
        transition = _transition(
            "snapshot-1",
            "validate_snapshot",
            state={
                "snapshot_status": "current",
                "observed_head_sha": "head-1",
                "observed_base_sha": "base-1",
            },
        )
        driver = FakeRemediationDriver([transition, dict(transition)])

        result = self._run(driver)

        self.assertEqual(result["reason_code"], "transition_not_progressed")
        self.assertEqual(
            [call[2] for call in driver.calls if call[0] == "attention"],
            ["transition_not_progressed"],
        )

    def test_invalid_signal_marks_attention_without_apply(self):
        driver = FakeRemediationDriver(self._repair())

        result = self._run(driver, recv=[{"signal_id": "bad"}])

        self.assertEqual(result["reason_code"], "signal_invalid")
        self.assertNotIn(
            "resolve", [call[2] for call in driver.calls if call[0] == "apply"]
        )

    def test_missing_driver_and_conflicting_registration(self):
        from robert_agent import durable_workflows, workflow_operations

        with self.assertRaisesRegex(RuntimeError, "RemediationDriver"):
            durable_workflows.pr_remediation_workflow_v1.__wrapped__("episode-1")
        first = FakeRemediationDriver([])
        second = FakeRemediationDriver([])
        self.assertIs(workflow_operations.configure_remediation_driver(first), first)
        self.assertIs(workflow_operations.configure_remediation_driver(first), first)
        with self.assertRaisesRegex(ValueError, "conflicting"):
            workflow_operations.configure_remediation_driver(second)


class RemediationTransitionValidationTests(unittest.TestCase):
    def test_exact_payload_and_required_state_validation(self):
        from robert_agent import workflow_operations

        valid = _transition(
            "snapshot-1",
            "validate_snapshot",
            state={
                "snapshot_status": "current",
                "observed_head_sha": "head-1",
                "observed_base_sha": "base-1",
            },
        )
        self.assertEqual(
            workflow_operations.validate_remediation_transition(valid),
            valid,
        )
        for state in (
            {"snapshot_status": "current"},
            {"snapshot_status": "unknown", "failure_logs": "secret"},
        ):
            with self.subTest(state=state), self.assertRaises(ValueError):
                workflow_operations.validate_remediation_transition(
                    _transition("bad", "validate_snapshot", state=state)
                )

    def test_terminal_statuses_and_signal_subject_are_strict(self):
        from robert_agent import workflow_operations

        with self.assertRaises(ValueError):
            workflow_operations.validate_remediation_transition(
                _transition(
                    "terminal-1",
                    "terminal",
                    state={"status": "unknown"},
                    terminal=True,
                )
            )
        with self.assertRaises(ValueError):
            workflow_operations.validate_remediation_signal(
                "episode-1",
                {
                    "signal_id": "s",
                    "topic": "subject-events",
                    "subject_type": "task",
                    "subject_id": "episode-1",
                    "fact_id": "f",
                },
            )


if __name__ == "__main__":
    unittest.main()
