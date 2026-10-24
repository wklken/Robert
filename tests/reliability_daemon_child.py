"""Real resident SQLite daemon, local Git remote, isolated subprocess crash.

The daemon's local-drain subprocess seam runs the real loop inline so the test
can inject its local Git runner. All domain, scheduling, audit, publication and
notification code is the production implementation.
"""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from robert_agent import daemon, loop_engine, publish

root = Path(sys.argv[1])
crash_after_push = sys.argv[2] == 'crash'
load_context = daemon._load_context
local_drain = daemon._run_local_drain
publisher = publish.publish_ready_actions


def context(*args, **kwargs):
    value, error = load_context(*args, **kwargs)
    if value:
        value.startup_live_poll_pending = False
        value.next_live_poll_at = '2099-01-01T00:00:00+00:00'
    return value, error


def local_git(command, **kwargs):
    if command[0] != 'git':
        raise AssertionError('Unexpected external command: ' + command[0])
    with (root / 'calls.jsonl').open('a') as output:
        output.write(json.dumps(command) + '\n')
    completed = subprocess.run(command, **kwargs)
    if crash_after_push and command[:2] == ['git', 'push'] and completed.returncode == 0:
        os._exit(73)
    return completed


def local_publish(*args, **kwargs):
    return publisher(*args, **{**kwargs, 'run_command': local_git})


def inline_child(command, **kwargs):
    stream=io.StringIO()
    with redirect_stdout(stream):
        loop_engine.main(command[3:])
    return json.loads(stream.getvalue())


def drain(value, **kwargs):
    return local_drain(value, **{**kwargs,'child_runner':inline_child})


with patch.object(daemon, '_load_context', side_effect=context), \
     patch.object(daemon, '_run_local_drain', side_effect=drain), \
     patch.object(publish, 'publish_ready_actions', side_effect=local_publish):
    result = daemon.run_daemon(root / 'daemon.yml')
    print(json.dumps(result), flush=True)
    raise SystemExit(0 if result.get('ok') else 1)
