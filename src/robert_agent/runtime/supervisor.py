"""runtime supervisor responsibilities."""

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from robert_agent.delivery.application import notifications as notify
from robert_agent.supervision import diagnosis as supervision_diagnose
from robert_agent.configuration import supervision as supervision_config

from robert_agent.supervision.application import checks as supervision_checks
from robert_agent.supervision import health as supervision_health


@contextmanager
def instance_lock(config):
    directory = supervision_health.runtime_dir(config)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(directory / "supervisor.lock", os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            pass
        yield acquired
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def run(config, *, once=False, stop=None):
    settings = {**supervision_config.DEFAULTS, **config.get("supervision", {})}
    if settings["mode"] == "off":
        return {"ok": True, "status": "disabled"}
    stop = stop or threading.Event()
    with instance_lock(config) as acquired:
        if not acquired:
            return {"ok": False, "status": "already_running"}
        handlers = {}
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                handlers[sig] = signal.signal(sig, lambda *_: stop.set())
        delivery = None
        maintenance = None
        maintenance_started = 0.0
        try:
            while not stop.is_set():
                result = supervision_checks.check(config, record=True)
                if result["ok"]:
                    result["diagnosis"] = supervision_diagnose.poll(config)
                if delivery is not None and delivery.poll() is not None:
                    delivery.wait()
                    delivery = None
                if delivery is None and os.environ.get("ROBERT_NOTIFICATION_COMMAND"):
                    delivery = subprocess.Popen(
                        [
                            sys.executable,
                            "-m",
                            "robert_agent.runtime.supervisor",
                            "--deliver-once",
                            str(config["db_path"]),
                            str(supervision_health.health_path(config)),
                        ],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                if maintenance is not None and maintenance.poll() is not None:
                    maintenance.wait()
                    maintenance = None
                if (
                    maintenance is not None
                    and time.monotonic() - maintenance_started > 360
                ):
                    _stop_child(maintenance)
                    maintenance = None
                if (
                    result["ok"]
                    and settings.get("repair")
                    and maintenance is None
                    and not once
                ):
                    maintenance = subprocess.Popen(
                        [
                            sys.executable,
                            "-m",
                            "robert_agent.runtime.supervisor",
                            "--repair-once",
                            str(Path(config["config_path"]).resolve()),
                        ],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                    maintenance_started = time.monotonic()
                if once:
                    return result
                stop.wait(settings["poll_seconds"])
            return {"ok": True, "status": "stopped"}
        finally:
            supervision_diagnose.shutdown(config)
            if maintenance is not None:
                _stop_child(maintenance)
            if delivery is not None:
                try:
                    delivery.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    delivery.terminate()
                    try:
                        delivery.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        delivery.kill()
                        delivery.wait()
            for sig, handler in handlers.items():
                signal.signal(sig, handler)


def _stop_child(proc):
    if proc.poll() is not None:
        proc.wait()
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)


def _repair_once(config_path):
    from robert_agent.workflows.maintenance import repair as supervision_repair
    from robert_agent.workflows.maintenance import (
        followup as supervision_repair_followup,
    )
    from robert_agent.configuration import command as validate_config

    def stopped(*_):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stopped)
    signal.signal(signal.SIGINT, stopped)
    config = validate_config.validate_config(config_path, skip_external=True)
    if config.get("ok"):
        supervision_repair.poll(config)
        supervision_repair_followup.poll(
            config, runtime=supervision_health.runtime_status(config)
        )


def main(argv=None):
    args = [sys.argv[0], *(sys.argv[1:] if argv is None else argv)]
    if len(args) == 3 and args[1] == "--repair-once":
        _repair_once(args[2])
    elif len(args) == 4 and args[1] == "--deliver-once":
        supervision_checks.deliver_health(args[2], Path(args[3]))
        try:
            if Path(args[2]).is_file():
                notify.deliver_pending(args[2], limit=1)
        except (OSError, sqlite3.Error):
            pass
    else:
        raise SystemExit("Use robert supervisor run/check/status")


if __name__ == "__main__":
    raise SystemExit(main())
