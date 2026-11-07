"""application delivery intents responsibilities."""

from contextlib import closing
import re
import sqlite3
from robert_agent.delivery.application import reconcile as publication_reconcile
from robert_agent.adapters.sqlite import execution_control as execution_store
from robert_agent.supervision import git as supervision_git

from robert_agent.delivery.application import effects as publication_effects
from robert_agent.delivery.application import scope as publication_scope
from robert_agent.delivery import intents as intent_facts
from robert_agent.delivery import values as publication_values


def derive_push_source_sha(action, action_scope, run_command):
    """Read the exact local source SHA for a non-remediation push."""

    if action.get("action_type") != "push_existing_pr":
        return action_scope, None
    scope = dict(action_scope or {})
    if scope.get("result_head_sha"):
        return scope, scope.get("result_head_sha")
    if scope.get("supervision_repair"):
        return scope, None
    metadata = publication_values.decode_json(action.get("metadata_json"))
    worktree_path = publication_effects.metadata_value(metadata, "worktree_path") or scope.get("worktree_path")
    source_ref = publication_effects.metadata_value(metadata, "source_ref") or "HEAD"
    if not worktree_path:
        return scope, None
    command = ["git", "rev-parse", source_ref]
    try:
        completed = run_command(command, cwd=worktree_path, capture_output=True, text=True)
    except Exception:
        return scope, None
    if publication_effects.command_failure(completed, "git rev-parse failed"):
        return scope, None
    source_sha = (getattr(completed, "stdout", "") or "").strip().splitlines()
    source_sha = source_sha[0].strip() if source_sha else ""
    if not re.fullmatch(r"[0-9a-fA-F]{7,64}", source_sha):
        return scope, None
    scope["result_head_sha"] = source_sha
    return scope, source_sha


def prepare_publication_intent(db_path, action_id, run_command):
    """Prepare and execute an intent in committed local transactions."""

    now = publication_values.now()
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        action_row = conn.execute(
            """
            SELECT action_id, result_id, task_id, action_type, target_url, external_id,
                   audit_status, publish_status, created_at, metadata_json
            FROM github_actions WHERE action_id = ?
            """,
            (action_id,),
        ).fetchone()
        if not action_row:
            return {"state": "missing", "action": None, "intent": None, "scope": None}
        action = dict(action_row)
        if not conn.execute(
            "SELECT 1 FROM tasks t WHERE t.task_id=? AND " + execution_store.runnable_sql(),
            (action["task_id"],),
        ).fetchone():
            return {"state": "missing", "action": action, "intent": None, "scope": None}
        if action["publish_status"] != "not_published" or action["audit_status"] != "accepted":
            return {"state": "missing", "action": action, "intent": None, "scope": None}
        scope = publication_scope.trusted_action_scope(conn, action)
        intent = intent_facts.load_intent(conn, action_id)
    if isinstance(scope, dict) and scope.get("supervision_repair"):
        try:
            supervision_git.binding(scope)
        except OSError:
            return {
                "state": "scope_missing",
                "action": action,
                "intent": intent,
                "scope": scope,
            }
    source_sha = None
    head_owner = None
    if intent is None and (scope is None or not isinstance(scope, dict)):
        return {
            "state": "scope_missing",
            "action": action,
            "intent": None,
            "scope": None,
        }
    if intent is not None and intent["status"] == "prepared" and not isinstance(scope, dict):
        return {
            "state": "scope_missing",
            "action": action,
            "intent": intent,
            "scope": scope,
        }
    if intent is not None:
        persisted_evidence = publication_values.decode_json(intent.get("remote_evidence_json"))
        source_sha = persisted_evidence.get("source_sha") or None
        head_owner = persisted_evidence.get("head_owner") or None
    # Source SHA is read before deriving the fingerprint and before the
    # prepared/executing commits.  On retries only the immutable value from
    # the existing intent is trusted; mutable local HEAD is never re-read.
    if intent is None or intent["status"] == "prepared":
        if isinstance(scope, dict):
            scope, source_sha = derive_push_source_sha(action, scope, run_command)
            if action.get("action_type") == "open_pr":
                with closing(sqlite3.connect(db_path)) as conn:
                    conn.row_factory = sqlite3.Row
                    head_owner = publication_effects.trusted_head_owner_for_intent(
                        conn,
                        action,
                        scope,
                        run_command,
                    )
                if head_owner:
                    scope = {**scope, "head_owner": head_owner}
    if (
        action.get("action_type") == "push_existing_pr"
        and isinstance(scope, dict)
        and not scope.get("result_head_sha")
        and not source_sha
    ):
        return {
            "state": "source_sha_missing",
            "action": action,
            "intent": intent,
            "scope": scope,
        }
    if action.get("action_type") == "open_pr" and not head_owner:
        return {
            "state": "head_owner_missing",
            "action": action,
            "intent": intent,
            "scope": scope,
        }
    if intent is None:
        if scope is None or not isinstance(scope, dict):
            return {
                "state": "scope_missing",
                "action": action,
                "intent": None,
                "scope": None,
            }
        try:
            idempotency_key, request_fingerprint = publication_reconcile.derive_publication_keys(action, scope)
        except ValueError:
            return {
                "state": "scope_missing",
                "action": action,
                "intent": None,
                "scope": None,
            }
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conflicting = conn.execute(
                "SELECT action_id FROM publication_intents WHERE idempotency_key=? AND action_id!=?",
                (idempotency_key, action_id),
            ).fetchone()
            if conflicting:
                return {
                    "state": "scope_missing",
                    "action": action,
                    "intent": None,
                    "scope": scope,
                }
            intent = dict(
                execution_store.prepare_publication_intent(
                    conn,
                    action_id,
                    idempotency_key,
                    request_fingerprint,
                    now,
                )
            )
    # A terminal published intent is replay success.  Backfill the action if
    # a process crashed after intent completion but before the action update.
    if intent["status"] == "published":
        return {
            "state": "published",
            "action": action,
            "intent": intent,
            "scope": scope,
        }
    if intent["status"] == "attention":
        return {
            "state": "attention",
            "action": action,
            "intent": intent,
            "scope": scope,
        }

    if intent["status"] == "executing":
        return {
            "state": "reconcile",
            "action": action,
            "intent": intent,
            "scope": scope,
            "source_sha": source_sha,
            "head_owner": head_owner,
        }

    evidence = {}
    if source_sha:
        evidence["source_sha"] = source_sha
    if head_owner:
        evidence["head_owner"] = head_owner
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.row_factory = sqlite3.Row
        latest = intent_facts.load_intent(conn, action_id)
        if latest["status"] == "prepared":
            latest = dict(
                execution_store.transition_publication_intent(
                    conn,
                    latest["publication_id"],
                    "executing",
                    now,
                    remote_evidence=evidence or None,
                )
            )
        elif latest["status"] in {"failed", "confirmed_absent"}:
            if latest["status"] == "confirmed_absent":
                evidence = {
                    **publication_values.decode_json(latest.get("remote_evidence_json")),
                    **evidence,
                    "retry_after_absence": True,
                }
                evidence.pop("status", None)
            latest = dict(
                execution_store.transition_publication_intent(
                    conn,
                    latest["publication_id"],
                    "executing",
                    now,
                    remote_evidence=evidence or None,
                )
            )
    return {
        "state": "ready",
        "action": action,
        "intent": latest,
        "scope": scope,
        "source_sha": source_sha,
        "head_owner": head_owner,
    }
