import tempfile
import unittest
from pathlib import Path
import socket
import stat
from unittest import mock

from tests.support import PACKAGE_ROOT  # noqa: F401 - add worktree src to sys.path


class WakeupNotifierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "data" / "robert.sqlite3"

    def test_listener_receives_and_coalesces_datagrams(self):
        from robert_agent import wakeup_notifier

        with wakeup_notifier.WakeupListener(self.db_path) as listener:
            first = wakeup_notifier.notify(self.db_path)
            second = wakeup_notifier.notify(self.db_path)

            self.assertEqual(first["status"], "notified")
            self.assertEqual(second["status"], "notified")
            self.assertTrue(listener.wait(0.5))
            self.assertFalse(listener.wait(0))

    def test_missing_listener_is_a_best_effort_noop(self):
        from robert_agent import wakeup_notifier

        result = wakeup_notifier.notify(self.db_path)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "unavailable")

    def test_notification_send_is_nonblocking(self):
        from robert_agent import wakeup_notifier

        class Sender:
            blocking = True

            def __enter__(self):
                return self

            def __exit__(self, _exc_type, _exc, _traceback):
                pass

            def setblocking(self, blocking):
                self.blocking = blocking

            def sendto(self, _payload, _path):
                if self.blocking:
                    raise AssertionError("notification send must not block")

        with mock.patch.object(
            wakeup_notifier.socket,
            "socket",
            return_value=Sender(),
        ):
            result = wakeup_notifier.notify(self.db_path)

        self.assertEqual(result["status"], "notified")

    def test_listener_replaces_a_stale_socket(self):
        from robert_agent import wakeup_notifier

        path = wakeup_notifier.socket_path_for_db(self.db_path)
        path.parent.mkdir(parents=True)
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        stale.bind(str(path))
        stale.close()

        with wakeup_notifier.WakeupListener(self.db_path) as listener:
            self.assertEqual(listener.path, path)
            self.assertEqual(wakeup_notifier.notify(self.db_path)["status"], "notified")

    def test_listener_does_not_unlink_a_replacement_socket(self):
        from robert_agent import wakeup_notifier

        listener = wakeup_notifier.WakeupListener(self.db_path)
        path = listener.path
        path.unlink()
        replacement = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(replacement.close)
        replacement.bind(str(path))

        listener.close()

        self.assertTrue(path.exists())

    def test_listener_restricts_directory_and_socket_permissions(self):
        from robert_agent import wakeup_notifier

        path = wakeup_notifier.socket_path_for_db(self.db_path)
        path.parent.mkdir(parents=True, mode=0o755)
        path.parent.chmod(0o755)

        with wakeup_notifier.WakeupListener(self.db_path):
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_databases_in_one_directory_have_independent_sockets(self):
        from robert_agent import wakeup_notifier

        other_db_path = self.db_path.with_name("other.sqlite3")
        with (
            wakeup_notifier.WakeupListener(self.db_path) as first,
            wakeup_notifier.WakeupListener(other_db_path) as second,
        ):
            self.assertNotEqual(first.path, second.path)
            wakeup_notifier.notify(self.db_path)
            self.assertTrue(first.wait(0.5))
            self.assertFalse(second.wait(0))

    def test_failed_socket_setup_removes_the_bound_path(self):
        from robert_agent import wakeup_notifier

        path = wakeup_notifier.socket_path_for_db(self.db_path)
        real_chmod = wakeup_notifier.os.chmod

        def fail_socket_chmod(target, mode):
            if Path(target) == path:
                raise OSError("chmod failed")
            return real_chmod(target, mode)

        with mock.patch.object(
            wakeup_notifier.os,
            "chmod",
            side_effect=fail_socket_chmod,
        ):
            with self.assertRaisesRegex(OSError, "chmod failed"):
                wakeup_notifier.WakeupListener(self.db_path)

        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
