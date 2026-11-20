"""A strict, scenario-local GitHub boundary for the installed-wheel tests."""

import argparse
from dataclasses import dataclass, field
import hashlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import socket
import subprocess
import time
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

if __package__:
    from .fixture_store import FixtureStore, last_modified
else:
    from fixture_store import FixtureStore, last_modified


_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_COMMENT_QUERY = (
    "query($id:ID!){node(id:$id){... on IssueComment{id body author{login} editor{login} lastEditedAt updatedAt}}}"
)
_SEARCH_FIELDS = {"number", "title", "body", "author", "authorAssociation", "isPullRequest", "updatedAt", "url"}
_PR_FIELDS = {"number", "url", "body", "baseRefName", "headRefName", "headRepositoryOwner"}


@dataclass
class Call:
    operation: str
    entity: dict = field(default_factory=dict)
    options: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)
    fields: dict = field(default_factory=dict)
    write: bool = False

    def evidence(self, payload):
        return {
            "request_id": uuid4().hex,
            "operation": self.operation,
            "entity": self.entity,
            "argv": payload["argv"],
            "actor": payload.get("actor", {}),
        }


def _args(argv, values, switches=()):
    result = {}
    index = 0
    while index < len(argv):
        flag = argv[index]
        if flag in result:
            raise ValueError(f"duplicate gh flag: {flag}")
        if flag in switches:
            result[flag] = True
            index += 1
        elif flag in values and index + 1 < len(argv):
            result[flag] = argv[index + 1]
            index += 2
        else:
            raise ValueError(f"unsupported gh argument: {flag}")
    return result


def _repo(value):
    if not _REPOSITORY.fullmatch(value) or any(part in {".", ".."} for part in value.split("/")):
        raise ValueError("invalid fixture repository")
    return value


def _select(option, accepted):
    columns = option.split(",")
    if len(columns) != len(set(columns)) or not set(columns) <= accepted:
        raise ValueError("unsupported gh JSON fields")
    return columns


def _options_for_api(argv):
    fields = {}
    headers = {}
    retained = []
    index = 0
    while index < len(argv):
        flag = argv[index]
        if flag in {"-f", "--raw-field", "-H", "--header"}:
            if index + 1 >= len(argv):
                raise ValueError(f"missing value for {flag}")
            target, delimiter = (fields, "=") if flag in {"-f", "--raw-field"} else (headers, ":")
            key, separator, value = argv[index + 1].partition(delimiter)
            key = key.lower() if target is headers else key
            if not key or not separator or key in target:
                raise ValueError(f"invalid or duplicate {flag} argument")
            target[key] = value.strip() if target is headers else value
            index += 2
        elif flag in {"--hostname", "--method", "-X"} and index + 1 < len(argv):
            retained.extend(argv[index : index + 2])
            index += 2
        else:
            retained.append(flag)
            index += 1
    options = _args(retained, {"--hostname", "--method", "-X"}, {"--include", "--paginate", "--slurp"})
    if options.get("--hostname", "github.com") != "github.com":
        raise ValueError("unsupported gh host")
    if ("--slurp" in options) != ("--paginate" in options):
        raise ValueError("fixture requires --paginate and --slurp together")
    if "--method" in options and "-X" in options:
        raise ValueError("duplicate gh method")
    return options, fields, headers


def _endpoint(path, method):
    if path in {"rate_limit", "notifications", "graphql"}:
        allowed = "POST" if path == "graphql" else "GET"
        if method != allowed:
            raise ValueError(f"unsupported method for {path}")
        return path, {}
    match = re.fullmatch(r"repos/([^/]+/[^/]+)/(.*)", path)
    if not match:
        raise ValueError(f"unsupported gh endpoint: {path}")
    repo, remainder = match.groups()
    entity = {"repo": _repo(repo)}
    if match := re.fullmatch(r"(issues|pulls)/(\d+)(?:/(comments|timeline|reviews))?", remainder):
        category, number, tail = match.groups()
        entity["number"] = int(number)
        operations = {
            ("issues", None, "GET"): "issue",
            ("pulls", None, "GET"): "pull",
            ("issues", "comments", "GET"): "issue_comments",
            ("issues", "comments", "POST"): "comment",
            ("issues", "timeline", "GET"): "timeline",
            ("pulls", "comments", "GET"): "review_comments",
            ("pulls", "reviews", "GET"): "reviews",
            ("pulls", "reviews", "POST"): "pr_review",
        }
        if operation := operations.get((category, tail, method)):
            return operation, entity
    if method == "GET":
        reads = (
            (r"collaborators/([^/]+)/permission", "permission", "login"),
            (r"issues/comments/(\d+)", "issue_comment", "comment_id"),
            (r"commits/([a-f0-9]{40,64})/check-runs", "check_runs", "sha"),
            (r"check-runs/(\d+)/annotations", "annotations", "id"),
            (r"actions/runs/(\d+)/jobs", "jobs", "id"),
            (r"actions/jobs/(\d+)/logs", "job_logs", "id"),
        )
        if remainder == "actions/runs":
            return "workflow_runs", entity
        for pattern, operation, key in reads:
            if match := re.fullmatch(pattern, remainder):
                return operation, {**entity, key: match[1]}
    raise ValueError(f"unsupported gh API method or endpoint: {method} {path}")


def _parse_api(argv):
    if not argv:
        raise ValueError("missing gh api endpoint")
    parsed = urlsplit(argv[0])
    if (
        (parsed.scheme or parsed.netloc) and (parsed.scheme, parsed.netloc) != ("https", "api.github.com")
    ) or parsed.fragment:
        raise ValueError("unsupported gh API URL")
    options, fields, headers = _options_for_api(argv[1:])
    method = options.get("--method") or options.get("-X") or ("POST" if fields else "GET")
    if method not in {"GET", "POST"}:
        raise ValueError("unsupported gh API method")
    operation, entity = _endpoint(parsed.path.lstrip("/"), method)
    params = {}
    allowed_params = {"per_page", "page"}
    if operation == "workflow_runs":
        allowed_params.add("head_sha")
    if parsed.query and operation not in {
        "notifications",
        "issue_comments",
        "review_comments",
        "reviews",
        "timeline",
        "workflow_runs",
        "check_runs",
        "annotations",
        "jobs",
    }:
        raise ValueError("unsupported gh query on this endpoint")
    for key, values in parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True).items():
        if key not in allowed_params or len(values) != 1:
            raise ValueError(f"unsupported gh query parameter: {key}")
        if key in {"per_page", "page"}:
            value = int(values[0])
            if value < 1 or (key == "per_page" and value > 100):
                raise ValueError(f"invalid gh page value: {key}")
        else:
            value = values[0]
        params[key] = value
    if operation == "notifications":
        entity["page"] = params.get("page", 1)
    if headers and (operation != "notifications" or set(headers) != {"if-modified-since"}):
        raise ValueError("unsupported gh request headers")
    if "--include" in options and operation != "notifications":
        raise ValueError("unsupported gh include")
    if "--paginate" in options and operation not in {
        "notifications",
        "issue_comments",
        "review_comments",
        "reviews",
        "timeline",
    }:
        raise ValueError("unsupported gh pagination")
    expected = {
        "comment": {"body"},
        "pr_review": {"body", "event", "commit_id"},
        "graphql": {"query", "id"},
    }.get(operation, set())
    if set(fields) != expected:
        raise ValueError(f"unsupported gh request fields for {operation}")
    if operation == "graphql":
        if fields["query"] != _COMMENT_QUERY:
            raise ValueError("unsupported GraphQL query")
        entity["node_id"] = fields["id"]
    if operation == "pr_review" and fields["event"] != "COMMENT":
        raise ValueError("unsupported PR review event")
    if "body" in fields:
        entity["body_sha256"] = hashlib.sha256(fields["body"].encode()).hexdigest()
    return Call(operation, entity, options, params, headers, fields, method == "POST" and operation != "graphql")


def parse_call(payload):
    argv = payload["argv"]
    if not isinstance(argv, list) or any(not isinstance(value, str) for value in argv):
        raise ValueError("invalid gh argv")
    if argv in (["auth", "status"], ["--version"]):
        return Call("auth" if argv[0] == "auth" else "version")
    if argv[:1] == ["api"]:
        return _parse_api(argv[1:])
    if argv[:2] == ["search", "issues"]:
        options = _args(
            argv[2:], {"--repo", "--assignee", "--mentions", "--state", "--json", "--limit"}, {"--include-prs"}
        )
        if (
            not options.get("--repo")
            or options.get("--state") != "open"
            or ("--assignee" in options) == ("--mentions" in options)
            or options.get("--assignee", "@me") != "@me"
            or not 1 <= int(options.get("--limit", "30")) <= 1000
        ):
            raise ValueError("unsupported gh search")
        options["columns"] = _select(options.get("--json", ""), _SEARCH_FIELDS)
        return Call("search", {"repo": _repo(options["--repo"])}, options)
    if argv[:1] == ["pr"] and len(argv) >= 2:
        action = argv[1]
        if action == "list":
            options = _args(argv[2:], {"--repo", "--head", "--state", "--json"})
            if options.get("--state") != "open" or not options.get("--head"):
                raise ValueError("unsupported PR list")
            options["columns"] = _select(options.get("--json", ""), _PR_FIELDS)
            return Call("pr_list", {"repo": _repo(options["--repo"]), "branch": options["--head"]}, options)
        if action == "view" and len(argv) >= 3:
            options = _args(argv[3:], {"--repo", "--json"})
            repo = _repo(options["--repo"])
            target = re.fullmatch(rf"(?:https://github\.com/{re.escape(repo)}/pull/)?(\d+)", argv[2])
            if not target:
                raise ValueError("unsupported PR view target")
            options["columns"] = _select(options.get("--json", ""), _PR_FIELDS)
            return Call("pr_view", {"repo": repo, "number": int(target[1])}, options)
        if action == "create":
            options = _args(argv[2:], {"--repo", "--head", "--base", "--title", "--body", "--body-file"}, {"--draft"})
            if not all(options.get(key) for key in ("--repo", "--head", "--base", "--title")):
                raise ValueError("missing PR create fields")
            if ("--body" in options) == ("--body-file" in options):
                raise ValueError("PR create requires one body")
            body = options["--body"] if "--body" in options else payload["body_file"]
            owner, separator, branch = options["--head"].partition(":")
            repo = _repo(options["--repo"])
            entity = {
                "repo": repo,
                "branch": branch if separator else owner,
                "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
            }
            return Call("pr_create", entity, options, fields={"body": body}, write=True)
    raise ValueError("unsupported gh command")


def _result(stdout="", stderr="", exit_code=0):
    return {"stdout": stdout, "stderr": stderr, "exit_code": exit_code}


def _http(call, status, body=None, headers=None):
    reason = HTTPStatus(status).phrase if status in HTTPStatus._value2member_map_ else "Fixture"
    headers = headers or {}
    prefix = ""
    if call.options.get("--include"):
        prefix = f"HTTP/1.1 {status} {reason}\r\n"
        prefix += "".join(f"{name}: {value}\r\n" for name, value in headers.items()) + "\r\n"
    stdout = prefix + (json.dumps(body, ensure_ascii=False) + "\n" if body is not None else "")
    if status >= 300:
        details = "".join(f"\n{name}: {value}" for name, value in headers.items())
        return _result(stdout, f"gh: {reason} (HTTP {status}){details}\n", 1)
    return _result(stdout)


def _pages(items, size):
    return [items[index : index + size] for index in range(0, len(items), size)] or [[]]


def _page_payload(call, items, size=None):
    pages = _pages(items, size or call.params.get("per_page", 30))
    if call.options.get("--paginate"):
        return pages
    index = call.params.get("page", 1) - 1
    return pages[index] if index < len(pages) else []


def _base_source(world, repo, number, raw, pull=False):
    timestamp = world["_timestamp"]
    url = f"https://github.com/{repo}/{'pull' if pull else 'issues'}/{number}"
    return {
        "id": number,
        "number": number,
        "title": f"Fixture {'PR' if pull else 'issue'} {number}",
        "body": "",
        "state": "open",
        "html_url": url,
        "author_association": "NONE",
        "created_at": timestamp,
        "updated_at": timestamp,
        "assignees": [],
        **{k: v for k, v in raw.items() if k not in {"comments", "review_comments", "timeline", "reviews"}},
    }


def _source(repository, number, *, pull=False):
    number = str(number)
    pulls = repository.get("pulls", {})
    if pull or number in pulls:
        return pulls.get(number), True
    return repository.get("issues", {}).get(number), False


def _ref(git_root, repo, branch):
    if git_root is None or not branch:
        return None
    path = (git_root / (repo + ".git")).resolve()
    if not path.is_relative_to(git_root) or not path.is_dir():
        return None
    completed = subprocess.run(
        ["git", "--git-dir", str(path), "rev-parse", "--verify", "--end-of-options", "refs/heads/" + branch],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _pr_ref(git_root, repo, raw, default):
    branch = raw.get("ref", default)
    repository = raw.get("repo", {"full_name": repo})
    name = repository.get("full_name", repo)
    return {**raw, "ref": branch, "repo": repository, "sha": raw.get("sha") or _ref(git_root, name, branch)}


def _pull(world, git_root, repo, number, raw):
    owner = repo.split("/")[0]
    head = _pr_ref(git_root, repo, raw.get("head", {}), f"fixture-{number}")
    base = _pr_ref(git_root, repo, raw.get("base", {}), "main")
    return {
        "merged": bool(raw.get("merged_at")),
        "merged_at": None,
        "mergeable": True,
        **_base_source(world, repo, number, raw, pull=True),
        "head": {**head, "label": head.get("label", f"{owner}:{head['ref']}")},
        "base": base,
    }


def _pr_view(world, git_root, repo, number, raw, columns):
    pull = _pull(world, git_root, repo, number, raw)
    value = {
        "number": number,
        "url": pull["html_url"],
        "body": pull["body"],
        "baseRefName": pull["base"]["ref"],
        "headRefName": pull["head"]["ref"],
        "headRepositoryOwner": {"login": pull["head"]["repo"]["full_name"].split("/")[0]},
    }
    return {key: value[key] for key in columns}


def _search(call, world, repository):
    options = call.options
    matches = []
    for category in ("issues", "pulls"):
        if category == "pulls" and not options.get("--include-prs"):
            continue
        for number, raw in repository.get(category, {}).items():
            source = _base_source(world, call.entity["repo"], int(number), raw, category == "pulls")
            if source["state"] != "open":
                continue
            if "--assignee" in options:
                found = any(user["login"] == world["account"] for user in source["assignees"])
            else:
                mention = re.compile(rf"(?<![\w-])@{re.escape(options['--mentions'])}(?![\w-])", re.I)
                found = any(mention.search(item.get("body") or "") for item in [source, *raw.get("comments", [])])
            if not found:
                continue
            value = {
                "number": source["number"],
                "title": source["title"],
                "body": source["body"],
                "author": source.get("user"),
                "authorAssociation": source["author_association"],
                "isPullRequest": category == "pulls",
                "updatedAt": source["updated_at"],
                "url": source["html_url"],
            }
            matches.append({key: value[key] for key in options["columns"]})
    return matches[: int(options.get("--limit", "30"))]


def _notification(call, world):
    last = last_modified(world)
    headers = {"Last-Modified": last, "X-Poll-Interval": str(world.get("notification_poll_interval", 1))}
    if call.headers.get("if-modified-since") == last:
        return _http(call, 304, headers=headers)
    size = world.get("notification_page_size", call.params.get("per_page", 50))
    pages = _pages(world["notifications"], size)
    page = call.params.get("page", 1)
    if page < len(pages):
        query = urlencode({"per_page": call.params.get("per_page", 50), "page": page + 1})
        headers["Link"] = f'<https://api.github.com/notifications?{query}>; rel="next"'
    return _http(call, 200, _page_payload(call, world["notifications"], size), headers)


def _comment_node(world, node_id):
    if node_id in world.get("nodes", {}):
        return world["nodes"][node_id]
    for repo in world["repositories"].values():
        for category in ("issues", "pulls"):
            for source in repo.get(category, {}).values():
                for comment in source.get("comments", []):
                    if comment.get("node_id") == node_id:
                        created = comment.get("created_at", world["_timestamp"])
                        updated = comment.get("updated_at", created)
                        return {
                            "id": node_id,
                            "body": comment.get("body", ""),
                            "author": comment.get("user"),
                            "editor": comment.get("editor"),
                            "lastEditedAt": comment.get("last_edited_at") or (updated if created != updated else None),
                            "updatedAt": updated,
                        }
    return None


def _next_number(repository):
    return max((int(key) for kind in ("issues", "pulls") for key in repository.get(kind, {})), default=0) + 1


def _record_write(call, world, repository, raw, git_root):
    op = call.operation
    repo = call.entity["repo"]
    account = {"login": world["account"]}
    if op == "pr_create":
        number = _next_number(repository)
        data = {"user": account, "body": call.fields["body"], "author_association": "MEMBER"}
        head = call.options["--head"]
        owner, sep, branch = head.partition(":")
        head_repo = f"{owner}/{repo.split('/')[1]}" if sep else repo
        branch = branch if sep else owner
        base = call.options["--base"]
        if not _ref(git_root, head_repo, branch) or not _ref(git_root, repo, base):
            return _http(call, 422, {"message": "Missing real fixture Git head or base ref"}), None
        data.update(
            title=call.options["--title"],
            draft=bool(call.options.get("--draft")),
            head={"ref": branch, "repo": {"full_name": head_repo}},
            base={"ref": base, "repo": {"full_name": repo}},
        )
        repository.setdefault("pulls", {})[str(number)] = data
        value = _pull(world, git_root, repo, number, data)
        return _result(value["html_url"] + "\n"), value
    number = call.entity["number"]
    comment_id = world["_next_id"]
    world["_next_id"] += 1
    data = {
        "id": comment_id,
        "body": call.fields["body"],
        "user": account,
        "created_at": world["_timestamp"],
        "updated_at": world["_timestamp"],
        "author_association": "MEMBER",
    }
    source_url = f"https://github.com/{repo}"
    if op == "comment":
        data["html_url"] = f"{source_url}/issues/{number}#issuecomment-{comment_id}"
        data["node_id"] = f"fixture-comment-{comment_id}"
        raw.setdefault("comments", []).append(data)
    else:
        data.update(
            html_url=f"{source_url}/pull/{number}#pullrequestreview-{comment_id}",
            commit_id=call.fields["commit_id"],
            state="COMMENTED",
            submitted_at=world["_timestamp"],
        )
        raw.setdefault("reviews", []).append(data)
    return _http(call, 201, data), data


def _read_repository(call, world, repository, git_root):
    op = call.operation
    repo = call.entity["repo"]
    entity = call.entity
    if op == "search":
        return _http(call, 200, _search(call, world, repository))
    if op == "pr_list":
        values = [
            _pr_view(world, git_root, repo, int(n), raw, call.options["columns"])
            for n, raw in repository.get("pulls", {}).items()
            if raw.get("state", "open") == "open" and raw.get("head", {}).get("ref") == entity["branch"]
        ]
        return _http(call, 200, values)
    if op == "permission":
        permission = repository.get("permissions", {}).get(entity["login"], "none")
        return _http(call, 200, {"permission": permission, "user": {"login": entity["login"]}})
    if op == "workflow_runs":
        runs = repository.get("workflow_runs", [])
        if call.params.get("head_sha"):
            runs = [run for run in runs if run.get("head_sha") == call.params["head_sha"]]
        return _http(call, 200, {"workflow_runs": _page_payload(call, runs)})
    if op == "check_runs":
        checks = repository.get("check_runs", {}).get(entity["sha"], [])
        return _http(call, 200, {"check_runs": _page_payload(call, checks)})
    if op in {"jobs", "annotations"}:
        items = _page_payload(call, repository.get(op, {}).get(entity["id"], []))
        return _http(call, 200, {"jobs": items} if op == "jobs" else items)
    if op == "job_logs":
        value = repository.get("job_logs", {}).get(entity["id"])
        return _result(value) if value is not None else _http(call, 404)
    if op == "issue_comment":
        for category in ("issues", "pulls"):
            for raw in repository.get(category, {}).values():
                for item in raw.get("comments", []):
                    if str(item.get("id")) == entity["comment_id"]:
                        return _http(call, 200, item)
        return _http(call, 404)
    number = entity["number"]
    raw, pull = _source(repository, number, pull=op in {"pull", "pr_view", "review_comments", "reviews"})
    if raw is None:
        return _http(call, 404)
    if op == "issue":
        value = _base_source(world, repo, number, raw, pull)
        if pull:
            value["pull_request"] = {"merged_at": raw.get("merged_at"), "html_url": value["html_url"]}
        return _http(call, 200, value)
    if op == "pull":
        return _http(call, 200, _pull(world, git_root, repo, number, raw))
    if op == "pr_view":
        return _http(call, 200, _pr_view(world, git_root, repo, number, raw, call.options["columns"]))
    key = "comments" if op == "issue_comments" else op
    return _http(call, 200, _page_payload(call, raw.get(key, [])))


def _execute(call, world, git_root):
    op = call.operation
    if op == "auth":
        return _result(stderr=f"Logged in to github.com as {world['account']} (fixture)\n"), None
    if op == "version":
        return _result("gh version 0.0.0-fixture\n"), None
    if op == "rate_limit":
        reset = int(time.time()) + 60
        resource = {"remaining": 5000, "limit": 5000, "reset": reset}
        values = {"core": resource, "search": {**resource, "remaining": 30, "limit": 30}}
        for key, fields in world.get("rate_limit", {}).items():
            values[key] = {**values.get(key, {}), **fields}
        return _http(call, 200, {"resources": values}), None
    if op == "notifications":
        return _notification(call, world), None
    if op == "graphql":
        return _http(call, 200, {"data": {"node": _comment_node(world, call.entity["node_id"])}}), None
    repo = call.entity["repo"]
    repository = world["repositories"].get(repo)
    if repository is None:
        return _http(call, 404), None
    if not call.write:
        return _read_repository(call, world, repository, git_root), None
    raw = None
    if op in {"comment", "pr_review"}:
        raw, _ = _source(repository, call.entity["number"], pull=op == "pr_review")
        if raw is None:
            return _http(call, 404), None
    return _record_write(call, world, repository, raw, git_root)


class Disconnect(Exception):
    pass


class FixtureHTTPServer(ThreadingHTTPServer):
    def __init__(self, address, store, git_root=None):
        self.store = store
        self.git_root = Path(git_root).resolve() if git_root else None
        super().__init__(address, Handler)

    def _fault(self, call, fault):
        if not fault:
            return None
        kind = fault["kind"]
        if kind == "hold":
            if self.store.wait(fault) == "abort":
                return _result(stderr="gh: fixture request aborted\n", exit_code=1)
            return None
        if kind == "http":
            return _http(call, fault["status"], fault.get("body"), fault.get("headers"))
        if kind == "exit":
            return _result(fault.get("stdout", ""), fault.get("stderr", ""), fault["exit_code"])
        raise Disconnect

    def github(self, payload):
        try:
            call = parse_call(payload)
        except (KeyError, TypeError, ValueError) as exc:
            self.store.failure({"reason": "unsupported_gh", "argv": payload.get("argv"), "detail": str(exc)})
            return _result(stderr=f"gh: fixture contract error: {exc}\n", exit_code=64)
        request = call.evidence(payload)
        try:
            response = self._fault(call, self.store.begin(request))
            if response is None:
                response = self._fault(call, self.store.phase(request, "before_effect"))
            if response is None:
                response, effect_id, fault = self.store.apply(
                    request, write=call.write, action=lambda world: _execute(call, world, self.git_root)
                )
                replacement = self._fault(call, fault)
                if replacement is not None:
                    response = replacement
                else:
                    response = self._fault(call, self.store.phase(request, "before_response", effect_id)) or response
        except Disconnect:
            self.store.finish(request["request_id"], {"disconnected": True})
            raise
        self.store.finish(request["request_id"], response)
        return response

    def activity(self, payload):
        if not isinstance(payload.get("operation"), str) or not isinstance(payload.get("entity"), dict):
            raise ValueError("activity requires operation and entity")
        phase = payload.get("phase")
        if not isinstance(phase, str) or not phase:
            raise ValueError("activity requires phase")
        request = {**payload, "request_id": uuid4().hex}
        fault = self.store.phase(request, phase)
        if fault and fault["kind"] == "hold":
            decision = self.store.wait(fault)
        else:
            decision = "abort" if fault else "continue"
        return {"request_id": request["request_id"], "decision": decision}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def _send(self, status, value):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlsplit(self.path)
        try:
            if url.path == "/health":
                value = {"ok": True}
            elif url.path == "/state":
                value = self.server.store.state()
            elif url.path == "/checkpoints":
                after = int(parse_qs(url.query).get("after_seq", ["0"])[0])
                value = {"checkpoints": self.server.store.checkpoints(after)}
            else:
                self._send(404, {"error": "unknown fixture endpoint"})
                return
            self._send(200, value)
        except ValueError as exc:
            self._send(400, {"error": str(exc)})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 8 * 1024 * 1024:
                raise ValueError("invalid fixture payload length")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("fixture payload must be an object")
            if self.path == "/scenario":
                scenario_id = self.server.store.seed(payload["run_id"], payload["case_id"], payload["seed"])
                value = {"scenario_id": scenario_id}
            elif self.path == "/faults":
                value = {"rule_id": self.server.store.arm(payload)}
            elif match := re.fullmatch(r"/faults/([a-f0-9]+)/disarm", self.path):
                self.server.store.disarm(payload["scenario_id"], match[1])
                value = {"ok": True}
            elif self.path == "/seed-events":
                self.server.store.patch(payload["scenario_id"], payload["patch"])
                value = {"ok": True}
            elif match := re.fullmatch(r"/barriers/([a-f0-9]+)/release", self.path):
                self.server.store.release(match[1], payload["decision"])
                value = {"ok": True}
            elif self.path == "/activity":
                value = self.server.activity(payload)
            elif self.path == "/gh":
                value = self.server.github(payload)
            else:
                self._send(404, {"error": "unknown fixture endpoint"})
                return
            self._send(200, value)
        except Disconnect:
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except (KeyError, TypeError, ValueError) as exc:
            if self.path == "/gh":
                self.server.store.failure({"reason": "fixture_request_failed", "detail": str(exc)})
            self._send(400, {"error": str(exc)})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--git-root", type=Path)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", default=8765, type=int)
    args = parser.parse_args()
    args.db.parent.mkdir(parents=True, exist_ok=True)
    with FixtureHTTPServer((args.host, args.port), FixtureStore(args.db), args.git_root) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
