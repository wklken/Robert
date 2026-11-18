import json
import subprocess
import unittest

from robert_agent.adapters.github import recording


class GitHubRecordingTests(unittest.TestCase):
    def test_actions_job_logs_replay_only_a_redacted_bounded_excerpt(self):
        args = ["gh", "run", "view", "--repo", "example/backend", "--job", "7", "--log-failed"]

        def runner(command, **kwargs):
            log = ("checkout output\n" * 30) + "\x1b[31mtest failed at /home/runner/work/test.py:5\x1b[0m\n"
            return subprocess.CompletedProcess(command, 0, log, "")

        recorder = recording.RecordingRunner(runner, max_job_log_chars=80)
        recorder(args, check=True)
        [payload] = recorder.responses.values()
        self.assertNotIn("/home/runner", payload)
        text = recording.ReplayRunner(recorder.responses)(args, check=True).stdout
        self.assertEqual(text, json.loads(payload)["log_excerpt"])
        self.assertLessEqual(len(text), 80)
        self.assertIn("test failed at <local-path>:5", text)
        self.assertNotIn("\x1b", text)

    def test_actions_job_log_with_sensitive_evidence_is_unavailable(self):
        args = ["gh", "run", "view", "--repo", "example/backend", "--job", "7", "--log-failed"]

        def runner(command, **kwargs):
            log = "Authorization: Bearer " + "a" * 24 + "\n" + "unrelated output\n" * 30
            return subprocess.CompletedProcess(command, 0, log, "")

        recorder = recording.RecordingRunner(runner, max_job_log_chars=80)
        recorder(args, check=True)
        self.assertEqual(list(recorder.responses.values()), [None])
        with self.assertRaises(subprocess.CalledProcessError):
            recording.ReplayRunner(recorder.responses)(args, check=True)
