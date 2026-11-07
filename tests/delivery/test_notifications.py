from contextlib import closing
import sqlite3
from tests.support.temp_workspace import TemporaryWorkspace


class NotificationTests(TemporaryWorkspace):
    def test_notify_records_local_notification(self):
        from robert_agent.delivery.application import notifications as notify

        db_path = self.root / "dd.sqlite3"
        self._init_notifications_db(db_path)

        result = notify.record_notification(
            db_path=db_path,
            notification_type="worker_stale",
            status="sent",
            metadata={"task_id": "task-1"},
        )

        self.assertTrue(result["ok"], result)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            row = conn.execute(
                "SELECT notification_type, status, task_id FROM notifications"
            ).fetchone()
        self.assertEqual(row, ("worker_stale", "sent", "task-1"))

    def test_notify_command_uses_argv_not_shell(self):
        from robert_agent.delivery.application import notifications as notify

        db_path = self.root / "dd.sqlite3"
        self._init_notifications_db(db_path)
        calls = []

        class Proc:
            returncode = 0

        def fake_run(args, **kwargs):
            calls.append((args, kwargs))
            return Proc()

        notify.record_notification(db_path, "worker_stale", "recorded")
        result = notify.deliver_pending(
            db_path,
            command="notify-tool --channel 'local alerts'",
            runner=fake_run,
        )

        self.assertEqual(result["sent"], 1, result)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], ["notify-tool", "--channel", "local alerts"])
        self.assertNotIn("shell", calls[0][1])

    def _init_notifications_db(self, db_path):
        from robert_agent.adapters.sqlite import database as storage

        storage.init_database(db_path)
