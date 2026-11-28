from contextlib import contextmanager
from contextvars import ContextVar
import json
import math
import os
import subprocess
import time


class ControlDeadlineExceeded(subprocess.TimeoutExpired):
    """The owning control invocation exhausted its shared execution budget."""


class ScopedDeadlineExceeded(ControlDeadlineExceeded):
    """One part of a control invocation exhausted its command budget."""


_scoped_deadline = ContextVar("command_deadline", default=None)


def control_deadline():
    raw = os.environ.get("_ROBERT_CONTROL_DEADLINE")
    return float(raw) if raw is not None else None


@contextmanager
def limit_command_deadline(deadline):
    previous = _scoped_deadline.get()
    if deadline is None or (previous is not None and previous <= deadline):
        yield
        return
    token = _scoped_deadline.set(deadline)
    try:
        yield
    finally:
        _scoped_deadline.reset(token)


def run_command(command, *, runner=None, **kwargs):
    """Bound a command by its own cap and the active control deadlines."""
    timeout = kwargs.pop("timeout", None)
    owner_deadline = control_deadline()
    scoped_deadline = _scoped_deadline.get()
    deadline = owner_deadline
    deadline_error = ControlDeadlineExceeded
    if scoped_deadline is not None and (deadline is None or scoped_deadline < deadline):
        deadline = scoped_deadline
        deadline_error = ScopedDeadlineExceeded
    deadline_limits_call = False
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if not math.isfinite(remaining) or remaining <= 0:
            if owner_deadline is None or time.monotonic() < owner_deadline:
                raise deadline_error(command, 0)
            raise ControlDeadlineExceeded(command, 0)
        deadline_limits_call = timeout is None or remaining <= timeout
        timeout = remaining if timeout is None else min(timeout, remaining)
    try:
        return (runner or subprocess.run)(command, timeout=timeout, **kwargs)
    except ControlDeadlineExceeded:
        raise
    except subprocess.TimeoutExpired as exc:
        if owner_deadline is not None and time.monotonic() >= owner_deadline:
            raise ControlDeadlineExceeded(exc.cmd, timeout, output=exc.output, stderr=exc.stderr) from exc
        if deadline_limits_call or (deadline is not None and time.monotonic() >= deadline):
            raise deadline_error(exc.cmd, timeout, output=exc.output, stderr=exc.stderr) from exc
        raise


def emit(payload, exit_code=0):
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return exit_code
