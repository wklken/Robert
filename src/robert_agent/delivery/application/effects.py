"""application delivery effects responsibilities."""
from robert_agent.adapters.git.remotes import github_owner
import re
import subprocess
from urllib.parse import urlparse
from robert_agent.workflows import pr_health
from robert_agent.workflows.review import publish as review_publish
from robert_agent.workflows.vote import publish as vote_publish
from robert_agent.supervision import git as supervision_git

from robert_agent.delivery.application import scope as publication_scope
from robert_agent.delivery.application import guards as publication_guards
from robert_agent.delivery import values as publication_values

COMMENT_MARKER_RE = re.compile(
    r"<!--\s*robert-comment\s+task_id=\S+\s+attempt_id=\S+\s+action_index=\d+\s*-->",
    re.I | re.S,
)

WORKSTREAM_MARKER_RE = re.compile(
    r"<!--\s*robert-workstream\b.*?-->",
    re.I | re.S,
)


def issue_comment_endpoint(target_url):
    parsed = urlparse(target_url or "")
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.netloc != "github.com" or len(parts) < 4:
        return None
    owner, repo, source_type, number = parts[:4]
    if source_type not in {"issues", "pull"} or not number.isdigit():
        return None
    return f"repos/{owner}/{repo}/issues/{number}/comments"



def hidden_marker(body, marker_re):
    match = marker_re.search(body or "")
    return match.group(0) if match else ""



def iter_json_objects(value):
    if isinstance(value, dict):
        yield value
        return
    if isinstance(value, list):
        for item in value:
            yield from iter_json_objects(item)



def command_failure(completed, fallback):
    if getattr(completed, "returncode", 0) == 0:
        return None
    return (getattr(completed, "stderr", "") or fallback).strip()



def find_existing_comment(endpoint, marker, run_command):
    command = ["gh", "api", f"{endpoint}?per_page=100", "--paginate", "--slurp"]
    completed = run_command(command, capture_output=True, text=True)
    safe_error = command_failure(completed, "gh api comment lookup failed")
    if safe_error:
        return {
            "ok": False,
            "status": "publish_failed",
            "safe_error": f"comment duplicate preflight failed: {safe_error}",
            "command": command,
        }
    response = publication_values.decode_json(getattr(completed, "stdout", ""))
    for comment in iter_json_objects(response):
        body = comment.get("body") if isinstance(comment.get("body"), str) else ""
        if marker and marker in body:
            return {
                "ok": True,
                "status": "published",
                "deduplicated": True,
                "external_id": str(comment.get("id") or ""),
                "target_url": comment.get("html_url") or comment.get("url") or "",
                "response": {"existing_comment": comment},
                "command": command,
            }
    return {"ok": True, "status": "not_found", "command": command}



def publish_comment(action, run_command, conn=None, recovery_guard=None):
    metadata = publication_values.decode_json(action["metadata_json"])
    body = metadata.get("body")
    if not isinstance(body, str) or not body:
        return {
            "ok": False,
            "status": "publish_failed",
            "safe_error": "comment action requires body",
        }
    marker = hidden_marker(body, COMMENT_MARKER_RE)
    if not marker:
        return {
            "ok": False,
            "status": "publish_failed",
            "safe_error": "comment action requires robert-comment idempotency marker",
        }
    endpoint = issue_comment_endpoint(action["target_url"] or metadata.get("target_url") or metadata.get("url"))
    if not endpoint:
        return {
            "ok": False,
            "status": "publish_failed",
            "safe_error": "comment action requires GitHub issue or PR target_url",
        }
    existing = find_existing_comment(endpoint, marker, run_command)
    if not existing["ok"] or existing["status"] == "published":
        return existing
    scope_guard = publication_guards.publication_scope_guard(conn, action, recovery_guard)
    if scope_guard:
        return scope_guard
    command = ["gh", "api", endpoint, "-f", f"body={body}"]
    completed = run_command(command, capture_output=True, text=True)
    safe_error = command_failure(completed, "gh api failed")
    if safe_error:
        return {
            "ok": False,
            "status": "publish_failed",
            "safe_error": safe_error,
            "command": command,
        }
    response = publication_values.decode_json(getattr(completed, "stdout", ""))
    if not isinstance(response, dict):
        response = {}
    return {
        "ok": True,
        "status": "published",
        "external_id": str(response.get("id") or ""),
        "target_url": response.get("html_url") or response.get("url") or action["target_url"],
        "response": response,
        "command": command,
    }



def pr_number_from_url(url):
    match = re.search(r"/pull/(\d+)(?:$|[/?#])", url or "")
    return match.group(1) if match else ""



def metadata_value(metadata, *keys):
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value:
            return value
    return ""



def head_branch(head):
    if ":" in head:
        return head.split(":", 1)[1]
    return head



def owner_login(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("login", "name"):
            owner = value.get(key)
            if isinstance(owner, str) and owner:
                return owner
        owner = value.get("owner")
        if owner is not value:
            return owner_login(owner)
    return ""



def owners_match(expected, actual):
    return bool(expected and actual and expected.lower() == actual.lower())



def pr_head_owner(pr):
    return owner_login(pr.get("headRepositoryOwner")) or owner_login(pr.get("headRepository"))



def find_existing_open_pr(repo, head, marker, run_command, base="", head_owner=""):
    branch = head_branch(head)
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
    completed = run_command(command, capture_output=True, text=True)
    safe_error = command_failure(completed, "gh pr list failed")
    if safe_error:
        return {
            "ok": False,
            "status": "publish_failed",
            "safe_error": f"open_pr duplicate preflight failed: {safe_error}",
            "command": command,
        }
    response = publication_values.decode_json(getattr(completed, "stdout", ""))
    for pr in iter_json_objects(response):
        url = pr.get("url") or ""
        number = str(pr.get("number") or pr_number_from_url(url) or "")
        base_ref = pr.get("baseRefName") or ""
        if base and base_ref and base_ref != base:
            continue
        head_ref = pr.get("headRefName") or ""
        if branch and head_ref and head_ref != branch:
            continue
        target = number or url
        if not target:
            continue
        view_command = [
            "gh",
            "pr",
            "view",
            target,
            "--repo",
            repo,
            "--json",
            "number,url,body,baseRefName,headRefName,headRepositoryOwner",
        ]
        view = run_command(view_command, capture_output=True, text=True)
        safe_error = command_failure(view, "gh pr view failed")
        if safe_error:
            return {
                "ok": False,
                "status": "publish_failed",
                "safe_error": f"open_pr duplicate preflight failed: {safe_error}",
                "command": view_command,
            }
        existing = publication_values.decode_json(getattr(view, "stdout", ""))
        existing_base = existing.get("baseRefName") or base_ref
        if base and existing_base and existing_base != base:
            continue
        existing_head = existing.get("headRefName") or head_ref
        if branch and existing_head and existing_head != branch:
            continue
        body = existing.get("body") if isinstance(existing.get("body"), str) else ""
        existing_owner = pr_head_owner(existing) or pr_head_owner(pr)
        if head_owner and not owners_match(head_owner, existing_owner):
            continue
        dedupe_reason = ""
        if head_owner and not owners_match(head_owner, existing_owner):
            continue
        if marker and marker in body:
            dedupe_reason = "matching_marker"
        elif owners_match(head_owner, existing_owner):
            dedupe_reason = "matching_head"
        else:
            continue
        return {
            "ok": True,
            "status": "published",
            "deduplicated": True,
            "external_id": str(existing.get("number") or number),
            "target_url": existing.get("url") or url,
            "response": {
                "existing_pr": existing or pr,
                "dedupe_reason": dedupe_reason,
            },
            "command": view_command,
        }
    return {"ok": True, "status": "not_found", "command": command}



def head_owner_from_push_metadata(metadata, head, run_command):
    branch = metadata_value(metadata, "branch", "head", "head_branch")
    if branch and branch != head:
        return ""
    worktree_path = metadata_value(metadata, "worktree_path")
    if not worktree_path:
        return ""
    remote = metadata_value(metadata, "remote") or "origin"
    completed = run_command(
        ["git", "remote", "get-url", remote],
        cwd=worktree_path,
        capture_output=True,
        text=True,
    )
    if command_failure(completed, "git remote get-url failed"):
        return ""
    return github_owner(getattr(completed, "stdout", ""))



def sibling_push_metadata(conn, action, head):
    if not conn or not action["result_id"]:
        return {}
    rows = conn.execute(
        """
        SELECT metadata_json
        FROM github_actions
        WHERE result_id = ?
          AND action_type = 'push_existing_pr'
        ORDER BY created_at DESC, action_id DESC
        """,
        (action["result_id"],),
    ).fetchall()
    for row in rows:
        metadata = publication_values.decode_json(row["metadata_json"])
        branch = metadata_value(metadata, "branch", "head", "head_branch")
        if not branch or branch == head:
            return metadata
    return {}



def head_owner_for_action(metadata, head, action, conn, run_command):
    scope = publication_scope.trusted_action_scope(conn, action) or {}
    if scope.get("head_owner"):
        return scope["head_owner"]
    if ":" in head:
        return head.split(":", 1)[0]
    owner = metadata_value(metadata, "head_owner", "fork_owner", "remote_owner", "head_repo_owner")
    if not owner:
        push_metadata = sibling_push_metadata(conn, action, head)
        owner = head_owner_from_push_metadata(push_metadata, head, run_command) if push_metadata else ""
    return owner



def trusted_head_owner_for_intent(conn, action, action_scope, run_command):
    """Resolve the immutable PR head owner without trusting Worker metadata."""

    scoped_owner = (
        action_scope.get("head_owner") if isinstance(action_scope, dict) else ""
    )
    if isinstance(scoped_owner, str) and scoped_owner:
        return scoped_owner
    if isinstance(action_scope, dict) and action_scope.get("supervision_repair"):
        return ""
    metadata = publication_values.decode_json(action.get("metadata_json"))
    head = metadata_value(metadata, "head", "head_branch", "branch")
    if ":" in head:
        return head.split(":", 1)[0]
    push_metadata = sibling_push_metadata(conn, action, head)
    if push_metadata:
        try:
            return head_owner_from_push_metadata(push_metadata, head, run_command)
        except Exception:
            return ""
    repo = action_scope.get("repo") if isinstance(action_scope, dict) else ""
    return repo.split("/", 1)[0] if isinstance(repo, str) and "/" in repo else ""



def head_for_pr_create(repo, head, owner):
    if ":" in head:
        return head
    repo_owner = repo.split("/", 1)[0]
    if owner and owner.lower() == repo_owner.lower():
        return head
    return f"{owner}:{head}" if owner else head



def publish_open_pr(action, run_command, conn=None, trusted_head_owner=None, recovery_guard=None):
    metadata = publication_values.decode_json(action["metadata_json"])
    repo = metadata_value(metadata, "repo", "repository")
    head = metadata_value(metadata, "head", "head_branch", "branch")
    base = metadata_value(metadata, "base", "base_branch", "target_base_branch")
    title = metadata_value(metadata, "title")
    body = metadata_value(metadata, "body", "pr_body")
    missing = [
        name
        for name, value in {
            "repo": repo,
            "head": head,
            "base": base,
            "title": title,
            "body": body,
        }.items()
        if not value
    ]
    if missing:
        return {
            "ok": False,
            "status": "publish_failed",
            "safe_error": f"open_pr action missing fields: {missing}",
        }
    marker = hidden_marker(body, WORKSTREAM_MARKER_RE)
    if not marker:
        return {
            "ok": False,
            "status": "publish_failed",
            "safe_error": "open_pr action body requires robert-workstream metadata",
        }
    head_owner = (
        trusted_head_owner
        if trusted_head_owner is not None
        else head_owner_for_action(metadata, head, action, conn, run_command)
    )
    existing = find_existing_open_pr(repo, head, marker, run_command, base=base, head_owner=head_owner)
    if not existing["ok"] or existing["status"] == "published":
        return existing
    scope_guard = publication_guards.publication_scope_guard(conn, action, recovery_guard)
    if scope_guard:
        return scope_guard
    create_head = head_for_pr_create(repo, head, head_owner)
    command = [
        "gh",
        "pr",
        "create",
        "--repo",
        repo,
        "--head",
        create_head,
        "--base",
        base,
        "--title",
        title,
        "--body",
        body,
    ]
    if metadata.get("draft"):
        command.append("--draft")
    completed = run_command(command, capture_output=True, text=True)
    safe_error = command_failure(completed, "gh pr create failed")
    if safe_error:
        existing_number = pr_number_from_url(safe_error)
        if existing_number and "already exists" in safe_error:
            return {
                "ok": True,
                "status": "published",
                "deduplicated": True,
                "external_id": existing_number,
                "target_url": f"https://github.com/{repo}/pull/{existing_number}",
                "response": {"dedupe_reason": "create_reported_existing_pr"},
                "command": command,
            }
        return {
            "ok": False,
            "status": "publish_failed",
            "safe_error": safe_error,
            "command": command,
        }
    lines = (getattr(completed, "stdout", "") or "").strip().splitlines()
    url = lines[-1].strip() if lines else ""
    return {
        "ok": True,
        "status": "published",
        "external_id": pr_number_from_url(url),
        "target_url": url or action["target_url"],
        "response": {"url": url},
        "command": command,
    }



def publish_push_existing_pr(action, run_command, conn=None, source_ref="HEAD", recovery_guard=None):
    metadata = publication_values.decode_json(action["metadata_json"])
    worktree_path = metadata_value(metadata, "worktree_path")
    remote = metadata_value(metadata, "remote") or "origin"
    branch = metadata_value(metadata, "branch", "head", "head_branch")
    missing = [
        name
        for name, value in {
            "worktree_path": worktree_path,
            "branch": branch,
        }.items()
        if not value
    ]
    if missing:
        return {
            "ok": False,
            "status": "publish_failed",
            "safe_error": f"push_existing_pr action missing fields: {missing}",
        }
    action_scope = publication_scope.trusted_action_scope(conn, action) or {}
    expected_remote_sha = action_scope.get("existing_pr_head_sha", "")
    result_head_sha = action_scope.get("result_head_sha") or (source_ref if source_ref != "HEAD" else None)
    source_ref = result_head_sha or source_ref
    destination_ref = f"refs/heads/{branch}"
    if action_scope and not result_head_sha:
        return {"ok": False, "status": "publish_failed",
                "safe_error": "audited result SHA is missing; retry the task to obtain a new audited result"}
    if action_scope.get("supervision_repair"):
        blocked = None

        def before_push():
            nonlocal blocked
            blocked = publication_guards.publication_scope_guard(conn, action, recovery_guard)
            return blocked is None

        try:
            completed = supervision_git.push(action_scope, result_head_sha, before_push=before_push, runner=run_command)
        except (OSError, subprocess.SubprocessError):
            return {"ok": False, "status": "publish_failed", "safe_error": "Maintenance Git push unavailable"}
        if blocked:
            return blocked
        safe_error = command_failure(completed, "Maintenance Git push failed")
        if safe_error:
            return {"ok": False, "status": "publish_failed", "safe_error": safe_error}
        return {
            "ok": True, "status": "published", "external_id": action_scope["branch_name"],
            "target_url": action.get("target_url"), "result_head_sha": result_head_sha,
            "response": {},
        }
    if expected_remote_sha:
        lookup_command = ["git", "ls-remote", "--heads", remote, branch]
        lookup = run_command(lookup_command, cwd=worktree_path, capture_output=True, text=True)
        lookup_error = command_failure(lookup, "git ls-remote failed")
        if lookup_error:
            return {
                "ok": False,
                "status": "publish_failed",
                "safe_error": f"remote branch lookup failed: {lookup_error}",
                "command": lookup_command,
            }
        lookup_stdout = (getattr(lookup, "stdout", "") or "").strip()
        current_remote_sha = lookup_stdout.split()[0] if lookup_stdout else ""
        if result_head_sha and current_remote_sha == result_head_sha:
            return {"ok": True, "status": "published", "deduplicated": True,
                    "external_id": branch, "target_url": action.get("target_url"),
                    "result_head_sha": result_head_sha,
                    "response": {"head_sha": result_head_sha}, "command": lookup_command}
        if current_remote_sha and current_remote_sha != expected_remote_sha:
            fetch_command = [
                "git", "fetch", "--no-tags", "--no-write-fetch-head",
                remote, destination_ref,
            ]
            fetched = run_command(
                fetch_command,
                cwd=worktree_path,
                capture_output=True,
                text=True,
            )
            fetch_error = command_failure(fetched, "git fetch failed")
            if fetch_error:
                return {
                    "ok": False,
                    "status": "publish_failed",
                    "safe_error": f"remote branch refresh failed: {fetch_error}",
                    "command": fetch_command,
                }
            ancestor_command = [
                "git", "merge-base", "--is-ancestor", current_remote_sha, result_head_sha
            ]
            ancestor = run_command(
                ancestor_command,
                cwd=worktree_path,
                capture_output=True,
                text=True,
            )
            if ancestor.returncode == 0:
                expected_remote_sha = current_remote_sha
            elif ancestor.returncode == 1:
                return {
                    "ok": True,
                    "status": "superseded",
                    "safe_error": "remote branch diverged from audited result; refusing to overwrite unreviewed head",
                    "command": ancestor_command,
                }
            else:
                safe_error = command_failure(
                    ancestor,
                    "git merge-base --is-ancestor failed",
                )
                return {
                    "ok": False,
                    "status": "publish_failed",
                    "safe_error": f"remote branch ancestry check failed: {safe_error}",
                    "command": ancestor_command,
                }
        command = [
            "git",
            "push",
            f"--force-with-lease={branch}:{expected_remote_sha}",
            remote,
            f"{source_ref}:{destination_ref}",
        ]
    else:
        command = ["git", "push", remote, f"{source_ref}:{destination_ref}"]
    scope_guard = publication_guards.publication_scope_guard(conn, action, recovery_guard)
    if scope_guard:
        return scope_guard
    completed = run_command(command, cwd=worktree_path, capture_output=True, text=True)
    safe_error = command_failure(completed, "git push failed")
    if safe_error:
        return {
            "ok": False,
            "status": "publish_failed",
            "safe_error": safe_error,
            "command": command,
        }
    return {
        "ok": True,
        "status": "published",
        "external_id": branch,
        "target_url": action["target_url"] or metadata.get("target_url") or metadata.get("url"),
        "result_head_sha": result_head_sha,
        "response": {"stdout": getattr(completed, "stdout", "")},
        "command": command,
    }



def publish_remediation_push(action, run_command, action_scope, conn=None, recovery_guard=None):
    repo = action_scope.get("repo")
    pr_number = action_scope.get("pr_number")
    observed_head_sha = action_scope.get("observed_head_sha")
    observed_base_sha = action_scope.get("observed_base_sha")
    audited_result_head_sha = action_scope.get("result_head_sha")
    if (
        not repo
        or not pr_number
        or not observed_head_sha
        or not observed_base_sha
        or not audited_result_head_sha
    ):
        return {
            "ok": False,
            "status": "publish_failed",
            "safe_error": "remediation publish scope is incomplete",
        }
    snapshot = pr_health.fetch_pr_snapshot(
        repo,
        pr_number,
        runner=run_command,
    )
    if snapshot is None:
        return {
            "ok": False,
            "status": "publish_failed",
            "safe_error": "remediation PR snapshot revalidation failed",
        }
    if (
        snapshot.get("state") == "open"
        and not snapshot.get("merged")
        and snapshot.get("head_sha") == audited_result_head_sha
        and snapshot.get("base_sha") == observed_base_sha
    ):
        return {"ok": True, "status": "published", "deduplicated": True,
                "external_id": str(pr_number), "target_url": action.get("target_url"),
                "result_head_sha": audited_result_head_sha,
                "response": {"head_sha": audited_result_head_sha}}
    if (
        snapshot.get("state") != "open"
        or snapshot.get("merged")
        or snapshot.get("head_sha") != observed_head_sha
        or snapshot.get("base_sha") != observed_base_sha
    ):
        return {
            "ok": True,
            "status": "superseded",
            "safe_error": "remediation PR snapshot changed before publication",
            "snapshot": snapshot,
        }
    metadata = publication_values.decode_json(action["metadata_json"])
    worktree_path = metadata_value(metadata, "worktree_path")
    head_command = ["git", "rev-parse", "HEAD"]
    completed = run_command(
        head_command,
        cwd=worktree_path,
        capture_output=True,
        text=True,
    )
    safe_error = command_failure(completed, "git rev-parse HEAD failed")
    local_head_sha = (getattr(completed, "stdout", "") or "").strip()
    if safe_error or not local_head_sha:
        return {
            "ok": False,
            "status": "publish_failed",
            "safe_error": safe_error or "git rev-parse HEAD returned no SHA",
            "command": head_command,
        }
    if local_head_sha != audited_result_head_sha:
        return {
            "ok": False,
            "status": "publish_failed",
            "safe_error": "remediation worktree HEAD changed after audit",
            "command": head_command,
        }
    result = publish_push_existing_pr(
        action,
        run_command,
        conn=conn,
        source_ref=audited_result_head_sha,
        recovery_guard=recovery_guard,
    )
    if result.get("status") == "published":
        result["result_head_sha"] = audited_result_head_sha
    return result



def publish_action(
    action,
    run_command,
    conn=None,
    source_sha=None,
    trusted_head_owner=None,
    recovery_guard=None,
):
    scope_violation = publication_guards.publish_scope_violation(conn, action)
    if scope_violation:
        return {
            "ok": False,
            "status": "publish_failed",
            "safe_error": f"action scope validation failed: {scope_violation}",
            "retryable": False,
        }
    scope_guard = publication_guards.publication_scope_guard(conn, action, recovery_guard)
    if scope_guard:
        return scope_guard
    if action["action_type"] == "submit_pr_review":
        return review_publish.publish(action, publication_scope.trusted_action_scope(conn, action), run_command,
                                      scope_guard=lambda: publication_guards.publication_scope_guard(conn, action, recovery_guard))
    if action["action_type"] == "comment" and vote_publish.is_vote(action):
        return vote_publish.publish(action, publication_scope.trusted_action_scope(conn, action), run_command,
                                    scope_guard=lambda: publication_guards.publication_scope_guard(conn, action, recovery_guard))
    if action["action_type"] == "comment":
        return publish_comment(action, run_command, conn=conn, recovery_guard=recovery_guard)
    if action["action_type"] == "open_pr":
        return publish_open_pr(
            action,
            run_command,
            conn=conn,
            trusted_head_owner=trusted_head_owner,
            recovery_guard=recovery_guard,
        )
    if action["action_type"] == "push_existing_pr":
        action_scope = publication_scope.trusted_action_scope(conn, action)
        if (
            isinstance(action_scope, dict)
            and action_scope.get("task_kind")
            in {"ci_remediation", "merge_conflict_remediation"}
        ):
            return publish_remediation_push(
                action,
                run_command,
                action_scope,
                conn=conn,
                recovery_guard=recovery_guard,
            )
        return publish_push_existing_pr(
            action,
            run_command,
            conn=conn,
            source_ref=source_sha or "HEAD",
            recovery_guard=recovery_guard,
        )
    return {
        "ok": False,
        "status": "unsupported_action",
        "safe_error": f"publisher does not support action_type {action['action_type']}",
    }
