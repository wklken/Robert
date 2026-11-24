"""Capture fixture and live database evidence before removing Compose volumes."""

import json
from pathlib import Path
import subprocess

from bootstrap import DB, GIT, STATE, write_json
from client import fixture
from observation import backup, process


def capture():
    output = STATE / "evidence"
    output.mkdir(exist_ok=True)
    failures = []
    try:
        remote = fixture("/state")
        write_json(output / "remote-state.json", remote)
        write_json(output / "checkpoints.json", fixture("/checkpoints?after_seq=0"))
        if remote.get("failures"):
            failures.extend(remote["failures"])
    except Exception as exc:
        failures.append(f"capture fixture: {exc}")
    if DB.is_file():
        try:
            backup(DB, output / "robert.sqlite3")
        except Exception as exc:
            failures.append(f"SQLite backup: {exc}")
    write_json(
        output / "processes.json",
        [row for path in Path("/proc").iterdir() if path.name.isdecimal() and (row := process(int(path.name)))],
    )
    refs = {}
    for bare in GIT.glob("*/*.git"):
        refs[str(bare.relative_to(GIT))] = subprocess.check_output(
            ["git", f"--git-dir={bare}", "show-ref"], text=True, timeout=5
        ).splitlines()
    write_json(output / "git-refs.json", refs)
    write_json(output / "capture-failures.json", failures)
    return failures


def abort_barriers():
    pending = fixture("/state")["barriers"]
    for barrier in pending:
        if barrier["decision"] is None:
            fixture(f"/barriers/{barrier['barrier_id']}/release", {"decision": "abort"})


if __name__ == "__main__":
    print(json.dumps(capture()))
