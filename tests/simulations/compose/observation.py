"""Read-only observations of a live installation."""

from contextlib import closing
from pathlib import Path
import sqlite3


def query(db, sql, parameters=()):
    with closing(sqlite3.connect(Path(db).as_uri() + "?mode=ro", uri=True, timeout=0.1)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, parameters)]


def backup(db, target):
    with (
        closing(sqlite3.connect(Path(db).as_uri() + "?mode=ro", uri=True, timeout=0.1)) as source,
        closing(sqlite3.connect(target)) as destination,
    ):
        source.backup(destination)


def process(pid):
    path = Path("/proc") / str(int(pid))
    try:
        # comm can contain spaces and closing parentheses.
        _, _, stat = (path / "stat").read_text().rpartition(") ")
        fields = stat.split()
        return {
            "pid": pid,
            "state": fields[0],
            "ppid": int(fields[1]),
            "pgid": int(fields[2]),
            "start_token": fields[19],
            "argv": (path / "cmdline").read_bytes().decode(errors="replace").split("\0")[:-1],
        }
    except (ProcessLookupError, FileNotFoundError):
        return None


def group(pgid):
    return [
        row
        for path in Path("/proc").iterdir()
        if path.name.isdecimal() and (row := process(int(path.name))) and row["pgid"] == pgid and row["state"] != "Z"
    ]
