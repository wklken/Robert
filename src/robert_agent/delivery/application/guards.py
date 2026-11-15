"""application delivery guards responsibilities."""

from dataclasses import dataclass

from robert_agent.delivery.application import scope as publication_scope
from robert_agent.delivery.application.source_delivery_observation import AcceptedDeliveryObservation
from robert_agent.delivery.application.source_results import current_started_github_delivery
from robert_agent.delivery.application import audit as audit_result
from robert_agent.execution import operations as execution_operations
from robert_agent.supervision import git as supervision_git

from robert_agent.delivery.application import outcomes as publication_outcomes
from robert_agent.delivery import intents as intent_facts
from robert_agent.delivery import values as publication_values
from robert_agent.foundation.contracts.delivery import OutputType, PublicationStatus
from robert_agent.foundation.values import utc_now
from robert_agent.persistence import database, transactions
from robert_agent.persistence.stores.source_sessions import SourceClaim
from robert_agent.work.tapd_authority import TicketIdentity
from contextlib import closing
from robert_agent.persistence.queries import source_github_publication, source_pr_selection
from robert_agent.persistence.rounds.source_evidence import context_digest


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


@dataclass(frozen=True, slots=True)
class NativeGitHubWriteScope:
    """Exact started effect and current durable source-authorization collaborator."""

    source_result_id: str
    publication_id: str
    request_fingerprint: str
    database_path: str
    config: dict
    ticket: TicketIdentity
    claim: SourceClaim
    revision: str
    recovery_observation: object = None
    mixed_observation: AcceptedDeliveryObservation | None = None


def _native_scope_current(conn, action, native):
    if conn is None or not isinstance(native, NativeGitHubWriteScope):
        return False
    with closing(database.connect(native.database_path, foreign_keys=True)) as writer, writer:
        transactions.begin_immediate(writer)
        row = source_github_publication.required_action(writer, native.source_result_id, action["action_id"])
        if (
            row is None
            or any(action.get(key) != row[key] for key in ("task_id", "result_id", "action_type", "target_json"))
            or row["publication_id"] != native.publication_id
            or row["request_fingerprint"] != native.request_fingerprint
            or row["intent_status"] != PublicationStatus.EXECUTING
        ):
            return False
        packet = source_github_publication.prepared_request(row)
        audit_scope = publication_scope.trusted_action_scope(writer, action, include_output_type=True)
        return (
            packet is not None
            and isinstance(audit_scope, dict)
            and packet.get("audit_scope_sha256") == context_digest(audit_scope)
            and source_pr_selection.effect_selection(writer, row)[0]
            and publication_values.decode_json(action["metadata_json"]) == packet["effect"]
            and current_started_github_delivery(
                writer,
                native.config,
                native.ticket,
                native.claim,
                source_result_id=native.source_result_id,
                action_id=row["action_id"],
                publication_id=native.publication_id,
                revision=native.revision,
                now=utc_now(),
                recovery_observation=native.recovery_observation,
                mixed_observation=native.mixed_observation,
            )
            is not None
        )


def publication_scope_guard(conn, action, recovery_guard=None, *, native_scope=None):
    """Recheck the durable binding immediately before an external write."""

    if native_scope is not None:
        if _native_scope_current(conn, action, native_scope):
            return None
        return {
            "ok": False,
            "status": "native_scope_rejected",
            "safe_error": "Native GitHub publication scope is unavailable.",
        }
    if action.get("target_json") is not None:
        return {
            "ok": False,
            "status": "native_scope_rejected",
            "safe_error": "A native GitHub action requires its exact source publication scope.",
        }
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


def push_target(scope, run_command, *, reconcile=False, pinned_head_repository=None):
    """Validate the output's push destination for both reads and the write."""
    from robert_agent.adapters.github import client
    from robert_agent.adapters.git.remotes import github_repository
    from robert_agent.foundation.commands import ControlDeadlineExceeded
    import subprocess

    sources = [source for source in scope.get("sources", ()) if source.get("source_type") == "pull_request"]
    new_pr = scope.get("output_type") == OutputType.NEW_PR
    if scope.get("supervision_repair") or (not new_pr and not sources and not pinned_head_repository):
        return None
    error = {
        "ok": False,
        "status": "publish_failed",
        "retryable": False,
        "safe_error": "PR head repository, publisher or push destination does not match the audited task.",
    }
    if not scope.get("repo") or (not new_pr and (len(sources) != 1 or not scope.get("github_account"))):
        return error
    try:
        if new_pr:
            owner, repository = scope["repo"].split("/", 1)
            head_repo = pinned_head_repository or f"{scope.get('head_owner') or owner}/{repository}"
        else:
            details = client.try_read_json(
                ["gh", "api", f"repos/{scope['repo']}/pulls/{sources[0]['number']}"], runner=run_command
            )
            head = (details or {}).get("head") or {}
            head_repo = (head.get("repo") or {}).get("full_name")
            if (
                not head_repo
                or (pinned_head_repository and head_repo.casefold() != pinned_head_repository.casefold())
                or (
                    not reconcile
                    and (details.get("state") != "open" or details.get("merged") or details.get("merged_at"))
                )
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
