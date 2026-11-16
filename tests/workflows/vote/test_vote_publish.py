"""Vote comment reconciliation separates failed reads from conflicting evidence."""

import json
import subprocess
from types import SimpleNamespace
import unittest
from unittest import mock

from robert_agent.foundation.commands import ControlDeadlineExceeded
from robert_agent.workflows.vote import publish

MARKER = "<!-- robert-vote round=round-1 -->"
BODY = f"# 多 Agent 投票报告\n{MARKER}"


class VoteReconcileTests(unittest.TestCase):
    def reconcile(self, runner):
        data = {"body": BODY, "target_url": "https://github.com/x/y/issues/1"}
        manifest = {"repo": "x/y", "number": 1}
        with mock.patch.object(publish, "validate_action", return_value=(data, manifest, MARKER)):
            return publish.reconcile({}, {"vote_publisher": "bot"}, runner)

    def test_failed_read_is_distinct_from_conflicting_evidence(self):
        comments = [{"id": number, "body": BODY, "user": {"login": "bot"}} for number in (1, 2)]
        for name, runner, reason_code in (
            ("exit", lambda *_a, **_k: SimpleNamespace(returncode=1, stdout="", stderr="reset"), "vote_query_failed"),
            ("timeout", mock.Mock(side_effect=subprocess.TimeoutExpired(["gh"], 60)), "vote_query_failed"),
            ("os", mock.Mock(side_effect=OSError("gh missing")), "vote_query_failed"),
            (
                "conflict",
                lambda *_a, **_k: SimpleNamespace(returncode=0, stdout=json.dumps(comments), stderr=""),
                "vote_reconciliation_unknown",
            ),
            (
                "malformed",
                lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="not json", stderr=""),
                "vote_reconciliation_unknown",
            ),
        ):
            with self.subTest(name):
                result = self.reconcile(runner)
                self.assertEqual((result["status"], result["reason_code"]), ("unknown", reason_code))

    def test_control_deadline_escapes_reconciliation(self):
        with self.assertRaises(ControlDeadlineExceeded):
            self.reconcile(mock.Mock(side_effect=ControlDeadlineExceeded(["gh", "api"], 0)))


if __name__ == "__main__":
    unittest.main()
