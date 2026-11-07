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
        return {'ok': False, 'status': 'supervision_fence_rejected', 'safe_error': 'Publication policy or recovery claim changed.'}
    if conn is None:
        return None
    scope = publication_scope.trusted_action_scope(conn, action)
    if isinstance(scope, dict) and scope.get("supervision_repair"):
        try:
            supervision_git.binding(scope)
        except OSError:
            return {'ok': False, 'status': 'publish_failed', 'safe_error': 'Maintenance Git binding unavailable'}
    if scope is None or execution_operations.publication_scope_is_current(
        conn, action.get("task_id"), scope
    ):
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
