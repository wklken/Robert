from robert_agent.adapters.processes import attempt as attempt_process
from robert_agent.adapters.processes import identity as worker_identity
from tests.support.repository_cycle import (
    RepositoryCycleFixture,
)


class AttemptIdentityTests(RepositoryCycleFixture):
    def test_dispatch_process_status_requires_complete_recorded_identity(self):

        status = attempt_process.dispatch_status({"dispatch": {"pid": 43210, "status": "running"}})

        self.assertEqual(status["status"], "unknown")
        self.assertEqual(status["reason"], "identity_unverified")

    def test_dispatch_process_status_treats_zombie_pid_as_not_found(self):
        original_inspect = worker_identity.inspect_recorded_identity
        try:
            worker_identity.inspect_recorded_identity = lambda _identity: "dead"
            status = attempt_process.dispatch_status({"dispatch": self._recorded_identity(43210)})
        finally:
            worker_identity.inspect_recorded_identity = original_inspect

        self.assertEqual(status["status"], "dead")
        self.assertEqual(status["reason"], "process_missing_or_zombie")

    def test_terminate_attempt_process_skips_sigterm_for_zombie_pid(self):
        from unittest.mock import patch

        with (
            patch.object(
                worker_identity,
                "terminate_recorded_identity",
                return_value={"status": "already_dead", "pid": 24680},
            ),
            patch.object(worker_identity, "live_process_group", return_value=False),
        ):
            result = attempt_process.terminate_attempt({"dispatch": self._recorded_identity(24680)})

        self.assertEqual(result["status"], "already_dead")
        self.assertEqual(result["pid"], 24680)

    def test_dispatch_process_status_detects_zombie_pid_without_proc_stat(self):
        from unittest.mock import patch

        with (
            patch.object(
                worker_identity,
                "inspect_recorded_identity",
                return_value="dead",
            ),
            patch.object(worker_identity, "live_process_group", return_value=False),
        ):
            status = attempt_process.dispatch_status({"dispatch": self._recorded_identity(13579)})

        self.assertEqual(status["status"], "dead")
        self.assertEqual(status["reason"], "process_missing_or_zombie")
