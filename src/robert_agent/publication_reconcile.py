"""Read-only classification of remote publication state.

The publisher owns all local mutations.  This module only queries GitHub or a
remote Git ref and returns one of the three durable reconciliation states.
Returned evidence is deliberately small so a remote response can never leak
an unbounded body, command output, or credential into Robert state.
"""

import hashlib
import json
import re
from urllib.parse import urlparse

from robert_agent import pr_health


COMMENT_MARKER_RE = re.compile(
    r"<!--\s*robert-comment\b.*?-->",
    re.I | re.S,
)
WORKSTREAM_MARKER_RE = re.compile(
    r"<!--\s*robert-workstream\b.*?-->",
    re.I | re.S,
)

_SCOPE_KEYS = {
    "repo",
    "base_branch",
    "worktree_path",
    "branch_name",
    "remote",
    "sources",
    "existing_pr_head_sha",
    "head_owner",
    "result_head_sha",
    "task_kind",
    "episode_id",
    "pr_number",
    "observed_head_sha",
    "observed_base_sha",
}


def _decode_json(value):
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed


def _bounded(value, limit=256):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if not isinstance(value, str):
        return ""
    return value[:limit]


def _evidence(**values):
    result = {}
    for key, value in values.items():
        if isinstance(value, list):
            result[key] = [_bounded(item) for item in value[:8]]
        elif isinstance(value, dict):
            result[key] = {
                str(item_key)[:64]: _bounded(item_value)
                for item_key, item_value in list(value.items())[:16]
            }
        else:
            result[key] = _bounded(value)
    return result


def _result(status, reason_code, **evidence):
    if status not in {"confirmed_effect", "confirmed_absent", "unknown"}:
        raise ValueError("invalid publication reconciliation status")
    return {
        "status": status,
        "reason_code": str(reason_code)[:96],
        "evidence": _evidence(**evidence),
    }


def _iter_objects(value):
    if isinstance(value, dict):
        yield value
        return
    if isinstance(value, list):
        for item in value:
            yield from _iter_objects(item)


def _command_failure(completed, fallback):
    if getattr(completed, "returncode", 0) == 0:
        return None
    # Only retain the exception/failure class, never remote stderr.
    return fallback


def _target_parts(target_url):
    parsed = urlparse(target_url or "")
    if parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 4 or parts[2] not in {"issues", "pull"} or not parts[3].isdigit():
        return None
    return parts[0], parts[1], parts[2], parts[3]


def _marker(body, marker_re):
    match = marker_re.search(body or "")
    return match.group(0) if match else ""


def _owner(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("login", "name"):
            if isinstance(value.get(key), str):
                return value[key]
        return _owner(value.get("owner"))
    return ""


def _head_branch(value):
    return value.split(":", 1)[-1] if isinstance(value, str) else ""


def _owner_matches(expected, actual):
    return bool(expected and actual and expected.casefold() == actual.casefold())


def _scope_projection(action_scope):
    if not isinstance(action_scope, dict):
        return None
    projected = {}
    for key in sorted(_SCOPE_KEYS):
        if key not in action_scope:
            continue
        value = action_scope[key]
        if key == "sources":
            if not isinstance(value, list):
                return None
            projected[key] = [
                {
                    "source_type": item.get("source_type"),
                    "number": item.get("number"),
                }
                for item in value
                if isinstance(item, dict)
            ]
        elif isinstance(value, (str, int, bool)) or value is None:
            projected[key] = value
    if not projected:
        return None
    return projected


def derive_publication_keys(action, action_scope):
    """Return stable idempotency/request keys from audited scope only."""

    projection = _scope_projection(action_scope)
    if not isinstance(projection, dict):
        raise ValueError("accepted action scope is required for publication intent")
    identity = {
        "action_id": str(action.get("action_id") or ""),
        "action_type": str(action.get("action_type") or ""),
        "target_url": str(action.get("target_url") or ""),
        "scope": projection,
    }
    if not identity["action_id"] or not identity["action_type"]:
        raise ValueError("publication action identity is incomplete")
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    request_identity = {
        "action_type": identity["action_type"],
        "target_url": identity["target_url"],
        "scope": projection,
    }
    request_encoded = json.dumps(
        request_identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    request_digest = hashlib.sha256(request_encoded.encode("utf-8")).hexdigest()
    return f"publication:{digest}", f"request:{request_digest}"


def _reconcile_comment(action, action_scope, run_command):
    target = _target_parts(action.get("target_url"))
    metadata = _decode_json(action.get("metadata_json"))
    marker = _marker(metadata.get("body"), COMMENT_MARKER_RE)
    if not marker:
        return _result("unknown", "comment_marker_missing")
    if not target:
        return _result("unknown", "comment_target_malformed")
    owner, repository, source_type, number = target
    repo = f"{owner}/{repository}"
    scoped_repo = action_scope.get("repo") if isinstance(action_scope, dict) else ""
    if scoped_repo and scoped_repo.casefold() != repo.casefold():
        return _result("unknown", "scope_repository_conflict")
    endpoint = f"repos/{repo}/issues/{number}/comments"
    command = ["gh", "api", f"{endpoint}?per_page=100", "--paginate", "--slurp"]
    try:
        completed = run_command(command, capture_output=True, text=True)
    except Exception as exc:  # query failure is intentionally unknown
        return _result("unknown", "comment_query_failed", error_type=type(exc).__name__)
    if _command_failure(completed, "comment_query_failed"):
        return _result("unknown", "comment_query_failed")
    raw = getattr(completed, "stdout", "")
    try:
        response = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return _result("unknown", "comment_response_malformed")
    matches = []
    for comment in _iter_objects(response):
        body = comment.get("body") if isinstance(comment.get("body"), str) else ""
        if marker in body:
            matches.append(comment)
    if len(matches) > 1:
        return _result("unknown", "comment_marker_conflict", match_count=len(matches))
    if matches:
        comment = matches[0]
        return _result(
            "confirmed_effect",
            "comment_marker_match",
            external_id=str(comment.get("id") or ""),
            target_url=comment.get("html_url") or comment.get("url") or "",
        )
    return _result("confirmed_absent", "comment_lookup_complete_absent")


def _reconcile_open_pr(action, action_scope, run_command):
    metadata = _decode_json(action.get("metadata_json"))
    scope = action_scope if isinstance(action_scope, dict) else {}
    scoped_repo = scope.get("repo")
    scoped_head = scope.get("branch_name")
    scoped_base = scope.get("base_branch")
    metadata_repo = metadata.get("repo")
    metadata_head = metadata.get("head") or metadata.get("branch")
    metadata_base = metadata.get("base")
    if (
        isinstance(scoped_repo, str)
        and isinstance(metadata_repo, str)
        and scoped_repo.casefold() != metadata_repo.casefold()
    ):
        return _result("unknown", "scope_repository_conflict")
    if scoped_head and metadata_head and _head_branch(metadata_head) != scoped_head:
        return _result("unknown", "scope_head_conflict")
    if scoped_base and metadata_base and metadata_base != scoped_base:
        return _result("unknown", "scope_base_conflict")
    repo = scoped_repo or metadata_repo
    head = scoped_head or metadata_head
    base = scoped_base or metadata_base
    marker = _marker(metadata.get("body"), WORKSTREAM_MARKER_RE)
    expected_owner = scope.get("head_owner") or metadata.get("head_owner") or ""
    if not isinstance(repo, str) or not repo or not isinstance(head, str) or not head or not base or not marker:
        return _result("unknown", "open_pr_scope_incomplete")
    branch = _head_branch(head)
    command = [
        "gh",
        "pr",
        "list",
        "--repo",
        repo,
        "--head",
        branch,
        "--state",
        "open",
        "--json",
        "number,url,baseRefName,headRefName,headRepositoryOwner",
    ]
    try:
        completed = run_command(command, capture_output=True, text=True)
    except Exception as exc:
        return _result("unknown", "open_pr_query_failed", error_type=type(exc).__name__)
    if _command_failure(completed, "open_pr_query_failed"):
        return _result("unknown", "open_pr_query_failed")
    try:
        response = json.loads(getattr(completed, "stdout", "") or "[]")
    except (TypeError, ValueError):
        return _result("unknown", "open_pr_response_malformed")
    if not isinstance(response, list):
        return _result("unknown", "open_pr_response_malformed")
    candidates = []
    for item in response:
        if not isinstance(item, dict):
            return _result("unknown", "open_pr_response_malformed")
        candidates.append(item)
    if not candidates:
        return _result("confirmed_absent", "open_pr_lookup_complete_absent")
    matches = []
    for candidate in candidates:
        number = str(candidate.get("number") or "")
        if not number:
            url = candidate.get("url") or ""
            match = re.search(r"/pull/(\d+)(?:$|[/?#])", url)
            number = match.group(1) if match else ""
        if not number:
            return _result("unknown", "open_pr_candidate_malformed")
        view_command = [
            "gh",
            "pr",
            "view",
            number,
            "--repo",
            repo,
            "--json",
            "number,url,body,baseRefName,headRefName,headRepositoryOwner",
        ]
        try:
            viewed = run_command(view_command, capture_output=True, text=True)
        except Exception as exc:
            return _result("unknown", "open_pr_query_failed", error_type=type(exc).__name__)
        if _command_failure(viewed, "open_pr_query_failed"):
            return _result("unknown", "open_pr_query_failed")
        try:
            detail = json.loads(getattr(viewed, "stdout", "") or "{}")
        except (TypeError, ValueError):
            return _result("unknown", "open_pr_response_malformed")
        if not isinstance(detail, dict):
            return _result("unknown", "open_pr_response_malformed")
        detail_repo = _target_parts(detail.get("url") or "")
        actual_repo = f"{detail_repo[0]}/{detail_repo[1]}" if detail_repo else repo
        actual_base = detail.get("baseRefName") or candidate.get("baseRefName") or ""
        actual_head = detail.get("headRefName") or candidate.get("headRefName") or ""
        actual_owner = _owner(detail.get("headRepositoryOwner")) or _owner(candidate.get("headRepositoryOwner"))
        body = detail.get("body") if isinstance(detail.get("body"), str) else ""
        exact = (
            actual_repo.casefold() == repo.casefold()
            and actual_base == base
            and actual_head == branch
            and marker in body
            and (not expected_owner or _owner_matches(expected_owner, actual_owner))
        )
        if exact:
            matches.append(detail)
            continue
        # A candidate on the requested branch/base but with a conflicting
        # marker/owner is ambiguity, never proof of absence.
        if actual_repo.casefold() == repo.casefold() and actual_base == base and actual_head == branch:
            return _result("unknown", "open_pr_candidate_conflict")
    if len(matches) > 1:
        return _result("unknown", "open_pr_candidate_conflict", match_count=len(matches))
    if matches:
        detail = matches[0]
        return _result(
            "confirmed_effect",
            "open_pr_marker_match",
            external_id=str(detail.get("number") or ""),
            target_url=detail.get("url") or "",
        )
    return _result("unknown", "open_pr_candidate_conflict")


def _remote_branch(action, action_scope, run_command):
    metadata = _decode_json(action.get("metadata_json"))
    scope = action_scope if isinstance(action_scope, dict) else {}
    worktree = metadata.get("worktree_path") or scope.get("worktree_path")
    remote = metadata.get("remote") or scope.get("remote") or "origin"
    branch = metadata.get("branch") or metadata.get("head") or scope.get("branch_name")
    if not worktree or not branch:
        return _result("unknown", "push_scope_incomplete")
    command = ["git", "ls-remote", "--heads", remote, branch]
    try:
        completed = run_command(command, cwd=worktree, capture_output=True, text=True)
    except Exception as exc:
        return _result("unknown", "remote_branch_query_failed", error_type=type(exc).__name__)
    if _command_failure(completed, "remote_branch_query_failed"):
        return _result("unknown", "remote_branch_query_failed")
    lines = (getattr(completed, "stdout", "") or "").strip().splitlines()
    if len(lines) > 1:
        return _result("unknown", "remote_branch_response_conflict")
    remote_sha = ""
    if lines:
        parts = lines[0].split()
        if len(parts) < 2 or not re.fullmatch(r"[0-9a-fA-F]{7,64}", parts[0]):
            return _result("unknown", "remote_branch_response_malformed")
        remote_sha = parts[0]
    expected_sha = scope.get("result_head_sha") or scope.get("result_sha") or metadata.get("result_head_sha")
    pre_push_sha = scope.get("existing_pr_head_sha")
    if not expected_sha:
        return _result("unknown", "result_sha_missing")
    if remote_sha and remote_sha.casefold() == str(expected_sha).casefold():
        return _result("confirmed_effect", "remote_sha_match", remote_sha=remote_sha, branch=branch)
    if not remote_sha:
        if pre_push_sha:
            return _result("unknown", "existing_pr_branch_missing", branch=branch)
        return _result("confirmed_absent", "remote_branch_lookup_complete_absent", branch=branch)
    if pre_push_sha and remote_sha.casefold() == str(pre_push_sha).casefold():
        repo = scope.get("repo")
        pr_number = scope.get("pr_number")
        observed_head = scope.get("observed_head_sha")
        observed_base = scope.get("observed_base_sha")
        if not repo or not pr_number or not observed_head or not observed_base:
            return _result("unknown", "existing_pr_snapshot_incomplete")
        try:
            snapshot = pr_health.fetch_pr_snapshot(repo, pr_number, runner=run_command)
        except Exception as exc:
            return _result("unknown", "existing_pr_snapshot_query_failed", error_type=type(exc).__name__)
        if not snapshot or snapshot.get("state") != "open" or snapshot.get("merged"):
            return _result("unknown", "existing_pr_snapshot_changed")
        if snapshot.get("head_sha") != observed_head or snapshot.get("base_sha") != observed_base:
            return _result("unknown", "existing_pr_snapshot_changed")
        return _result("confirmed_absent", "existing_pr_pre_push_head_match", remote_sha=remote_sha)
    return _result("unknown", "remote_sha_conflict", remote_sha=remote_sha)


def classify_remote_state(action, action_scope, run_command, *, conn=None):
    """Classify one action without changing SQLite or remote state."""

    if not isinstance(action_scope, dict):
        return _result("unknown", "audited_scope_missing")
    action_type = action.get("action_type")
    if action_type == "comment":
        return _reconcile_comment(action, action_scope, run_command)
    if action_type == "open_pr":
        return _reconcile_open_pr(action, action_scope, run_command)
    if action_type == "push_existing_pr":
        return _remote_branch(action, action_scope, run_command)
    return _result("unknown", "unsupported_action_type")
