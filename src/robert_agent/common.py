import json
import math
import os
import subprocess
import time


class ControlDeadlineExceeded(subprocess.TimeoutExpired):
    """The owning control invocation exhausted its shared execution budget."""


def run_command(command, *, runner=None, **kwargs):
    """Bound control-plane commands by both their cap and the owning child deadline."""
    timeout = kwargs.pop('timeout', None)
    deadline = os.environ.get('_ROBERT_CONTROL_DEADLINE')
    deadline_limits_call = False
    if deadline is not None:
        deadline = float(deadline)
        remaining = deadline - time.monotonic()
        if not math.isfinite(remaining) or remaining <= 0:
            raise ControlDeadlineExceeded(command, 0)
        deadline_limits_call = timeout is None or remaining <= timeout
        timeout = remaining if timeout is None else min(timeout, remaining)
    try:
        return (runner or subprocess.run)(command, timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired as exc:
        if deadline_limits_call or (deadline is not None and time.monotonic() >= deadline):
            raise ControlDeadlineExceeded(exc.cmd, timeout, output=exc.output, stderr=exc.stderr) from exc
        raise


def emit(payload, exit_code=0):
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return exit_code
