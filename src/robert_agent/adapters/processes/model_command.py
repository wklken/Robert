"""Bounded no-tool model command transport with owned process cleanup."""

import os
from pathlib import Path
import selectors
import subprocess
import tempfile
import time

from robert_agent.adapters.processes import identity as worker_identity
from robert_agent.worker_adapters import build_worker_launch

MAX_STDOUT = 8192


def read_output(proc, deadline, stop):
    output = bytearray()
    eof = False
    with selectors.DefaultSelector() as selector:
        descriptor = proc.stdout.fileno()
        os.set_blocking(descriptor, False)
        selector.register(descriptor, selectors.EVENT_READ)
        while not stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timed_out", b""
            if eof:
                if proc.poll() is not None:
                    return ("exited" if proc.returncode else "read"), bytes(output)
                stop.wait(min(remaining, 0.05))
            elif selector.select(min(remaining, 0.05)):
                chunk = os.read(descriptor, min(4096, MAX_STDOUT + 1 - len(output)))
                if chunk:
                    output.extend(chunk)
                    if len(output) > MAX_STDOUT:
                        return "invalid_output", b""
                else:
                    eof = True
    return "stopped", b""


def stable_identity(proc, stop):
    # On macOS `ps` can observe the exec transition before argv stabilizes.
    # Never persist that transient command as proof for restart or cleanup.
    previous = None
    for _ in range(5):
        if proc.poll() is not None:
            return None
        try:
            observed = worker_identity.capture_process_identity(proc.pid)
        except (OSError, worker_identity.IdentityReadError):
            observed = None
        if observed and previous and worker_identity.compare_recorded_identity(previous, observed) == "alive_same":
            return observed
        previous = observed
        if stop.wait(0.025):
            return None
    if proc.poll() is None:
        raise ValueError("diagnosis process identity did not stabilize")
    return None


def run(profile, payload, stop, on_started):
    """Return bounded output after the process group exits; persist identity at spawn."""
    proc, stopped, output, status = None, True, b"", "stopped"
    try:
        with tempfile.TemporaryDirectory(prefix="robert-model-", ignore_cleanup_errors=True) as root:
            cwd = Path(root).resolve()
            source = cwd / "request.json"
            source.write_text(payload, encoding="utf-8")
            launch = build_worker_launch(source, cwd, worker_agent_config={"agent": "command", **profile})
            environment = {key: os.environ[key] for key in launch.environment_allowlist if key in os.environ}
            environment.update(HOME=str(cwd), TMPDIR=str(cwd), PATH=os.defpath)
            if stop.is_set():
                return {"status": status, "output": output, "stopped": stopped}
            try:
                with source.open("rb") as input_stream:
                    proc = subprocess.Popen(
                        launch.command,
                        cwd=launch.cwd,
                        env=environment,
                        stdin=input_stream,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                identity = stable_identity(proc, stop)
                if identity:
                    if identity["process_group_id"] != proc.pid or not on_started(identity):
                        raise ValueError("model process identity or claim changed")
                status, output = read_output(proc, time.monotonic() + launch.timeout_seconds, stop)
            finally:
                if proc is not None:
                    proc.stdout.close()
                    try:
                        stopped = proc.poll() is not None and not worker_identity.live_process_group(proc.pid)
                    except (OSError, subprocess.TimeoutExpired):
                        stopped = False
                    if not stopped:
                        stopped = worker_identity.cleanup_new_process(proc, wait_timeout=0.5)
    except (OSError, ValueError, subprocess.SubprocessError):
        status = "launch_failed" if proc is None else "process_attention"
    if not stopped:
        status, output = "process_attention", b""
    return {"status": status, "output": output, "stopped": stopped}
