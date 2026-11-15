"""Schedule bounded native observation and coordinator control children."""

import argparse
from contextlib import closing, contextmanager, nullcontext
from dataclasses import asdict
import os
from pathlib import Path
import signal
import sys
from threading import Event
import time

from robert_agent.application import source_cycle
from robert_agent.configuration import command as validate_config
from robert_agent.execution import control_children, runtime_state, source_controller
from robert_agent.foundation.commands import emit
from robert_agent.runtime.control import ControlLane, recover
from robert_agent.work.application import source_observation


SCAN = "native_scan"
MODEL = "native_model"
SCAN_INTERVAL_SECONDS = 30
_SCAN_LIMITS = source_observation.ScanLimits()


def command(lane, config_path, db_path, owner, *, local_only=False):
    return [
        sys.executable,
        "-B",
        "-m",
        "robert_agent.runtime.native_sources",
        lane,
        "--config",
        str(config_path),
        "--db",
        str(db_path),
        "--owner",
        owner,
        *(["--local-only"] if local_only else []),
    ]


def timeout(lane, config):
    if lane == SCAN:
        return _SCAN_LIMITS.seconds + 15
    workers = config.get("workers") or ()
    # Refresh/preparation and group cleanup have budgets in addition to the model.
    return max((item["timeout_seconds"] for item in workers), default=120) + _SCAN_LIMITS.seconds + 15


def scan_due(db_path, config):
    return source_observation.has_scan_work(db_path, config)


def model_due(db_path, config, *, owner, local_only):
    return source_cycle.has_due_source(db_path, config, owner=owner, local_only=local_only)


class _Stop:
    def __init__(self, stop_requested):
        self.signal = Event()
        self.stop_requested = stop_requested
        self.deadline = float(os.environ.get("_ROBERT_CONTROL_DEADLINE", "inf"))

    def is_set(self):
        return self.signal.is_set() or self.stop_requested() or time.monotonic() >= self.deadline

    def wait(self, seconds):
        self.signal.wait(min(seconds, max(0, self.deadline - time.monotonic())))
        return self.is_set()


@contextmanager
def stop_signals(stop_requested=lambda: False):
    stop = _Stop(stop_requested)

    def request_stop(_signum, _frame):
        stop.signal.set()

    previous = {key: signal.signal(key, request_stop) for key in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield stop
    finally:
        for key, handler in previous.items():
            signal.signal(key, handler)


def run_control_child(db_path, lane, executor, argv, *, timeout_seconds, stop, heartbeat=None):
    """Run one owned control child, settling only its wrapper group on exit."""
    limit = min(timeout_seconds, stop.deadline - time.monotonic())
    if stop.is_set() or limit <= 2:
        return {"ok": False, "status": "child_timeout"}
    child = ControlLane(db_path, lane, executor)
    child.start(argv, timeout_seconds=limit)
    result = None
    next_heartbeat = time.monotonic() + 5
    try:
        while not stop.is_set():
            result = child.poll()
            if result is not None:
                break
            if heartbeat is not None and time.monotonic() >= next_heartbeat:
                heartbeat()
                next_heartbeat = time.monotonic() + 5
            stop.wait(0.05)
    finally:
        stopped = child.stop()
    if not stopped:
        return {"ok": False, "status": "attention", "reason": "control_child_cleanup_unproven"}
    return result or {"ok": False, "status": "child_stopped"}


def _run_bounded(lane, config_path, db_path, config, *, executor, owner, local_only, stop, heartbeat):
    return run_control_child(
        db_path,
        lane,
        executor,
        command(lane, config_path, db_path, owner, local_only=local_only),
        timeout_seconds=timeout(lane, config),
        stop=stop,
        heartbeat=heartbeat,
    )


def needs_attention(results):
    return results.get("status") == "control_child_attention" or any(
        isinstance(value, dict) and not value.get("ok") and value.get("status") == "attention"
        for value in results.values()
    )


def failed(results):
    return needs_attention(results) or any(
        isinstance(value, dict) and not value.get("ok") for value in results.values()
    )


def runnable(db_path, config, *, local_only):
    """Describe scheduled source work before it has a repository."""
    return (not local_only and scan_due(db_path, config)) or model_due(
        db_path, config, owner=None, local_only=local_only
    )


def run_once(config_path, config, *, local_only=False, daemon_owner=None, stop_requested=lambda: False, heartbeat=None):
    """Observe before advancing a single source, using the daemon's controller lease."""
    db_path = Path(config["db_path"])
    if stop_requested() or not runnable(db_path, config, local_only=local_only):
        return {}
    held = (
        nullcontext(source_controller.StandaloneController(daemon_owner, daemon_owner, "acquired"))
        if daemon_owner
        else source_controller.standalone(
            db_path, config_path, ttl_seconds=timeout(SCAN, config) + timeout(MODEL, config) + 30, cleanup=recover
        )
    )
    with held as controller:
        if not controller.owner:
            return {"status": controller.status}
        if daemon_owner and not recover(
            db_path, scope_key=source_controller.resource_key(db_path), current_owner=controller.executor
        ):
            return {"status": "control_child_attention"}
        results = {}
        with stop_signals(stop_requested) as stop:

            def step(lane, drain_only):
                return _run_bounded(
                    lane,
                    config_path,
                    db_path,
                    config,
                    executor=controller.executor,
                    owner=controller.owner,
                    local_only=drain_only,
                    stop=stop,
                    heartbeat=heartbeat,
                )

            if not local_only and scan_due(db_path, config):
                results[SCAN] = step(SCAN, False)
            drain_only = local_only or (SCAN in results and not results[SCAN]["ok"])
            if (
                not needs_attention(results)
                and not stop.is_set()
                and model_due(db_path, config, owner=controller.owner, local_only=drain_only)
            ):
                results[MODEL] = step(MODEL, drain_only)
        if needs_attention(results):
            controller.status = "control_child_attention"
        controller.summary = results
        return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lane", choices=(SCAN, MODEL))
    parser.add_argument("--config", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--local-only", action="store_true")
    args = parser.parse_args(argv)
    executor = os.environ.get("_ROBERT_EXECUTOR_ID")
    if not executor or not Path(args.db).is_file():
        return emit({"ok": False, "status": "control_owner_required"}, 3)
    with closing(runtime_state.connect(args.db)) as conn:
        owner = control_children.source_controller_owner(
            conn, child_id=executor, lane=args.lane, database_path=args.db, requested_owner=args.owner
        )
    if owner is None:
        return emit({"ok": False, "status": "control_owner_required"}, 3)
    config = validate_config.validate_config(args.config, skip_external=True)
    if not config["ok"]:
        return emit(config, 3)
    if Path(args.db).resolve() != Path(config["db_path"]).resolve():
        return emit({"ok": False, "status": "control_database_changed"}, 3)
    with stop_signals() as stop:
        if args.lane == SCAN:
            if args.local_only:
                return emit({"ok": True, "status": "paused"})
            result = source_observation.scan_sources(args.db, config, limits=_SCAN_LIMITS, stop=stop)
        else:
            result = source_cycle.advance_source(
                args.db, config["data_dir"], config, owner=owner, local_only=args.local_only, stop=stop
            )
    return emit({"ok": True, **asdict(result)})


if __name__ == "__main__":
    raise SystemExit(main())
