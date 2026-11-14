"""SSH command for the synthetic GitHub installation.

Git still runs its own upload/receive-pack and hook processes. The only
substitution is the transport to the synthetic remote.
"""

import os
from pathlib import Path
import re
import shlex
import sys

from client import fixture


ROOT = Path("/git")
_REMOTE = re.compile(r"/?([a-zA-Z0-9_-]+/[a-zA-Z0-9_.-]+)\.git")


def activity(operation, entity, phase):
    return fixture(
        "/activity",
        {
            "operation": operation,
            "entity": entity,
            "phase": phase,
            "actor": {
                "pid": os.getpid(),
                "pgid": os.getpgrp(),
                "parent_pid": os.getppid(),
                "executor_id": os.environ.get("_ROBERT_EXECUTOR_ID"),
            },
        },
        timeout=330,
    )["decision"]


def transport(args):
    if len(args) != 2 or args[0] != "git@github.com":
        raise ValueError("only the synthetic GitHub SSH host is allowed")
    command = shlex.split(args[1])
    if len(command) != 2 or command[0] not in {"git-upload-pack", "git-receive-pack"}:
        raise ValueError("only Git pack commands are allowed")
    match = _REMOTE.fullmatch(command[1])
    if match is None or any(part in {".", ".."} for part in match[1].split("/")):
        raise ValueError("invalid synthetic repository path")
    repo = match[1]
    path = ROOT / f"{repo}.git"
    if not path.is_dir() or not path.resolve().is_relative_to(ROOT):
        raise ValueError("unknown synthetic repository")
    if activity(command[0], {"repo": repo}, "before_effect") != "continue":
        return 1
    os.execvp(command[0], [command[0], str(path)])


def post_receive(repo):
    for line in sys.stdin:
        old, new, ref = line.split()
        if (
            activity("git-receive-pack", {"repo": repo, "ref": ref, "old": old, "new": new}, "post_receive")
            != "continue"
        ):
            return 1
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["post-receive"]:
        raise SystemExit(post_receive(sys.argv[2]))
    raise SystemExit(transport(sys.argv[1:]))
