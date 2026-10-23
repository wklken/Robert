"""Pure classification and bounded projection for workflow subject facts.

The classifier deliberately does not inspect Robert state, GitHub, or the
filesystem.  Callers provide an already observed fact with an exact subject
and source identity.  Only the small evidence projection returned here is
safe to persist in a workflow binding.
"""

from collections.abc import Mapping


ADDITIVE = "additive"
INVALIDATING = "invalidating"
TERMINAL = "terminal"
IRRELEVANT = "irrelevant"
CLASSIFICATIONS = frozenset({ADDITIVE, INVALIDATING, TERMINAL, IRRELEVANT})

_ADDITIVE_EVENT_TYPES = {
    "comment",
    "issue_comment",
    "review",
    "review_comment",
    "review_feedback",
    "review_request",
    "feedback",
}
_IRRELEVANT_EVENT_TYPES = {
    "notification",
    "duplicate",
    "no_change",
    "unrelated",
}
_TERMINAL_STATES = {"closed", "merged"}
_INVALIDATING_REASONS = {
    "head_changed",
    "base_changed",
    "base_ref_changed",
    "branch_disappeared",
    "source_branch_disappeared",
    "permission_revoked",
    "permission_denied",
    "authorization_revoked",
}
_REVOKED_PERMISSIONS = {"denied", "revoked"}
_EVIDENCE_KEYS = (
    "fact_id",
    "subject_type",
    "subject_id",
    "source_type",
    "source_key",
    "source_id",
    "repo",
    "number",
    "reason_code",
    "old_head_sha",
    "new_head_sha",
    "old_base_sha",
    "new_base_sha",
    "old_base_ref",
    "new_base_ref",
    "old_source_state",
    "new_source_state",
    "source_state",
    "source_branch_exists",
    "old_permission",
    "new_permission",
    "permission_decision",
)
_IDENTITY_KEYS = {"subject_type", "subject_id", "source_type", "source_key"}
_MAX_TEXT = 256
_MAX_EVIDENCE_BYTES = 4096


def _required_id(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_TEXT:
        raise ValueError(f"workflow fact {name} must be a non-empty bounded string")
    return value.strip()


def _optional_text(value, name):
    if value is None:
        return None
    return _required_id(value, name)


def _snapshot_value(fact, key, *aliases):
    value = fact.get(key)
    if value is not None:
        return value
    for alias in aliases:
        value = fact.get(alias)
        if value is not None:
            return value
    return None


def _pair(fact, old_key, new_key, old_aliases=(), new_aliases=()):
    old = _snapshot_value(fact, old_key, *old_aliases)
    new = _snapshot_value(fact, new_key, *new_aliases)
    old_snapshot = fact.get("old_snapshot")
    new_snapshot = fact.get("new_snapshot")
    if old is None and isinstance(old_snapshot, Mapping):
        old = old_snapshot.get(old_key.removeprefix("old_"))
    if new is None and isinstance(new_snapshot, Mapping):
        new = new_snapshot.get(new_key.removeprefix("new_"))
    return old, new


def _identity(fact):
    if not isinstance(fact, Mapping):
        raise ValueError("workflow fact must be a mapping")
    fact_id = fact.get("fact_id") or fact.get("event_fingerprint")
    _required_id(fact_id, "fact_id")
    if (
        fact.get("fact_id")
        and fact.get("event_fingerprint")
        and fact["fact_id"] != fact["event_fingerprint"]
    ):
        raise ValueError("workflow fact_id conflicts with event_fingerprint")
    subject_type = _required_id(fact.get("subject_type"), "subject_type")
    subject_id = _required_id(fact.get("subject_id"), "subject_id")
    source_type = _required_id(fact.get("source_type"), "source_type")
    source_key = fact.get("source_key") or fact.get("source_id")
    _required_id(source_key, "source_key")
    return {
        "fact_id": fact_id,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "source_type": source_type,
        "source_key": _required_id(source_key, "source_key"),
    }


def _state(fact):
    state = _snapshot_value(fact, "source_state", "state")
    if isinstance(state, str):
        return state.strip().lower()
    return ""


def _reason(fact):
    for key in ("invalidation_reason", "reason_code", "change_type"):
        value = fact.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return ""


def _has_invalidation_claim(fact):
    reason = _reason(fact)
    if reason in _INVALIDATING_REASONS:
        return True
    return any(
        key in fact
        for key in (
            "old_head_sha",
            "new_head_sha",
            "old_base_sha",
            "new_base_sha",
            "old_base_ref",
            "new_base_ref",
            "source_branch_exists",
            "branch_exists",
            "old_permission",
            "new_permission",
            "permission_decision",
        )
    )


def _changed_pair(fact, old_key, new_key, old_aliases=(), new_aliases=()):
    old, new = _pair(fact, old_key, new_key, old_aliases, new_aliases)
    if old is None and new is None:
        return None, None, False
    if old is None or new is None:
        raise ValueError(f"workflow invalidation requires {old_key} and {new_key}")
    old = _required_id(old, old_key)
    new = _required_id(new, new_key)
    return old, new, old != new


def _permission_change(fact):
    old = _snapshot_value(fact, "old_permission")
    new = _snapshot_value(fact, "new_permission")
    decision = fact.get("permission_decision")
    if isinstance(decision, Mapping):
        old = old if old is not None else decision.get("old")
        new = new if new is not None else decision.get("new")
    elif isinstance(decision, str) and new is None:
        new = decision
    if old is None and new is None:
        return None, None, False
    if old is None or new is None:
        raise ValueError("workflow invalidation requires old_permission and new_permission")
    old = _required_id(old, "old_permission").lower()
    new = _required_id(new, "new_permission").lower()
    return old, new, old != new


def _validate_claimed_invalidation(fact):
    changed = False
    reason = _reason(fact)
    if reason in {"head_changed"}:
        old, new, _changed = _changed_pair(fact, "old_head_sha", "new_head_sha")
        if old is None and new is None:
            raise ValueError("workflow invalidation requires old_head_sha and new_head_sha")
    elif reason in {"base_changed"}:
        old, new, _changed = _changed_pair(fact, "old_base_sha", "new_base_sha")
        if old is None and new is None:
            raise ValueError("workflow invalidation requires old_base_sha and new_base_sha")
    elif reason in {"base_ref_changed"}:
        old, new, _changed = _changed_pair(fact, "old_base_ref", "new_base_ref")
        if old is None and new is None:
            raise ValueError("workflow invalidation requires old_base_ref and new_base_ref")
    elif reason in {"permission_revoked", "permission_denied", "authorization_revoked"}:
        old, new, _changed = _permission_change(fact)
        if old is None and new is None:
            raise ValueError("workflow invalidation requires old_permission and new_permission")
    pairs = (
        ("old_head_sha", "new_head_sha", (), ()),
        ("old_base_sha", "new_base_sha", (), ()),
        ("old_base_ref", "new_base_ref", (), ()),
    )
    for old_key, new_key, old_aliases, new_aliases in pairs:
        _old, _new, pair_changed = _changed_pair(
            fact, old_key, new_key, old_aliases, new_aliases
        )
        changed = changed or pair_changed

    branch_exists = _snapshot_value(fact, "source_branch_exists", "branch_exists")
    if branch_exists is not None:
        if type(branch_exists) is not bool:
            raise ValueError("workflow source_branch_exists must be a boolean")
        changed = changed or branch_exists is False

    _old_permission, new_permission, permission_changed = _permission_change(fact)
    changed = changed or (
        permission_changed and new_permission in _REVOKED_PERMISSIONS
    )
    if reason in _INVALIDATING_REASONS and not changed:
        raise ValueError("workflow invalidation claim lacks proven evidence")
    return changed


def classify_subject_fact(fact):
    """Classify an observed subject fact without consulting mutable state."""

    identity = _identity(fact)
    del identity  # identity validation is the important side effect here.
    event_type = fact.get("event_type")
    if not isinstance(event_type, str):
        raise ValueError("workflow fact event_type must be a bounded string")
    event_type = event_type.strip().lower()
    if fact.get("duplicate") or fact.get("duplicate_of") or fact.get("no_change"):
        return IRRELEVANT

    source_state = _state(fact)
    new_source_state = fact.get("new_source_state")
    merged = fact.get("merged") is True or bool(fact.get("merged_at"))
    if source_state in _TERMINAL_STATES or (
        isinstance(new_source_state, str)
        and new_source_state.strip().lower() in _TERMINAL_STATES
    ) or merged:
        return TERMINAL

    claimed_invalidation = _has_invalidation_claim(fact)
    if claimed_invalidation and _validate_claimed_invalidation(fact):
        return INVALIDATING
    if claimed_invalidation:
        # Explicit old/new observations that are equal are a no-change fact.
        return IRRELEVANT

    if event_type in _IRRELEVANT_EVENT_TYPES:
        return IRRELEVANT
    status = fact.get("authorization_status")
    authorized = status in {
        "authorized_trigger",
        "authorized_system_trigger",
        "accepted_context",
        "accepted_review_participant",
    }
    if event_type in _ADDITIVE_EVENT_TYPES and authorized:
        return ADDITIVE
    return IRRELEVANT


def normalize_fact_evidence(fact, classification=None):
    """Return the bounded/redacted projection safe for binding metadata."""

    if classification is None:
        classification = classify_subject_fact(fact)
    if classification not in CLASSIFICATIONS:
        raise ValueError("unknown workflow fact classification")
    identity = _identity(fact)
    evidence = dict(identity)
    aliases = {
        "source_id": "source_id",
        "repo": "repo",
        "number": "number",
        "old_head_sha": "old_head_sha",
        "new_head_sha": "new_head_sha",
        "old_base_sha": "old_base_sha",
        "new_base_sha": "new_base_sha",
        "old_base_ref": "old_base_ref",
        "new_base_ref": "new_base_ref",
        "old_source_state": "old_source_state",
        "new_source_state": "new_source_state",
        "source_state": "source_state",
        "old_permission": "old_permission",
        "new_permission": "new_permission",
        "permission_decision": "permission_decision",
    }
    reason = _reason(fact)
    if classification == TERMINAL and not reason:
        reason = "source_merged" if (
            fact.get("merged") is True or fact.get("merged_at")
        ) else "source_closed"
    if classification == INVALIDATING and not reason:
        for old_key, new_key, derived in (
            ("old_head_sha", "new_head_sha", "head_changed"),
            ("old_base_sha", "new_base_sha", "base_changed"),
            ("old_base_ref", "new_base_ref", "base_ref_changed"),
        ):
            old, new = _pair(fact, old_key, new_key)
            if old is not None and new is not None and old != new:
                reason = derived
                break
        if not reason and _snapshot_value(fact, "source_branch_exists", "branch_exists") is False:
            reason = "branch_disappeared"
        if not reason:
            _old_permission, new_permission, permission_changed = _permission_change(fact)
            if permission_changed and new_permission in _REVOKED_PERMISSIONS:
                reason = "permission_revoked"
    if reason:
        evidence["reason_code"] = _required_id(reason, "reason_code")
    source_state = _state(fact)
    if classification == TERMINAL and source_state:
        evidence["source_state"] = source_state
    for target, source in aliases.items():
        value = fact.get(source)
        if value is None:
            continue
        if target == "number":
            if type(value) is not int or value <= 0:
                raise ValueError("workflow fact number must be a positive integer")
            evidence[target] = value
        elif target == "permission_decision":
            if isinstance(value, Mapping):
                decision = {}
                for key in ("old", "new"):
                    if key in value:
                        decision[key] = _required_id(value[key], f"permission_{key}").lower()
                evidence[target] = decision
            else:
                evidence[target] = _required_id(value, target).lower()
        elif target == "source_state":
            evidence[target] = _required_id(value, target).lower()
        else:
            evidence[target] = _required_id(value, target)
    branch_exists = _snapshot_value(fact, "source_branch_exists", "branch_exists")
    if branch_exists is not None:
        if type(branch_exists) is not bool:
            raise ValueError("workflow source_branch_exists must be a boolean")
        evidence["source_branch_exists"] = branch_exists
    # Keep the event id and classification in one small, deterministic record.
    evidence["classification"] = classification
    import json

    if len(json.dumps(evidence, ensure_ascii=False, sort_keys=True).encode("utf-8")) > _MAX_EVIDENCE_BYTES:
        raise ValueError("workflow fact evidence is bounded")
    return evidence


def classify_and_normalize(fact):
    """Return the classification and its safe evidence projection."""

    classification = classify_subject_fact(fact)
    return classification, normalize_fact_evidence(fact, classification)
