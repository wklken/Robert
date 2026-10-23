import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stderr
import io


class DBOSRuntimeTests(unittest.TestCase):
    def setUp(self):
        from dbos import DBOS
        from robert_agent import dbos_runtime

        dbos_runtime.destroy_runtime()
        DBOS.destroy()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def tearDown(self):
        from dbos import DBOS
        from robert_agent import dbos_runtime

        dbos_runtime.destroy_runtime()
        DBOS.destroy()

    def _config(self, mode="shadow"):
        return {
            "workflow_engine": {
                "mode": mode,
                "system_database_url_env": "ROBERT_DBOS_TEST_URL",
                "system_database_scheme": "sqlite" if mode != "legacy" else None,
                "configured": mode != "legacy",
            },
            "dbos_executor_id": "test-executor",
        }

    def test_legacy_runtime_is_a_noop_and_idempotent(self):
        from robert_agent import dbos_runtime

        with mock.patch.object(dbos_runtime, "DBOS") as dbos_class:
            first = dbos_runtime.configure_runtime(self._config("legacy"))
            second = dbos_runtime.configure_runtime(self._config("legacy"))

        self.assertIs(first, second)
        self.assertEqual(first.mode, "legacy")
        self.assertIsNone(first.dbos)
        dbos_class.assert_not_called()
        self.assertNotIn("sqlite:///secret-db.sqlite3", repr(first))

    def test_destroy_runtime_resets_human_request_driver_without_dbos_runtime(self):
        from robert_agent import dbos_runtime, workflow_operations

        class Driver:
            def next_transition(self, _task_id):
                return {}

            def apply_transition(self, _task_id, _transition):
                return {}

            def consume_signal(self, _task_id, _message):
                return {}

            def mark_attention(self, _task_id, _reason_code):
                return None

        workflow_operations.configure_human_request_driver(Driver())
        dbos_runtime.destroy_runtime()
        with self.assertRaisesRegex(RuntimeError, "HumanRequestDriver"):
            workflow_operations.current_human_request_driver()

        workflow_operations.configure_human_request_driver(Driver())
        dbos_runtime.configure_runtime(self._config("legacy"))
        dbos_runtime.destroy_runtime()
        with self.assertRaisesRegex(RuntimeError, "HumanRequestDriver"):
            workflow_operations.current_human_request_driver()

    def test_destroy_runtime_resets_remediation_driver_without_dbos_runtime(self):
        from robert_agent import dbos_runtime, workflow_operations

        class Driver:
            def next_transition(self, _episode_id):
                return {}

            def apply_transition(self, _episode_id, _transition):
                return {}

            def consume_signal(self, _episode_id, _message):
                return {}

            def mark_attention(self, _episode_id, _reason_code):
                return None

        workflow_operations.configure_remediation_driver(Driver())
        dbos_runtime.destroy_runtime()
        with self.assertRaisesRegex(RuntimeError, "RemediationDriver"):
            workflow_operations.current_remediation_driver()

    def test_conflicting_runtime_configuration_fails_loudly(self):
        from robert_agent import dbos_runtime

        with mock.patch.dict(
            os.environ,
            {"ROBERT_DBOS_TEST_URL": "sqlite:///first.sqlite3"},
            clear=False,
        ):
            first = dbos_runtime.configure_runtime(self._config())
            self.assertEqual(first.mode, "shadow")
            os.environ["ROBERT_DBOS_TEST_URL"] = "sqlite:///second.sqlite3"
            with self.assertRaises(ValueError):
                dbos_runtime.configure_runtime(self._config())

    def test_launch_and_destroy_are_idempotent_and_admin_server_is_disabled(self):
        from robert_agent import dbos_runtime

        url = f"sqlite:///{Path(self.tmp.name) / 'dbos.sqlite3'}"
        with mock.patch.dict(
            os.environ,
            {"ROBERT_DBOS_TEST_URL": url},
            clear=False,
        ):
            runtime = dbos_runtime.configure_runtime(self._config())
            dbos_runtime.launch_runtime(runtime)
            dbos_runtime.launch_runtime(runtime)
            self.assertFalse(runtime.dbos._config["runtimeConfig"]["run_admin_server"])
            self.assertEqual(runtime.dbos._config["name"], "robert")
            self.assertEqual(runtime.dbos._config["system_database_url"], url)
            dbos_runtime.destroy_runtime()
            dbos_runtime.destroy_runtime()

    def test_dbos_startup_does_not_log_system_database_url(self):
        from robert_agent import dbos_runtime

        secret_path = Path(self.tmp.name) / "dbos-secret.sqlite3"
        url = f"sqlite:///{secret_path}"
        logs = io.StringIO()
        with mock.patch.dict(
            os.environ,
            {"ROBERT_DBOS_TEST_URL": url},
            clear=False,
        ), redirect_stderr(logs):
            runtime = dbos_runtime.configure_runtime(self._config())
            dbos_runtime.launch_runtime(runtime)
            dbos_runtime.destroy_runtime()

        self.assertNotIn(url, logs.getvalue())
        self.assertNotIn("dbos-secret.sqlite3", logs.getvalue())

    def test_failed_credentialed_dbos_startup_does_not_log_url_or_password(self):
        from robert_agent import dbos_runtime

        url = "postgresql://operator:supersecret@example.invalid/robert"
        logs = io.StringIO()
        with mock.patch.dict(
            os.environ,
            {"ROBERT_DBOS_TEST_URL": url},
            clear=False,
        ), redirect_stderr(logs):
            runtime = dbos_runtime.configure_runtime(self._config())
            with self.assertRaises(Exception):
                dbos_runtime.launch_runtime(runtime)

        self.assertNotIn(url, logs.getvalue())
        self.assertNotIn("supersecret", logs.getvalue())

    def test_same_bound_workflow_id_is_reused(self):
        from robert_agent import dbos_runtime, workflow_operations

        class TerminalDriver:
            def next_transition(self, task_id):
                return {
                    "transition_id": f"terminal:{task_id}",
                    "operation": "terminal",
                    "wait_seconds": None,
                    "terminal": True,
                    "state": {"status": "invalidated"},
                }

            def apply_transition(self, task_id, transition):
                raise AssertionError((task_id, transition))

            def consume_signal(self, task_id, message):
                raise AssertionError((task_id, message))

            def mark_attention(self, task_id, reason_code):
                raise AssertionError((task_id, reason_code))

        url = f"sqlite:///{Path(self.tmp.name) / 'dbos.sqlite3'}"
        binding = {
            "subject_type": "task",
            "subject_id": "task-1",
            "workflow_kind": "human_request",
            "owner_engine": "dbos",
            "definition_version": "v1",
            "workflow_id": "robert:v1:task:task-1",
            "binding_status": "active",
        }
        with mock.patch.dict(
            os.environ,
            {"ROBERT_DBOS_TEST_URL": url},
            clear=False,
        ):
            runtime = dbos_runtime.configure_runtime(self._config())
            dbos_runtime.launch_runtime(runtime)
            workflow_operations.configure_human_request_driver(TerminalDriver())
            first = dbos_runtime.start_bound_workflow(binding)
            second = dbos_runtime.start_bound_workflow(binding)
            self.assertEqual(first, binding["workflow_id"])
            self.assertEqual(second, first)
            statuses = runtime.dbos.list_workflows(
                workflow_ids=[binding["workflow_id"]],
                limit=10,
            )

        self.assertEqual(len(statuses), 1)
        self.assertEqual(statuses[0].workflow_id, binding["workflow_id"])

    def test_bound_workflow_rejects_legacy_or_conflicting_binding(self):
        from robert_agent import dbos_runtime

        with self.assertRaises(ValueError):
            dbos_runtime.start_bound_workflow(
                {
                    "subject_type": "task",
                    "subject_id": "task-1",
                    "workflow_kind": "human_request",
                    "owner_engine": "legacy",
                    "workflow_id": None,
                }
            )

    def test_public_runtime_payload_never_contains_database_url(self):
        from robert_agent import dbos_runtime

        url = "sqlite:///secret-db.sqlite3"
        with mock.patch.dict(
            os.environ,
            {"ROBERT_DBOS_TEST_URL": url},
            clear=False,
        ):
            runtime = dbos_runtime.configure_runtime(self._config())
        payload = runtime.public_payload()
        self.assertNotIn(url, json.dumps(payload, sort_keys=True))
        self.assertEqual(payload["system_database_scheme"], "sqlite")


if __name__ == "__main__":
    unittest.main()
