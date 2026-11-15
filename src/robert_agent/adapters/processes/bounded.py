"""Run short controller commands with interruptible, size-bounded I/O."""

import os
import selectors
import subprocess
import time

from robert_agent.adapters.processes.identity import cleanup_new_process


class BoundedCommandError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def run_bounded(command, *, cwd, env, input, timeout, max_output_bytes, stop):
    deadline = time.monotonic() + timeout
    pending = memoryview(input or b"")
    output = bytearray()
    stderr_size = 0

    with subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE if pending else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    ) as child:
        try:
            with selectors.DefaultSelector() as selector:
                for stream in (child.stdin, child.stdout, child.stderr):
                    if stream is not None:
                        os.set_blocking(stream.fileno(), False)
                        events = selectors.EVENT_WRITE if stream is child.stdin else selectors.EVENT_READ
                        selector.register(stream, events)

                while True:
                    if stop is not None and stop.is_set():
                        raise BoundedCommandError("cancelled")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise BoundedCommandError("timeout")
                    if not selector.get_map() and child.poll() is not None:
                        return subprocess.CompletedProcess(command, child.returncode, bytes(output), b"")
                    for key, _events in selector.select(min(remaining, 0.05)):
                        stream = key.fileobj
                        try:
                            if stream is child.stdin:
                                pending = pending[os.write(key.fd, pending[:4096]) :]
                                if pending:
                                    continue
                            else:
                                chunk = os.read(key.fd, min(4096, max_output_bytes + 1 - len(output) - stderr_size))
                                if chunk:
                                    if stream is child.stdout:
                                        output.extend(chunk)
                                    else:
                                        stderr_size += len(chunk)
                                    if len(output) + stderr_size > max_output_bytes:
                                        raise BoundedCommandError("output_limit")
                                    continue
                        except BlockingIOError:
                            continue
                        except BrokenPipeError:
                            pass
                        selector.unregister(stream)
                        stream.close()
        except BaseException:
            if not cleanup_new_process(child, wait_timeout=0.25):
                raise BoundedCommandError("process_cleanup_unknown") from None
            raise
