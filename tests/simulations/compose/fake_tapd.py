#!/usr/local/bin/python
"""TAPD executable served by the isolated fixture, with writes read from stdin."""

import json
import os
import sys
from urllib.error import URLError

from client import fixture


def main():
    argv = sys.argv[1:]
    payload = {
        "argv": argv,
        "body": sys.stdin.read(),
        "actor": {"pid": os.getpid(), "pgid": os.getpgrp(), "parent_pid": os.getppid()},
    }
    try:
        result = fixture("/tapd", payload, timeout=25)
    except (URLError, OSError):
        print(json.dumps({"message": "fixture reply lost"}), file=sys.stderr)
        return 1
    sys.stdout.write(result["stdout"])
    sys.stderr.write(result["stderr"])
    return result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
