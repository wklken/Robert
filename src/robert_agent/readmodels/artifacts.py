"""Read registered artifact files with the dashboard containment policy."""

import os
from pathlib import Path
import stat


def read_artifact_content(db_path, registered_path, max_bytes=65536):
    try:
        root = Path(db_path).expanduser().resolve().parent
        path = Path(registered_path).expanduser().resolve(strict=True)
        relative = path.relative_to(root)
        # Open every component relative to an already-open directory. A worker
        # replacing a checked directory or file with a symlink cannot redirect I/O.
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for component in relative.parts[:-1]:
                child_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = child_fd
            file_fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)
        with os.fdopen(file_fd, "rb") as fh:
            info = os.fstat(fh.fileno())
            if not stat.S_ISREG(info.st_mode):
                return None
            truncated = info.st_size > max_bytes
            if truncated:
                fh.seek(-max_bytes, 2)
            content = fh.read(max_bytes).decode("utf-8", errors="replace")
    except (OSError, ValueError, RuntimeError):
        return None
    return {"content": content, "truncated": truncated}
