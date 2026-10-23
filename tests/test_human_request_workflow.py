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


class FakeHumanRequestDriver:
    def __init__(self, transitions, apply_status="applied", consume_status="consumed"):
        self.transitions = list(transitions)
        self.apply_status = apply_status
        self.consume_status = consume_status
        self.calls = []

    def next_transition(self, task_id):
        self.calls.append(("next", task_id))
        return self.transitions.pop(0)

    def apply_transition(self, task_id, transition):
        self.calls.append(("apply", task_id, transition["operation"], transition["transition_id"]))
        return {
            "applied_transition_id": transition["transition_id"],
            "status": self.apply_status,
            "state": {},
        }

    def consume_signal(self, task_id, message):
        self.calls.append(("consume", task_id, message["signal_id"]))
        return {
            "signal_id": message["signal_id"],
            "status": self.consume_status,
            "state": {},
        }

    def mark_attention(self, task_id, reason_code):
        self.calls.append(("attention", task_id, reason_code))
        return {"state": {"reason_code": reason_code}}


class HumanRequestWorkflowTests(unittest.TestCase):
    def setUp(self):
        from robert_agent import workflow_operations

        workflow_operations.reset_human_request_driver()
        self.addCleanup(workflow_operations.reset_human_request_driver)

    def _run(self, driver, *, recv=None):
        from robert_agent import durable_workflows, workflow_operations

        workflow_operations.configure_human_request_driver(driver)
        with mock.patch.object(durable_workflows.DBOS, "recv", side_effect=recv):
            return durable_workflows.human_request_workflow_v1.__wrapped__("task-1")

    def _promised_actions(self, terminal_status=None):
        transitions = [
            _transition("prepare-1", "prepare"),
            _transition("dispatch-1", "dispatch"),
            _transition("supervise-1", "supervise"),
            _transition("audit-1", "audit", state={"action_count": 1}),
            _transition("publish-1", "publish", state={"promised_actions": ["comment"]}),
            _transition("finalize-1", "finalize"),
        ]
        transitions.append(
            _transition(
                "terminal-1",
                "terminal",
                state={"status": terminal_status} if terminal_status else {},
                terminal=True,
            )
        )
        return transitions

    def test_missing_driver_configuration_fails_from_next_transition_step(self):
        from robert_agent import durable_workflows, workflow_operations

        workflow_operations.reset_human_request_driver()
        with self.assertRaisesRegex(RuntimeError, "HumanRequestDriver"):
            durable_workflows.human_request_workflow_v1.__wrapped__("task-1")

    def test_comment_only_runs_each_action_once_and_finishes_after_publish(self):
        driver = FakeHumanRequestDriver(self._promised_actions("completed"))

        result = self._run(driver)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(
            [call[2] for call in driver.calls if call[0] == "apply"],
            ["prepare", "dispatch", "supervise", "audit", "publish", "finalize"],
        )

    def test_already_applied_is_a_successful_replay(self):
        driver = FakeHumanRequestDriver(
            self._promised_actions("completed"),
            apply_status="already_applied",
        )

        result = self._run(driver)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(len([call for call in driver.calls if call[0] == "attention"]), 0)

    def test_driver_attention_result_ends_without_mark_attention_retry(self):
        driver = FakeHumanRequestDriver(
            [_transition("prepare-1", "prepare")],
            apply_status="attention",
        )

        result = self._run(driver)

        self.assertEqual(result["status"], "attention")
        self.assertEqual(result["reason_code"], "driver_attention")
        self.assertEqual([call for call in driver.calls if call[0] == "attention"], [])

    def test_new_pr_and_existing_pr_update_have_one_publish_and_finalize(self):
        from robert_agent import workflow_operations

        for output_type in ("new_pr", "update_existing_pr"):
            with self.subTest(output_type=output_type):
                workflow_operations.reset_human_request_driver()
                transitions = self._promised_actions("completed")
                transitions[-3]["state"] = {"promised_actions": [output_type]}
                driver = FakeHumanRequestDriver(transitions)

                result = self._run(driver)

                self.assertEqual(result["status"], "completed")
                self.assertEqual(
                    sum(call[2] == "dispatch" for call in driver.calls if call[0] == "apply"),
                    1,
                )
                self.assertEqual(
                    sum(call[2] == "publish" for call in driver.calls if call[0] == "apply"),
                    1,
                )
                self.assertEqual(
                    sum(call[2] == "finalize" for call in driver.calls if call[0] == "apply"),
                    1,
                )

    def test_wait_timeout_then_response_consumes_one_signal(self):
        message = {
            "signal_id": "signal-1",
            "topic": "subject-events",
            "subject_type": "task",
            "subject_id": "task-1",
            "fact_id": "fact-1",
        }
        driver = FakeHumanRequestDriver(
            [
                _transition("wait-1", "wait", wait_seconds=3),
                _transition("wait-2", "wait", wait_seconds=4),
            ]
            + self._promised_actions("completed")
        )

        result = self._run(driver, recv=[None, message])

        self.assertEqual(result["status"], "completed")
        calls = [call[0] for call in driver.calls]
        self.assertEqual(calls[:4], ["next", "next", "consume", "next"])
        self.assertEqual(calls.count("apply"), 6)

    def test_additive_followup_is_created_only_after_promised_actions(self):
        transitions = self._promised_actions("completed")
        transitions.insert(
            -1,
            _transition("followup-1", "create_followup", state={"followup": "task-2"}),
        )
        driver = FakeHumanRequestDriver(transitions)

        result = self._run(driver)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(
            [call[2] for call in driver.calls if call[0] == "apply"][-1],
            "create_followup",
        )

    def test_rejected_and_closed_or_merged_invalidation_publish_nothing(self):
        from robert_agent import workflow_operations

        for terminal_status in ("rejected", "invalidated"):
            with self.subTest(terminal_status=terminal_status):
                workflow_operations.reset_human_request_driver()
                transitions = [
                    _transition("prepare-1", "prepare"),
                    _transition("dispatch-1", "dispatch"),
                    _transition("supervise-1", "supervise"),
                    _transition("audit-1", "audit", state={"action_count": 1}),
                    _transition(
                        "terminal-1",
                        "terminal",
                        state={"status": terminal_status},
                        terminal=True,
                    ),
                ]
                driver = FakeHumanRequestDriver(transitions)

                result = self._run(driver)

                self.assertEqual(result["status"], terminal_status)
                self.assertNotIn(
                    "publish", [call[2] for call in driver.calls if call[0] == "apply"]
                )
                self.assertNotIn(
                    "finalize", [call[2] for call in driver.calls if call[0] == "apply"]
                )

    def test_duplicate_signal_is_harmless(self):
        message = {
            "signal_id": "signal-duplicate",
            "topic": "subject-events",
            "subject_type": "task",
            "subject_id": "task-1",
            "fact_id": "fact-1",
        }
        driver = FakeHumanRequestDriver(
            [
                _transition("wait-1", "wait", wait_seconds=1),
            ]
            + self._promised_actions("completed"),
            consume_status="duplicate",
        )

        result = self._run(driver, recv=[message])

        self.assertEqual(result["status"], "completed")
        self.assertEqual(len([call for call in driver.calls if call[0] == "consume"]), 1)

    def test_malformed_signal_marks_attention_without_driver_actions(self):
        driver = FakeHumanRequestDriver([_transition("wait-1", "wait", wait_seconds=1)])

        result = self._run(driver, recv=[{"signal_id": "only-id"}])

        self.assertEqual(result["status"], "attention")
        self.assertEqual(result["reason_code"], "signal_invalid")
        self.assertEqual(
            [call[2] for call in driver.calls if call[0] == "attention"],
            ["signal_invalid"],
        )
        self.assertEqual([call for call in driver.calls if call[0] == "apply"], [])

    def test_unknown_signal_topic_does_not_reach_the_driver(self):
        driver = FakeHumanRequestDriver([_transition("wait-1", "wait", wait_seconds=1)])
        message = {
            "signal_id": "signal-unknown", "topic": "unknown-topic",
            "subject_type": "task", "subject_id": "task-1", "fact_id": "fact-1",
        }

        result = self._run(driver, recv=[message])

        self.assertEqual(result["reason_code"], "signal_invalid")
        self.assertEqual([call for call in driver.calls if call[0] == "consume"], [])

    def test_repeated_actionable_transition_marks_attention_once(self):
        transition = _transition("same-id", "prepare")
        driver = FakeHumanRequestDriver([transition, dict(transition)])

        result = self._run(driver)

        self.assertEqual(result["reason_code"], "transition_not_progressed")
        self.assertEqual(
            [call[2] for call in driver.calls if call[0] == "attention"],
            ["transition_not_progressed"],
        )
        self.assertEqual(len([call for call in driver.calls if call[0] == "apply"]), 1)

    def test_malformed_transition_and_out_of_order_finalize_are_attention(self):
        malformed = {
            "transition_id": "bad",
            "operation": "dispatch",
            "wait_seconds": None,
            "terminal": False,
        }
        driver = FakeHumanRequestDriver([malformed])

        result = self._run(driver)

        self.assertEqual(result["reason_code"], "transition_invalid")
        self.assertEqual([call for call in driver.calls if call[0] == "apply"], [])

        driver = FakeHumanRequestDriver(
            [_transition("finalize-1", "finalize", state={"promised_actions": ["comment"]})]
        )
        from robert_agent import workflow_operations

        workflow_operations.reset_human_request_driver()
        result = self._run(driver)
        self.assertEqual(result["reason_code"], "transition_out_of_order")
        self.assertEqual([call for call in driver.calls if call[0] == "apply"], [])

        driver = FakeHumanRequestDriver(
            [_transition("terminal-1", "terminal", state={"status": "completed"}, terminal=True)]
        )
        workflow_operations.reset_human_request_driver()
        result = self._run(driver)
        self.assertEqual(result["reason_code"], "terminal_out_of_order")
        self.assertEqual([call for call in driver.calls if call[0] == "apply"], [])

        driver = FakeHumanRequestDriver(
            [
                _transition("prepare-1", "prepare"),
                _transition("dispatch-1", "dispatch"),
                _transition("supervise-1", "supervise"),
                _transition("audit-1", "audit", state={"action_count": 1}),
                _transition(
                    "terminal-1",
                    "terminal",
                    state={"status": "completed", "promised_actions": ["comment"]},
                    terminal=True,
                ),
            ]
        )
        workflow_operations.reset_human_request_driver()
        result = self._run(driver)
        self.assertEqual(result["reason_code"], "terminal_out_of_order")
        self.assertNotIn(
            "publish", [call[2] for call in driver.calls if call[0] == "apply"]
        )
        self.assertNotIn(
            "finalize", [call[2] for call in driver.calls if call[0] == "apply"]
        )

        driver = FakeHumanRequestDriver(
            [
                _transition("prepare-1", "prepare"),
                _transition("dispatch-1", "dispatch"),
                _transition("supervise-1", "supervise"),
                _transition("audit-1", "audit", state={"action_count": 1}),
                _transition("finalize-1", "finalize", state={"action_count": 0}),
                _transition(
                    "terminal-1",
                    "terminal",
                    state={"status": "completed"},
                    terminal=True,
                ),
            ]
        )
        workflow_operations.reset_human_request_driver()
        result = self._run(driver)
        self.assertEqual(result["reason_code"], "transition_out_of_order")
        self.assertNotIn(
            "finalize", [call[2] for call in driver.calls if call[0] == "apply"]
        )

    def test_audit_transition_requires_explicit_action_count(self):
        from robert_agent import workflow_operations

        with self.assertRaises(ValueError):
            workflow_operations.validate_human_request_transition(
                _transition("audit-1", "audit")
            )

    def test_conflicting_driver_registration_is_rejected(self):
        from robert_agent import workflow_operations

        first = FakeHumanRequestDriver([])
        second = FakeHumanRequestDriver([])
        self.assertIs(workflow_operations.configure_human_request_driver(first), first)
        self.assertIs(workflow_operations.configure_human_request_driver(first), first)
        with self.assertRaisesRegex(ValueError, "conflicting"):
            workflow_operations.configure_human_request_driver(second)

    def test_transition_validation_rejects_sensitive_or_unbounded_state(self):
        from robert_agent import workflow_operations

        with self.assertRaises(ValueError):
            workflow_operations.validate_human_request_transition(
                _transition("one", "prepare", state={"prompt": "do not pass"})
            )
        for message in (
            "postgresql://operator:secret@example.invalid/robert",
            "Authorization: Bearer abc",
            "-----BEGIN PRIVATE KEY-----",
        ):
            with self.subTest(message=message), self.assertRaises(ValueError):
                workflow_operations.validate_human_request_transition(
                    _transition("one", "prepare", state={"message": message})
                )
        with self.assertRaises(ValueError):
            workflow_operations.validate_human_request_transition(
                _transition("one", "wait", wait_seconds=0)
            )


if __name__ == "__main__":
    unittest.main()
