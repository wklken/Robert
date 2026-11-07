"""GitHub API helpers for the rewritten DD GitHub agent.

Phase 4 is fixture-backed. Live `gh` calls are added after the event and
authorization contracts are covered by tests.
"""


def collaborator_permission_from_payload(payload):
    permission = payload.get("permission") or payload.get("role_name")
    if not permission:
        return "unknown"
    return str(permission)
