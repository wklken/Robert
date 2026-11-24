import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
import hashlib
import hmac
from pathlib import Path
import tempfile
from threading import Barrier, Event
import unittest
from unittest import mock

from robert_agent.interfaces.web import auth


def authorization(username="robert", password="cache-test-password"):
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()


class BasicAuthCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.salt = b"cache-test-salt".ljust(32, b".")
        cls.password_hash = hashlib.pbkdf2_hmac("sha256", b"cache-test-password", cls.salt, auth.ITERATIONS)

    def setUp(self):
        self.credentials = auth.BasicAuth("robert", self.salt, self.password_hash, auth.ITERATIONS)

    def test_repeated_success_and_equivalent_headers_skip_password_derivation(self):
        with mock.patch.object(auth.hashlib, "pbkdf2_hmac", wraps=hashlib.pbkdf2_hmac) as derive:
            self.assertTrue(self.credentials.authorized(authorization()))
            self.assertTrue(self.credentials.authorized(authorization()))
            self.assertTrue(self.credentials.authorized(authorization().replace("Basic", "basic")))
            self.assertEqual(derive.call_count, 1)

    def test_wrong_credentials_are_never_cached_and_do_not_displace_success(self):
        with (
            mock.patch.object(auth.hashlib, "pbkdf2_hmac", wraps=hashlib.pbkdf2_hmac) as derive,
            mock.patch.object(auth.hmac, "compare_digest", wraps=hmac.compare_digest) as compare,
        ):
            self.assertTrue(self.credentials.authorized(authorization()))
            for username, password in [("robert", "wrong"), ("other", "cache-test-password"), ("robert", "")]:
                with self.subTest(username=username, password=password):
                    for _ in range(2):
                        compare.reset_mock()
                        self.assertFalse(self.credentials.authorized(authorization(username, password)))
                        self.assertIn(mock.call(username.encode(), b"robert"), compare.call_args_list)
                        self.assertTrue(any(call.args[1] == self.password_hash for call in compare.call_args_list))
            self.assertTrue(self.credentials.authorized(authorization()))
            self.assertEqual(derive.call_count, 7)

    def test_malformed_credentials_still_fail_before_derivation_after_a_cache_hit(self):
        self.assertTrue(self.credentials.authorized(authorization()))
        with mock.patch.object(auth.hashlib, "pbkdf2_hmac", wraps=hashlib.pbkdf2_hmac) as derive:
            for header in [
                None,
                1,
                "",
                "Bearer ignored",
                "Basic !!!",
                "Basic " + base64.b64encode(b"missing-colon").decode(),
                "Basic " + base64.b64encode(b"robert:\xff").decode(),
                "Basic " + base64.b64encode(b"robert:" + b"a" * auth.MAX_CREDENTIAL_BYTES).decode(),
                authorization() + ", " + authorization(),
                "Basic " + "a" * (auth.MAX_FILE_BYTES + 1),
            ]:
                with self.subTest(header=header):
                    self.assertFalse(self.credentials.authorized(header))
            derive.assert_not_called()

    def test_success_expires_at_sixty_seconds_and_hits_do_not_extend_it(self):
        with (
            mock.patch.object(auth, "monotonic", return_value=100.0, create=True) as clock,
            mock.patch.object(auth.hashlib, "pbkdf2_hmac", wraps=hashlib.pbkdf2_hmac) as derive,
        ):
            self.assertTrue(self.credentials.authorized(authorization()))
            clock.return_value = 159.999
            self.assertTrue(self.credentials.authorized(authorization()))
            self.assertEqual(derive.call_count, 1)
            clock.return_value = 160.0
            self.assertTrue(self.credentials.authorized(authorization()))
            self.assertEqual(derive.call_count, 2)
            clock.return_value = 219.999
            self.assertTrue(self.credentials.authorized(authorization()))
            self.assertEqual(derive.call_count, 2)
            clock.return_value = 220.0
            self.assertTrue(self.credentials.authorized(authorization()))
            self.assertEqual(derive.call_count, 3)

    def test_fresh_instances_and_replaced_settings_do_not_share_success(self):
        same_settings = replace(self.credentials)
        changed_hash = hashlib.pbkdf2_hmac("sha256", b"changed-password", self.salt, auth.ITERATIONS)
        changed_settings = replace(self.credentials, password_hash=changed_hash)
        with mock.patch.object(auth.hashlib, "pbkdf2_hmac", wraps=hashlib.pbkdf2_hmac) as derive:
            for credentials in [self.credentials, same_settings]:
                self.assertTrue(credentials.authorized(authorization()))
                self.assertTrue(credentials.authorized(authorization()))
            self.assertEqual(derive.call_count, 2)
            self.assertFalse(changed_settings.authorized(authorization()))
            self.assertTrue(changed_settings.authorized(authorization(password="changed-password")))
            self.assertEqual(derive.call_count, 4)
        with self.assertRaises(FrozenInstanceError):
            self.credentials.password_hash = changed_hash

    def test_loading_a_changed_file_uses_new_settings_without_mutating_running_verifier(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"
            auth.write_basic_auth_file(path, "robert", "cache-test-password")
            running = auth.load_basic_auth_file(path)
            with mock.patch.object(auth.hashlib, "pbkdf2_hmac", wraps=hashlib.pbkdf2_hmac) as derive:
                self.assertTrue(running.authorized(authorization()))
                reloaded = auth.load_basic_auth_file(path)
                self.assertTrue(reloaded.authorized(authorization()))
                self.assertEqual(derive.call_count, 2)
            replacement = Path(directory) / "replacement.json"
            auth.write_basic_auth_file(replacement, "robert", "changed-password")
            replacement.replace(path)
            reloaded = auth.load_basic_auth_file(path)
            self.assertFalse(reloaded.authorized(authorization()))
            self.assertTrue(reloaded.authorized(authorization(password="changed-password")))
            self.assertTrue(running.authorized(authorization()))
            self.assertFalse(running.authorized(authorization(password="changed-password")))

    def test_concurrent_warm_requests_reuse_success_and_reject_wrong_credentials(self):
        workers = 8
        barrier = Barrier(workers)

        def request(index):
            barrier.wait(timeout=5)
            return self.credentials.authorized(authorization(password="wrong") if index % 2 else authorization())

        with mock.patch.object(auth.hashlib, "pbkdf2_hmac", wraps=hashlib.pbkdf2_hmac) as derive:
            self.assertTrue(self.credentials.authorized(authorization()))
            with ThreadPoolExecutor(max_workers=workers) as executor:
                self.assertEqual(list(executor.map(request, range(workers))), [True, False] * 4)
            self.assertEqual(derive.call_count, 5)
            self.assertTrue(self.credentials.authorized(authorization()))
            self.assertEqual(derive.call_count, 5)

    def test_overlapping_cold_successes_do_not_slide_the_first_success_expiration(self):
        started = Event()
        release = Event()
        derive_password = hashlib.pbkdf2_hmac

        def delayed_derivation(*args):
            started.set()
            if not release.wait(timeout=5):
                raise AssertionError("concurrent validation was not released")
            return derive_password(*args)

        with (
            mock.patch.object(auth, "monotonic", return_value=100.0, create=True) as clock,
            mock.patch.object(auth.hashlib, "pbkdf2_hmac", side_effect=delayed_derivation) as derive,
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            pending = executor.submit(self.credentials.authorized, authorization())
            try:
                self.assertTrue(started.wait(timeout=5))
                derive.side_effect = derive_password
                self.assertTrue(self.credentials.authorized(authorization()))
                clock.return_value = 159.0
            finally:
                release.set()
            self.assertTrue(pending.result(timeout=5))
            self.assertEqual(derive.call_count, 2)
            clock.return_value = 160.0
            self.assertTrue(self.credentials.authorized(authorization()))
            self.assertEqual(derive.call_count, 3)
            self.assertTrue(self.credentials.authorized(authorization()))
            self.assertEqual(derive.call_count, 3)

    def test_cache_retains_one_hmac_fingerprint_and_no_password_header_or_secret_repr(self):
        self.assertTrue(self.credentials.authorized(authorization()))
        cache = self.credentials._cache
        fingerprint, expires_at = cache.success
        self.assertEqual(fingerprint, hmac.digest(cache.secret, b"robert:cache-test-password", "sha256"))
        self.assertEqual(len(fingerprint), 32)
        self.assertIsInstance(expires_at, float)
        other = replace(self.credentials)
        self.assertNotEqual(cache.secret, other._cache.secret)
        for sensitive in [
            "cache-test-password",
            authorization(),
            cache.secret.hex(),
            fingerprint.hex(),
            self.password_hash.hex(),
        ]:
            self.assertNotIn(sensitive, repr(self.credentials) + repr(cache))
        # Exercise replacement with several verified candidates to enforce the memory
        # bound even if a verifier accepted more than one password hash preimage.
        with mock.patch.object(auth.hashlib, "pbkdf2_hmac", return_value=self.password_hash):
            for index in range(100):
                self.assertTrue(self.credentials.authorized(authorization(password=f"candidate-{index}")))
        self.assertEqual(len(cache.success), 2)
        self.assertEqual(cache.success[0], hmac.digest(cache.secret, b"robert:candidate-99", "sha256"))
