"""Test public exact GitHub command transport bounds and protocol failures."""

from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from robert_agent.adapters.github.discussion import DiscussionReader


class CurrentDiscussionTransportTests(unittest.TestCase):
    def test_overall_control_deadline_is_checked_before_spawn(self):
        with patch.dict("os.environ", {"_ROBERT_CONTROL_DEADLINE": "0"}):
            reader = DiscussionReader(runner=lambda *_args, **_kwargs: self.fail("spawned after deadline"))
            self.assertIsNone(reader.parent("org/repo", 17))

    def test_oversize_and_non_http_error_prose_do_not_prove_any_observation(self):
        for stdout in (b"x" * (1024 * 1024 + 1), b"gh: Not Found (HTTP 404)"):
            with self.subTest(size=len(stdout)):
                calls = []

                def run(command, **kwargs):
                    calls.append(command)
                    self.assertEqual(kwargs["max_output_bytes"], 1024 * 1024)
                    return SimpleNamespace(returncode=1, stdout=stdout, stderr=b"")

                result = DiscussionReader(runner=run).item(
                    repository="org/repo",
                    pr_number=17,
                    event_type="comment",
                    item_id="66",
                    event_url="https://github.com/org/repo/pull/17#issuecomment-66",
                )
                self.assertEqual(result.status, "unavailable")
                self.assertEqual(len(calls), 1)

    def test_stop_before_spawn_prevents_transport(self):
        stop = Event()
        stop.set()
        reader = DiscussionReader(stop=stop, runner=lambda *_args, **_kwargs: self.fail("spawned after stop"))
        self.assertIsNone(reader.parent("org/repo", 17))
