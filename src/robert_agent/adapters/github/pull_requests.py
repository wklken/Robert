"""Read pull request metadata through the configured GitHub CLI."""

import json
import subprocess


def read_pr(repo, number):
    completed = subprocess.run(
        ["gh", "api", f"repos/{repo}/pulls/{number}"],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    value = json.loads(completed.stdout)
    if not isinstance(value, dict) or value.get("number") != number:
        raise ValueError("GitHub returned invalid PR metadata")
    return value
