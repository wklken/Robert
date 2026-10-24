"""Isolated process fixture: real daemon/driver/DBOS, local Git remote only.

Configuration validation still checks the production PostgreSQL contract. Only
DBOS's system database URL is replaced by SQLite in this test process; live
GitHub discovery is disabled. No production execution code is replaced.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from robert_agent import daemon, dbos_runtime, workflow_driver

root = Path(sys.argv[1])
crash_after_push = sys.argv[2] == 'crash'
load_context = daemon._load_context
configure_runtime = dbos_runtime.configure_runtime
configure_driver = workflow_driver.configure_driver


def context(*args, **kwargs):
    value, error = load_context(*args, **kwargs)
    if value:
        value.startup_live_poll_pending = False
        value.next_live_poll_at = '2099-01-01T00:00:00+00:00'
    return value, error


def runtime(config):
    with patch.dict(os.environ, {'ROBERT_ISOLATED_SYSTEM_DB': 'sqlite:///' + str(root / 'system.db')}):
        return configure_runtime(config)


def local_git(command, **kwargs):
    if command[0] != 'git':
        raise AssertionError('Unexpected external command: ' + command[0])
    with (root / 'calls.jsonl').open('a') as output:
        output.write(json.dumps(command) + '\n')
    completed = subprocess.run(command, **kwargs)
    if crash_after_push and command[:2] == ['git', 'push'] and completed.returncode == 0:
        os._exit(73)  # external effect happened; no domain acknowledgement
    return completed


def driver(config):
    value = configure_driver(config)
    value.publish_runner = local_git
    return value


with patch.object(daemon, '_load_context', side_effect=context), \
     patch.object(dbos_runtime, 'configure_runtime', side_effect=runtime), \
     patch.object(workflow_driver, 'configure_driver', side_effect=driver):
    result = daemon.run_daemon(root / 'daemon.yml')
    print(json.dumps(result), flush=True)
    raise SystemExit(0 if result.get('ok') else 1)
