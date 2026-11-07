"""Process-owned publication locks shared by publisher and operator cancellation."""
from contextlib import contextmanager
import fcntl
import hashlib
from pathlib import Path


@contextmanager
def task_publication_lock(db_path, task_id):
    # File locks are released by the OS even when the publisher crashes. Never
    # unlink the inode: a waiting process could otherwise lock a different file.
    directory = Path(str(Path(db_path).expanduser().resolve()) + '.publication-locks')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = hashlib.sha256(task_id.encode('utf-8')).hexdigest()
    with (directory / (name + '.lock')).open('a') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
