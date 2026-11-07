"""Best-effort local notification for durable SQLite wakeups."""

import hashlib
import os
from pathlib import Path
import select
import socket
import stat


def socket_path_for_db(db_path):
    db_path = Path(db_path).expanduser().resolve()
    identity = hashlib.sha256(str(db_path).encode("utf-8")).hexdigest()[:16]
    return db_path.parent / "run" / f"w-{identity}.sock"


def notify(db_path, *, sender=None):
    try:
        if sender is not None:
            sender(db_path)
        else:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifier:
                notifier.setblocking(False)
                notifier.sendto(b"1", str(socket_path_for_db(db_path)))
    except Exception as exc:
        return {
            "ok": True,
            "status": "unavailable",
            "safe_error": str(exc),
        }
    return {"ok": True, "status": "notified"}


class WakeupListener:
    def __init__(self, db_path):
        self.path = socket_path_for_db(db_path)
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        try:
            existing = self.path.lstat()
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if not stat.S_ISSOCK(existing.st_mode):
                raise FileExistsError(f"wakeup path is not a socket: {self.path}")
            self.path.unlink()
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self._bound_identity = None
        try:
            self._socket.bind(str(self.path))
            bound = self.path.lstat()
            self._bound_identity = (bound.st_dev, bound.st_ino)
            os.chmod(self.path, 0o600)
            self._socket.setblocking(False)
        except Exception:
            self._socket.close()
            self._unlink_bound_path()
            raise

    def wait(self, timeout_seconds):
        readable, _writable, _exceptional = select.select(
            [self._socket],
            [],
            [],
            max(0, timeout_seconds),
        )
        if not readable:
            return False
        while True:
            try:
                self._socket.recv(1)
            except BlockingIOError:
                break
        return True

    def close(self):
        self._socket.close()
        self._unlink_bound_path()

    def _unlink_bound_path(self):
        if self._bound_identity is None:
            return
        try:
            current = self.path.lstat()
        except FileNotFoundError:
            return
        if (current.st_dev, current.st_ino) == self._bound_identity:
            self.path.unlink()

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        self.close()
