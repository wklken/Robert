"""GitHub CLI JSON reads with the control-plane command deadline."""

import json
import subprocess
from robert_agent.foundation.commands import run_command


def read_json(args, runner=subprocess.run):
    completed = run_command(args, runner=runner, text=True, capture_output=True, check=True)
    stdout = completed.stdout.strip()
    if not stdout:
        return []
    return json.loads(stdout)


def try_read_json(args, runner=subprocess.run):
    try:
        return read_json(args, runner=runner)
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None


def command_key(args):
    args = list(args)
    if "--hostname" in args:
        index = args.index("--hostname")
        del args[index : index + 2]
    return json.dumps(args)


def is_actions_job_log_read(args):
    return (
        len(args) == 8
        and tuple(args[:4]) == ("gh", "run", "view", "--repo")
        and args[5] == "--job"
        and args[6].isdigit()
        and args[7] == "--log-failed"
    )
