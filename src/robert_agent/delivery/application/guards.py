"""application delivery guards responsibilities."""

from robert_agent.delivery.application import scope as publication_scope
from robert_agent.delivery.application import audit as audit_result
from robert_agent.execution import operations as execution_operations
from robert_agent.supervision import git as supervision_git

from robert_agent.delivery.application import outcomes as publication_outcomes
from robert_agent.delivery import intents as intent_facts
from robert_agent.delivery import values as publication_values


def publish_scope_violation(conn, action):
    action_scope = publication_scope.trusted_action_scope(conn, action)
    if action_scope is None:
        return None
    payload = publication_values.decode_json(action["metadata_json"])
    payload["type"] = action["action_type"]
    if action.get("target_url"):
        payload["target_url"] = action["target_url"]
        payload["url"] = action["target_url"]
    return audit_result.action_scope_violation([payload], action_scope)


def publication_scope_guard(conn, action, recovery_guard=None):
    """Recheck the durable binding immediately before an external write."""

    if recovery_guard and not recovery_guard():
        return {
            "ok": False,
            "status": "supervision_fence_rejected",
            "safe_error": "Publication denied by current trust, route, or recovery policy. Review authorization before retrying.",
        }
    if conn is None:
        return None
    scope = publication_scope.trusted_action_scope(conn, action)
    if isinstance(scope, dict) and scope.get("supervision_repair"):
        try:
            supervision_git.binding(scope)
        except OSError:
            return {
                "ok": False,
                "status": "publish_failed",
                "safe_error": "Maintenance Git binding unavailable",
            }
    if scope is None or execution_operations.publication_scope_is_current(conn, action.get("task_id"), scope):
        return None
    now = publication_values.now()
    intent = intent_facts.load_intent(conn, action["action_id"])
    reconciliation = {
        "status": "unknown",
        "reason_code": "publication_scope_stale",
        "evidence": {},
    }
    publication_outcomes.mark_attention(conn, action, intent, scope, reconciliation, now)
    conn.commit()
    return {
        "ok": False,
        "status": "publish_failed",
        "reason_code": "publication_scope_stale",
        "safe_error": "publication scope is no longer active",
        "command": [],
    }


def existing_pr_push_target(scope, run_command, *, reconcile=False):
    """Validate and pin the PR push destination for both reads and the write."""
    from robert_agent.adapters.github import client
    from robert_agent.adapters.git.remotes import github_repository
    from robert_agent.foundation.commands import ControlDeadlineExceeded
    import subprocess

    sources = [source for source in scope.get("sources", ()) if source.get("source_type") == "pull_request"]
    if not sources or scope.get("supervision_repair"):
        return None
    error = {
        "ok": False,
        "status": "publish_failed",
        "retryable": False,
        "safe_error": "PR head repository, publisher or push destination does not match the audited task.",
    }
    if len(sources) != 1 or not scope.get("repo") or not scope.get("github_account"):
        return error
    try:
        details = client.try_read_json(
            ["gh", "api", f"repos/{scope['repo']}/pulls/{sources[0]['number']}"], runner=run_command
        )
        head = (details or {}).get("head") or {}
        head_repo = (head.get("repo") or {}).get("full_name")
        if (
            not head_repo
            or (not reconcile and (details.get("state") != "open" or details.get("merged") or details.get("merged_at")))
            or ((details.get("user") or {}).get("login") or "").casefold() != scope["github_account"].casefold()
            or head.get("ref") != scope.get("branch_name")
        ):
            return error
        remote = run_command(
            ["git", "remote", "get-url", "--push", "--all", scope.get("remote") or "origin"],
            cwd=scope.get("worktree_path"),
            capture_output=True,
            text=True,
        )
        urls = remote.stdout.strip().splitlines()
        if remote.returncode or len(urls) != 1 or github_repository(urls[0]) != head_repo.casefold():
            return error
        expanded = run_command(
            ["git", "ls-remote", "--get-url", urls[0]],
            cwd=scope.get("worktree_path"),
            capture_output=True,
            text=True,
        )
        if expanded.returncode or github_repository(expanded.stdout.strip()) != head_repo.casefold():
            return error
    except ControlDeadlineExceeded:
        raise
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError):
        return error
    # Keep the named remote for push: an explicit pushurl bypasses pushInsteadOf,
    # while pushing a raw URL would apply that rewrite for a second time.
    return {"ok": True, "remote": scope.get("remote") or "origin", "read_remote": urls[0]}
