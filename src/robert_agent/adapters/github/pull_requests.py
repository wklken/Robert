"""Read exact current pull request metadata through the configured GitHub CLI."""

import json
import os
import re
import time

from robert_agent.foundation.commands import ControlDeadlineExceeded, run_command


def read_pr(repo, number, *, runner=None, stop=None, timeout=60, host=None):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or type(number) is not int or number <= 0:
        raise ValueError("Invalid GitHub PR coordinates")
    deadline = getattr(stop, "deadline", float("inf"))
    timeout = min(timeout, deadline - time.monotonic())
    if timeout <= 0 or (stop is not None and stop.is_set()):
        raise ControlDeadlineExceeded(["gh", "api"], 0)
    command = ["gh", "api"]
    options = {}
    if host is not None:
        command += ["--hostname", host]
        environment = {k: v for k, v in os.environ.items() if k not in {"GH_HOST", "GH_REPO"}}
        environment.update(GH_HOST=host, GH_PROMPT_DISABLED="1")
        options["env"] = environment
    if stop is not None and stop.is_set():
        raise ControlDeadlineExceeded(command, 0)
    completed = run_command(
        [*command, f"repos/{repo}/pulls/{number}"],
        runner=runner,
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout,
        **options,
    )
    value = json.loads(completed.stdout)
    if not isinstance(value, dict) or type(value.get("number")) is not int or value["number"] != number:
        raise ValueError("GitHub returned invalid PR metadata")
    return value


def read_native_prs(repo, numbers, *, runner=None, stop=None):
    """Read exact public GitHub targets inside one shared command budget."""
    deadline = time.monotonic() + 30
    snapshots = []
    for number in numbers:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or (stop is not None and stop.is_set()):
            break
        snapshots.append(read_pr(repo, number, host="github.com", stop=stop, runner=runner, timeout=min(10, remaining)))
    return snapshots
