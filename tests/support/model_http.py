"""Scripted, credential-free model endpoint for loopback fault injection."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Lock, Thread
import time


@contextmanager
def model_http(routes):
    """Serve path-specific (status, Retry-After) sequences, repeating the last."""
    calls = []
    counts = {}
    lock = Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            with lock:
                index = counts.get(self.path, 0)
                responses = routes.get(self.path, [(404, None)])
                status, retry_after = responses[min(index, len(responses) - 1)]
                counts[self.path] = index + 1
                calls.append(
                    {
                        "path": self.path,
                        "status": status,
                        "at": time.monotonic(),
                        "wall_at": time.time(),
                        "retry_after": retry_after,
                    }
                )
            body = json.dumps({"result": "model fixture"} if status == 200 else {"error": "model fixture"}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if retry_after is not None:
                self.send_header("Retry-After", retry_after)
            self.end_headers()
            self.wfile.write(body)

    class Endpoint:
        def requests(self, path=None):
            with lock:
                return [dict(call) for call in calls if path is None or call["path"] == path]

    endpoint = Endpoint()
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        endpoint.url = f"http://127.0.0.1:{server.server_port}"
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        try:
            yield endpoint
        finally:
            server.shutdown()
            thread.join(timeout=2)
