"""Vote source refresh must not convert a lane deadline into a failed write."""

import unittest
from unittest import mock

from robert_agent.foundation.commands import ControlDeadlineExceeded
from robert_agent.workflows.vote import publish


class VotePublicationDeadlineTests(unittest.TestCase):
    def test_source_snapshot_deadline_escapes(self):
        manifest = {"repo": "x/y", "number": 1, "source_type": "issue"}
        with (
            mock.patch.object(publish, "validate_action", return_value=({}, manifest, "marker")),
            mock.patch.object(publish, "reconcile", return_value={"status": "confirmed_absent"}),
        ):
            with self.assertRaises(ControlDeadlineExceeded):
                publish.publish({}, {}, mock.Mock(side_effect=ControlDeadlineExceeded(["gh", "api"], 0)))
