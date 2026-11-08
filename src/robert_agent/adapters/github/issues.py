"""Bounded Issue creation and remote recovery reads through gh."""

import json
import os
import re
from urllib.parse import urlencode, urlsplit

from robert_agent.foundation.commands import run_command


def _endpoint(repo):
    if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("invalid repository")
    return f"repos/{repo}/issues"


def _json(argv, runner, **kwargs):
    result = run_command(argv, runner=runner, capture_output=True, text=True, check=True, timeout=30, **kwargs)
    return json.loads(result.stdout)


def _issue(repo, value):
    if not isinstance(value, dict) or "pull_request" in value:
        raise ValueError("GitHub returned invalid Issue metadata")
    number = value.get("number")
    url = urlsplit(value.get("html_url") or "")
    if (
        type(number) is not int
        or number <= 0
        or type(value.get("id")) is not int
        or not (value.get("user") or {}).get("login")
        or url.scheme != "https"
        or url.netloc != os.environ.get("GH_HOST", "github.com")
        or url.path.casefold() != f"/{repo}/issues/{number}".casefold()
    ):
        raise ValueError("GitHub returned invalid Issue metadata")
    return value


def create(repo, *, title, body, runner=None):
    value = _json(
        ["gh", "api", _endpoint(repo), "--method", "POST", "--input", "-"],
        runner,
        input=json.dumps({"title": title, "body": body}, ensure_ascii=False),
    )
    return _issue(repo, value)


def check_identity(expected, *, runner=None):
    value = _json(["gh", "api", "user"], runner)
    if not isinstance(value, dict) or str(value.get("login", "")).casefold() != expected.casefold():
        raise ValueError("GitHub CLI identity does not match the configured account")
    return value["login"]


def find_created(repo, request_id, actor, since, *, runner=None):
    endpoint = _endpoint(repo) + "?" + urlencode({"state": "all", "since": since, "per_page": 100})
    pages = _json(["gh", "api", endpoint, "--paginate", "--slurp"], runner)
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise ValueError("GitHub returned incomplete Issue pages")
    marker = f"<!-- robert-dispatch request_id={request_id} -->"
    matches = []
    for page in pages:
        for value in page:
            if not isinstance(value, dict):
                raise ValueError("GitHub returned invalid Issue page")
            if (
                "pull_request" not in value
                and marker in (value.get("body") or "")
                and (value.get("user") or {}).get("login", "").casefold() == actor.casefold()
            ):
                matches.append(_issue(repo, value))
    # An empty result is deliberately not proof of absence after an uncertain POST.
    return matches
