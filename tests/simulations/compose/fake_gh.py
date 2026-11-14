#!/usr/bin/env python3
"""Offline gh executable: forward an argv call to the Compose fixture."""

import json
import os
from pathlib import Path
import sys
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    payload = {
        "argv": argv,
        "actor": {
            "pid": os.getpid(),
            "pgid": os.getpgid(0),
            "parent_pid": os.getppid(),
            "executor_id": os.environ.get("_ROBERT_EXECUTOR_ID"),
        },
    }
    if argv[:2] == ["pr", "create"] and "--body-file" in argv:
        index = argv.index("--body-file") + 1
        if index < len(argv):
            payload["body_file"] = sys.stdin.read() if argv[index] == "-" else Path(argv[index]).read_text()
    base = os.environ.get("ROBERT_FIXTURE_URL", "http://fixtures:8765").rstrip("/")
    request = Request(f"{base}/gh", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=180) as response:
            result = json.load(response)
    except (URLError, OSError, ValueError) as exc:
        print(f"gh: fixture request failed: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(result["stdout"])
    sys.stderr.write(result["stderr"])
    return result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
