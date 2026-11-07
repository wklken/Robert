"""Offline acceptance of independently supervised runtime programs."""

from robert_agent.supervision import health as supervision_health

import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time

from robert_agent.adapters.services import native as service
from robert_agent.persistence.schema import database as storage
from robert_agent.adapters.processes import identity as worker_identity


_FIXTURE = """
import json, sys, time
from pathlib import Path

if sys.argv[1:3] == ['supervisor', 'run']:
    from robert_agent.runtime import supervisor
    config = Path(sys.argv[sys.argv.index('--config') + 1])
    result = supervisor.run(json.loads(config.read_text()))
    raise SystemExit(0 if result['ok'] else 1)

assert sys.argv[1:3] in (['daemon', 'run'], ['web', 'run'])
time.sleep(600)
"""


def _ctl(config, env, *args, timeout=12):
    return subprocess.run(
        [sys.executable, "-m", "supervisor.supervisorctl", "-c", str(config), *args],
        cwd=config.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _expect_ctl(config, env, *args):
    result = _ctl(config, env, *args)
    if result.returncode:
        raise AssertionError(
            f"Supervisor {args} failed ({result.returncode}): {(result.stderr or result.stdout)[-500:]}"
        )
    return result.stdout.strip()


def _wait(condition, label, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if value := condition():
            return value
        time.sleep(0.12)
    raise AssertionError(f"Supervisor fixture did not reach {label}")


def _running(config, env, name, *, not_pid=None):
    outcome = _ctl(config, env, "status", name, timeout=2)
    fields = outcome.stdout.split()
    if outcome.returncode or fields[:3] != [name, "RUNNING", "pid"] or len(fields) < 4:
        return None
    pid = int(fields[3].rstrip(","))
    return pid if pid != not_pid else None


def _health(path, pid, *, after=""):
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if payload.get("ok") and payload.get("pid") == pid and payload.get("checked_at", "") > after:
        return payload
    return None


def _shutdown(config, env, process):
    if process.poll() is None:
        _expect_ctl(config, env, "shutdown")
    if process.wait(timeout=10) != 0:
        raise AssertionError("Temporary supervisord did not exit cleanly")


def _cleanup(config, env, process):
    if process.poll() is not None:
        return
    try:
        _ctl(config, env, "shutdown", timeout=2)
        process.wait(timeout=4)
    except (OSError, subprocess.SubprocessError):
        process.terminate()
        try:
            process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


def run(root):
    started = time.monotonic()
    fixture = Path(root).resolve() / "service-scenarios"
    fixture.mkdir()
    source = fixture / "fixture.py"
    source.write_text(_FIXTURE, encoding="utf-8")
    executable = fixture / "robert-fixture"
    executable.write_text(f'#!/bin/sh\nexec {shlex.join([sys.executable, "-B", str(source)])} "$@"\n', encoding="utf-8")
    executable.chmod(0o700)
    config_path = fixture / "runtime.json"
    data = fixture / "runtime"
    db = data / "state.sqlite3"
    storage.init_database(db)
    config = {
        "config_path": str(config_path),
        "data_dir": str(data),
        "db_path": str(db),
        "repos": [],
        "supervision": {"mode": "observe", "poll_seconds": 1, "batch_size": 5, "stall_seconds": 2},
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")
    health = supervision_health.health_path(config)
    # macOS Unix sockets also append the manager PID while binding. Keep their
    # private namespace short even when the caller's temp directory is long.
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="rs-svc-") as directory:
        service_root = Path(directory)
        service_data = service_root / "data"
        sockets = service_data / "run"
        sockets.mkdir(parents=True)
        (service_data / "logs").mkdir()
        env = {"PATH": os.defpath, "HOME": str(service_root)}
        managers = []
        identities = []

        def capture(pid):
            identity = worker_identity.capture_process_identity(pid)
            identities.append(identity)
            return identity

        try:
            for target in ("daemon", "supervisor"):
                definition = service_root / f"{target}.conf"
                definition.write_text(
                    service.render_supervisord_config(executable, config_path, service_data, target=target),
                    encoding="utf-8",
                )
                with (fixture / f"{target}-manager.log").open("wb") as log:
                    process = subprocess.Popen(
                        [sys.executable, "-m", "supervisor.supervisord", "-n", "-c", str(definition)],
                        cwd=service_root,
                        env=env,
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                managers.append((definition, process))
            ((daemon, daemon_manager), (observer, observer_manager)) = managers
            daemon_pid = _wait(lambda: _running(daemon, env, service.SUPERVISOR_PROGRAM), "daemon RUNNING")
            web_pid = _wait(lambda: _running(daemon, env, "robert-web"), "fixture web RUNNING")
            original_pid = _wait(lambda: _running(observer, env, service.SUPERVISION_PROGRAM), "supervisor RUNNING")
            capture(daemon_pid)
            capture(web_pid)
            capture(original_pid)
            before = _wait(lambda: _health(health, original_pid), "initial supervisor heartbeat")

            _expect_ctl(daemon, env, "stop", service.SUPERVISOR_PROGRAM)
            stopped = _ctl(daemon, env, "status", service.SUPERVISOR_PROGRAM).stdout.split()
            assert stopped[:2] == [service.SUPERVISOR_PROGRAM, "STOPPED"], stopped
            assert _running(daemon, env, "robert-web") == web_pid
            _wait(
                lambda: _health(health, original_pid, after=before["checked_at"]),
                "heartbeat after targeted daemon stop",
            )
            assert _running(observer, env, service.SUPERVISION_PROGRAM) == original_pid

            _expect_ctl(observer, env, "restart", service.SUPERVISION_PROGRAM)
            restarted_pid = _wait(
                lambda: _running(observer, env, service.SUPERVISION_PROGRAM, not_pid=original_pid),
                "restarted supervisor RUNNING",
            )
            restarted_identity = capture(restarted_pid)
            restart_health = _wait(lambda: _health(health, restarted_pid), "restarted supervisor heartbeat")
            assert worker_identity.inspect_recorded_identity(restarted_identity) == "alive_same"
            os.kill(restarted_pid, signal.SIGKILL)
            recovered_pid = _wait(
                lambda: _running(observer, env, service.SUPERVISION_PROGRAM, not_pid=restarted_pid),
                "automatic supervisor restart after SIGKILL",
            )
            capture(recovered_pid)
            _wait(
                lambda: _health(health, recovered_pid, after=restart_health["checked_at"]),
                "recovered supervisor heartbeat",
            )
            assert _running(daemon, env, "robert-web") == web_pid
            assert daemon_manager.poll() is None and observer_manager.poll() is None

            _shutdown(observer, env, observer_manager)
            _shutdown(daemon, env, daemon_manager)
            assert not list(sockets.iterdir()), "temporary socket or PID file remains after shutdown"
            for identity in identities:
                assert worker_identity.inspect_recorded_identity(identity) == "dead", identity
                assert not worker_identity.live_process_group(identity["process_group_id"]), identity
            for _, manager in managers:
                assert not worker_identity.live_process_group(manager.pid), manager.pid
        finally:
            for definition, process in reversed(managers):
                _cleanup(definition, env, process)
            for identity in identities:
                if worker_identity.inspect_recorded_identity(identity) == "alive_same":
                    worker_identity.terminate_recorded_identity(identity)
    return {
        "scenario": "isolated_supervisord_services",
        "passed": True,
        "duration_seconds": round(time.monotonic() - started, 3),
        "checks": [
            "two_real_managers",
            "daemon_target_stopped_independently",
            "legacy_web_program_untouched",
            "heartbeat_advanced_after_daemon_stop",
            "supervisor_restart_observed",
            "supervisor_autorestarted_after_sigkill",
            "all_process_groups_and_socket_files_reaped",
        ],
    }
