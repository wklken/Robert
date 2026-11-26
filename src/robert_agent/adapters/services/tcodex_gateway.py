"""Discover the loopback Responses proxy of an isolated tcodex app-server."""

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
import selectors
import subprocess
import tempfile
import time


MAX_CONFIG_BYTES = 262144
CONFIG_REQUEST_ID = 1


class GatewayStartupError(ValueError):
    pass


@dataclass(frozen=True)
class TcodexGateway:
    executable: str
    home: str


def _provider_url(stream, deadline):
    pending = b""
    total = 0
    with selectors.DefaultSelector() as selector:
        selector.register(stream, selectors.EVENT_READ)
        while selector.select(max(0, deadline - time.monotonic())) and time.monotonic() < deadline:
            chunk = stream.read(8192)
            total += len(chunk)
            if not chunk or total > MAX_CONFIG_BYTES:
                break
            *lines, pending = (pending + chunk).split(b"\n")
            for line in lines:
                message = json.loads(line)
                if message.get("id") != CONFIG_REQUEST_ID:
                    continue
                config = message["result"]["config"]
                provider = config["model_providers"][config["model_provider"]]
                if provider["wire_api"] == "responses":
                    return provider["base_url"]
                raise GatewayStartupError("tcodex provider does not use Responses")
    raise GatewayStartupError("tcodex gateway endpoint unavailable")


@contextmanager
def open_gateway(gateway, deadline):
    """Keep the proxy alive for one HTTP call without starting an agent session."""
    requests = (
        {"id": 0, "method": "initialize", "params": {"clientInfo": {"name": "robert", "version": "1"}}},
        {"method": "initialized"},
        {"id": CONFIG_REQUEST_ID, "method": "config/read", "params": {"includeLayers": False}},
    )
    with tempfile.TemporaryDirectory(prefix="robert-tcodex-") as cwd:
        try:
            process = subprocess.Popen(
                [gateway.executable, "app-server", "--listen", "stdio://"],
                cwd=cwd,
                env={"HOME": gateway.home, "PATH": os.defpath, "TMPDIR": cwd},
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError:
            raise GatewayStartupError("tcodex gateway could not start") from None
        try:
            try:
                process.stdin.write("".join(json.dumps(request) + "\n" for request in requests).encode())
                endpoint = _provider_url(process.stdout, deadline)
            except GatewayStartupError:
                raise
            except (AttributeError, KeyError, OSError, TypeError, ValueError, RecursionError):
                raise GatewayStartupError("invalid tcodex gateway configuration") from None
            yield endpoint
        finally:
            process.stdin.close()
            process.stdout.close()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
