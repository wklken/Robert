"""Independent daily proposal analysis and digest scheduler."""

import argparse
from contextlib import closing
from datetime import date
import os
import signal
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

from robert_agent.configuration.validation import validate_config
from robert_agent.delivery.lock import task_publication_lock
from robert_agent.foundation import values
from robert_agent.foundation.commands import emit
from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.persistence import database
from robert_agent.persistence.queries import proposals
from robert_agent.work.application import proposal_digest, proposal_scan, web_setup


TIMEZONE = ZoneInfo("Asia/Shanghai")
ANALYSIS_TIMEOUT_SECONDS = 3600


class Scheduler:
    def __init__(self, config, config_path):
        self.config = config
        self.config_path = str(config_path)
        self.process = None
        self.started = None
        self.day = None
        self.stopped = False

    def start_analysis(self, day, now):
        with task_publication_lock(self.config["db_path"], "proposal-cron-analysis") as acquired:
            if not acquired or not proposal_digest.claim_analysis(self.config, day, now):
                return False
        self.day = day
        self.started = time.monotonic()
        try:
            self.process = subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "robert_agent.runtime.cron",
                    "--config",
                    self.config_path,
                    "--analysis",
                    "--day",
                    day,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env={**os.environ, "_ROBERT_CONTROL_DEADLINE": str(self.started + ANALYSIS_TIMEOUT_SECONDS - 10)},
            )
        except OSError:
            proposal_digest.finish_analysis(self.config, day, now, {"ok": False, "status": "launch_failed"})
            return False
        return True

    def stop_child(self):
        if self.process is None:
            return
        if self.process.poll() is None:
            # The unreaped direct child retains this process-group identity.
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.process.wait(timeout=3)
        self.process = None

    def step(self, *, now=None):
        now = now or values.utc_now()
        local = utc_timestamp(now).astimezone(TIMEZONE)
        day = local.date().isoformat()
        schedule = self.config["proposer"]["schedule"]
        if self.process is not None:
            outcome = self.process.poll()
            if outcome is not None:
                self.process = None
                with closing(database.connect(self.config["db_path"])) as conn:
                    row = proposals.scan_result(conn, proposal_digest.analysis_slot(self.day))
                if row and values.json_object(row["result_json"]).get("status") == "running":
                    proposal_digest.finish_analysis(self.config, self.day, now, {"ok": False, "status": "child_failed"})
            elif time.monotonic() - self.started >= ANALYSIS_TIMEOUT_SECONDS:
                self.stop_child()
                proposal_digest.finish_analysis(self.config, self.day, now, {"ok": False, "status": "timed_out"})
        clock = local.strftime("%H:%M")
        if clock >= schedule["digest_time"]:
            proposal_digest.enqueue(self.config, day, now, running=self.process is not None)
        if clock >= schedule["analysis_time"] and self.process is None:
            self.start_analysis(day, now)
        return {"ok": True, "status": "running" if self.process is not None else "idle"}


def run_analysis(config, day):
    date.fromisoformat(day)
    with task_publication_lock(config["db_path"], "proposal-cron-analysis") as acquired:
        if not acquired:
            return {"ok": True, "status": "already_running"}
        try:
            result = proposal_scan.scan_daily(config, now=values.utc_now())
        except (ValueError, OSError, subprocess.SubprocessError):
            result = {"ok": False, "status": "analysis_failed"}
        proposal_digest.finish_analysis(config, day, values.utc_now(), result)
        return result


def run(config, config_path, *, once=False):
    with task_publication_lock(config["db_path"], "proposal-cron") as acquired:
        if not acquired:
            return {"ok": False, "status": "already_running"}
        scheduler = Scheduler(config, config_path)
        previous = {}

        def request_stop(signum, frame):
            scheduler.stopped = True

        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, request_stop)
        try:
            while not scheduler.stopped:
                result = scheduler.step()
                if once and scheduler.process is None:
                    return result
                time.sleep(1 if once else 5)
        finally:
            scheduler.stop_child()
            for signum, handler in previous.items():
                signal.signal(signum, handler)
    return {"ok": True, "status": "stopped"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--analysis", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--day", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.analysis and not args.day:
        parser.error("--analysis requires --day")
    config = validate_config(args.config, skip_external=True)
    if not config["ok"]:
        return emit(config, 3)
    if not config["proposer"]["enabled"] or not config["proposer"].get("schedule", {}).get("enabled"):
        return emit({"ok": True, "status": "disabled"})
    web_setup.prepare_repositories(config)
    result = run_analysis(config, args.day) if args.analysis else run(config, args.config, once=args.once)
    return emit(result, 0 if result.get("ok") else 3)


if __name__ == "__main__":
    raise SystemExit(main())
