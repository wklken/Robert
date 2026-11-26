"""Bounded Issue creation and remote recovery reads through gh."""

from datetime import datetime
import json
import os
import re
from urllib.parse import urlencode, urlsplit

from robert_agent.foundation.commands import run_command
from robert_agent.foundation.timestamps import utc_timestamp


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


def _number(number):
    if type(number) is not int or number <= 0:
        raise ValueError("invalid Issue number")
    return str(number)


def _candidate(repo, value):
    if not isinstance(value, dict) or not isinstance(value.get("user"), dict):
        raise ValueError("GitHub returned incomplete Issue metadata")
    value = _issue(repo, value)
    assignees = value.get("assignees")
    if (
        not isinstance(assignees, list)
        or any(
            not isinstance(actor, dict) or not isinstance(actor.get("login"), str) or not actor["login"]
            for actor in assignees
        )
        or value.get("state") not in {"open", "closed"}
        or not isinstance(value.get("title"), str)
        or value.get("body") is not None
        and not isinstance(value.get("body"), str)
        or utc_timestamp(value.get("created_at")) is None
    ):
        raise ValueError("GitHub returned incomplete Issue metadata")
    return value


def _window(since, until):
    if (
        not isinstance(since, datetime)
        or not isinstance(until, datetime)
        or since.utcoffset() is None
        or until.utcoffset() is None
        or since > until
    ):
        raise ValueError("invalid Issue discovery interval")


def _pages(endpoint, max_pages, runner):
    if type(max_pages) is not int or not 1 <= max_pages <= 100:
        raise ValueError("invalid page limit")
    separator = "&" if "?" in endpoint else "?"
    for number in range(1, max_pages + 1):
        rows = _json(["gh", "api", f"{endpoint}{separator}per_page=100&page={number}"], runner)
        if not isinstance(rows, list) or len(rows) > 100 or any(not isinstance(row, dict) for row in rows):
            raise ValueError("GitHub returned invalid Issue page")
        yield rows
        if len(rows) < 100:
            return
    raise ValueError("GitHub Issue scan page limit reached")


def list_recent(repo, *, since, until, max_pages=10, runner=None):
    """Read complete created-desc Issue pages within an aware, inclusive interval."""
    _window(since, until)
    found, seen = [], set()
    previous = None
    pages = _pages(_endpoint(repo) + "?state=open&sort=created&direction=desc", max_pages, runner)
    for rows in pages:
        older = False
        for raw in rows:
            created = utc_timestamp(raw.get("created_at"))
            if created is None or previous is not None and created > previous:
                raise ValueError("GitHub returned invalid Issue ordering")
            previous = created
            if created < since:
                older = True
                continue
            if "pull_request" in raw:
                continue
            row = _candidate(repo, raw)
            if row["id"] in seen:
                raise ValueError("GitHub returned duplicate Issue")
            seen.add(row["id"])
            if created <= until and row["state"] == "open":
                found.append(row)
        if older:
            pages.close()
            break
    return tuple(found)


def get(repo, number, *, runner=None):
    """Fetch current state and assignees for one existing Issue."""
    row = _candidate(repo, _json(["gh", "api", f"{_endpoint(repo)}/{_number(number)}"], runner))
    if row["number"] != number:
        raise ValueError("GitHub returned another Issue")
    return row


def discussion(repo, number, *, max_pages=10, exclude_author=None, include_author_results=False, runner=None):
    """Read bounded, complete comment bodies in their server order."""
    bodies, seen = [], set()
    endpoint = f"{_endpoint(repo)}/{_number(number)}/comments"
    for rows in _pages(endpoint, max_pages, runner):
        for row in rows:
            if type(row.get("id")) is not int or row["id"] in seen or not isinstance(row.get("body"), str):
                raise ValueError("GitHub returned invalid Issue comment")
            seen.add(row["id"])
            actor = row.get("user")
            login = actor.get("login") if isinstance(actor, dict) else None
            if exclude_author and isinstance(login, str) and login.casefold() == exclude_author.casefold():
                if not include_author_results or "<!-- robert-comment task_id=" not in row["body"]:
                    continue
            bodies.append(row["body"])
    return tuple(bodies)


def has_open_pull_request(repo, number, *, runner=None):
    """Read linked closing PRs; incomplete evidence never establishes availability."""
    _endpoint(repo)
    _number(number)
    owner, name = repo.split("/", 1)
    query = """query($owner:String!,$name:String!,$number:Int!){
      repository(owner:$owner,name:$name){issue(number:$number){
        closedByPullRequestsReferences(first:100,includeClosedPrs:true){
          nodes{state} pageInfo{hasNextPage}
        }
      }}
    }"""
    result = _json(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            "query=" + query,
            "-f",
            "owner=" + owner,
            "-f",
            "name=" + name,
            "-F",
            "number=" + str(number),
        ],
        runner,
    )
    try:
        connection = result["data"]["repository"]["issue"]["closedByPullRequestsReferences"]
        nodes = connection["nodes"]
        complete = connection["pageInfo"]["hasNextPage"] is False
    except (TypeError, KeyError):
        raise ValueError("GitHub returned incomplete linked pull request metadata") from None
    if (
        result.get("errors")
        or not complete
        or not isinstance(nodes, list)
        or any(not isinstance(node, dict) or node.get("state") not in {"OPEN", "CLOSED", "MERGED"} for node in nodes)
    ):
        raise ValueError("GitHub returned incomplete linked pull request metadata")
    return any(node["state"] == "OPEN" for node in nodes)
