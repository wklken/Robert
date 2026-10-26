import json
import math
import os
import subprocess
import time


def run_command(command, *, runner=None, **kwargs):
    """Bound control-plane commands by both their cap and the owning child deadline."""
    timeout = kwargs.pop('timeout', None) or 60
    deadline = os.environ.get('_ROBERT_CONTROL_DEADLINE')
    if deadline is not None:
        remaining = float(deadline) - time.monotonic()
        if not math.isfinite(remaining) or remaining <= 0:
            raise subprocess.TimeoutExpired(command, 0)
        timeout = min(timeout, remaining)
    return (runner or subprocess.run)(command, timeout=timeout, **kwargs)


def emit(payload, exit_code=0):
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return exit_code
