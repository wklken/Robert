"""JSON requests to the offline fixture and loopback workbench."""

import json
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener


FIXTURE_URL = "http://fixtures:8765"
WEB_URL = "http://127.0.0.1:8765"
_OPENER = build_opener(ProxyHandler({}))


def request(url, payload=None, *, headers=None, timeout=10):
    headers = dict(headers or {})
    data = None
    if payload is not None:
        headers.setdefault("Content-Type", "application/json")
        data = json.dumps(payload).encode()
    req = Request(url, data=data, headers=headers)
    try:
        with _OPENER.open(req, timeout=timeout) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        with exc:
            return exc.code, json.load(exc)


def fixture(path, payload=None, *, timeout=10):
    status, result = request(FIXTURE_URL + path, payload, timeout=timeout)
    if not 200 <= status < 300:
        raise RuntimeError(f"fixture {path} returned {status}: {result}")
    return result
