"""HTTP fault injection also runs in the standard CI test matrix."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tests.simulations import diagnosis_rate_limit_scenarios as diagnosis
from tests.simulations import model_rate_limit_scenarios as workers


class ModelRateLimitScenarios(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(prefix='robert-429-test-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_intermittent_worker_rate_limits(self):
        for count in (1, 2):
            with self.subTest(count=count):
                self.assertTrue(workers._recovers(self.root / str(count), count)['passed'])

    def test_persistent_worker_rate_limits_are_bounded(self):
        self.assertTrue(workers._exhausted(self.root / 'worker')['passed'])

    def test_concurrent_workers_share_limited_endpoint(self):
        self.assertTrue(workers._concurrent(self.root / 'worker')['passed'])

    def test_cancellation_during_backoff(self):
        self.assertTrue(workers._canceled(self.root / 'worker')['passed'])

    def test_diagnosis_rate_limit_recovery_and_retry_after(self):
        for header, kind in [('3', 'seconds'), (None, 'http_date'),
                             ('invalid', 'invalid_header'), (None, 'missing_header')]:
            with self.subTest(kind=kind):
                self.assertTrue(diagnosis.recovered(self.root / kind, header, kind=kind)['passed'])

    def test_persistent_diagnosis_rate_limits_survive_restart(self):
        self.assertTrue(diagnosis.exhausted(self.root / 'diagnosis')['passed'])

    def test_worker_recovers_while_diagnosis_is_rate_limited(self):
        self.assertTrue(diagnosis.simultaneous(self.root / 'both')['passed'])
