"""Publication must remain stopped when ownership reads consume its remaining time."""

from threading import Event
import unittest
from unittest.mock import patch

from robert_agent.execution.source_controller import PublicationStop


class PublicationStopTests(unittest.TestCase):
    def test_deadline_reached_during_owner_check_stops_publication(self):
        clock = [10.0]

        def current():
            clock[0] = 20.0
            return True

        with patch("robert_agent.execution.source_controller.time.monotonic", side_effect=lambda: clock[0]):
            self.assertTrue(PublicationStop(20.0, current).is_set())

    def test_cancellation_during_owner_check_stops_publication(self):
        cancellation = Event()

        def current():
            cancellation.set()
            return True

        with patch("robert_agent.execution.source_controller.time.monotonic", return_value=10.0):
            self.assertTrue(PublicationStop(20.0, current, cancellation).is_set())
