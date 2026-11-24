"""Caller-transaction storage for supervision observations and incidents."""

from robert_agent.foundation.contracts.supervision import IncidentStatus
from robert_agent.supervision import receipts as supervision_receipts
from robert_agent.persistence.queries import supervision_retirement as db_read_supervision_retirement

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import supervision_checkpoints as db_read_supervision_checkpoints
from robert_agent.persistence.queries import supervision_incidents as db_read_supervision_incidents
from robert_agent.persistence.stores import supervision_checkpoints as db_write_supervision_checkpoints
from robert_agent.persistence.stores import supervision_incidents as db_write_supervision_incidents

from robert_agent.persistence.queries import schema_migrations as db_read_schema_migrations
from robert_agent.persistence.queries import sqlite_master as db_read_sqlite_master
from robert_agent.persistence.queries import tasks as db_read_tasks

import json
from uuid import uuid4

from robert_agent.persistence.schema import database as storage
from robert_agent.supervision.rules import timestamp


_ACTIVE = "status NOT IN ('resolved','dismissed')"
_SCOPE = "scope_type=? AND scope_id=? AND rule_id=?"


def has_schema(conn):
    """Report whether the built-in supervision migration completed."""
    tables = {row[0] for row in db_read_sqlite_master.find_for_has_schema(conn)}
    if len(tables) != 4:
        return False
    row = db_records.first(db_read_schema_migrations.find_checksum_by_name(conn, storage.SUPERVISION_MIGRATION_NAME))
    return row is not None and row[0] == storage.SUPERVISION_MIGRATION_CHECKSUM


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _decode(columns, row):
    result = {}
    for (name, *_), value in zip(columns, row):
        if name.endswith("_json"):
            result[name[:-5]] = json.loads(value)
        elif name == "lineage_key":
            result[name] = value or None
        else:
            result[name] = value
    return result


def _one(cursor):
    row = db_records.first(cursor)
    return _decode([(key,) for key in cursor[0].keys()] if cursor else [], row) if row is not None else None


def list_incidents(conn, status=None, limit=100):
    rows = db_read_supervision_incidents.list_incidents(conn, status, max(0, min(limit, 1000)))
    return [_decode([(key,) for key in row.keys()], row) for row in rows]


def get_checkpoint(conn, scope_type, scope_id, rule_id):
    return _one(db_read_supervision_checkpoints.find_for_get_checkpoint(conn, scope_type, scope_id, rule_id))


def update_checkpoint(conn, scope_type, scope_id, rule_id, *, now, metadata):
    """Replace metadata owned by a named checkpoint without replacing its observation."""
    db_write_supervision_checkpoints.update_checkpoint(conn, scope_type, scope_id, rule_id, now, _json(metadata))


def _record_observation(conn, key, observation, now):
    progress_token = observation.get("progress_token")
    if progress_token is not None and not isinstance(progress_token, str):
        progress_token = _json(progress_token)
    db_write_supervision_checkpoints.record_observation(
        conn,
        *(
            *key,
            observation["classification"],
            observation.get("reason_code"),
            _json(observation.get("evidence") or {}),
            progress_token,
            observation.get("progress_observed_at") or now if progress_token is not None else None,
            observation.get("next_check_at"),
            now,
            observation.get("eligible_since"),
        ),
    )


def _confirmed(conn, key, observation, active, now):
    values = (
        observation["reason_code"],
        observation["severity"],
        _json(observation["evidence"]),
        _json(observation["expected_progress"]),
        now,
        observation.get("next_check_at"),
    )
    if active:
        db_write_supervision_incidents.update_for_confirmed(conn, *(*values, active[0]))
        return None
    previous = db_records.first(db_read_supervision_incidents.find_for_confirmed(conn, *key))
    incident_id = f"incident-{uuid4().hex}"
    db_write_supervision_incidents.insert_for_confirmed(
        conn, *(incident_id, *key, *values, now, previous[0] if previous else None)
    )
    return incident_id


def apply_observations(conn, observations, now):
    """Persist validated observations and return incident transitions.

    The caller holds a writer transaction from revalidation through commit.
    Evidence must already be bounded and redacted. A rule can resolve an
    incident with `healthy` and timestamped positive domain evidence for the
    same scope, rule and lineage. Omitting an observation does nothing.
    """
    changes = []
    for observation in observations:
        key = (
            observation["scope_type"],
            observation["scope_id"],
            observation["rule_id"],
            observation.get("lineage_key") or "",
        )
        _record_observation(conn, key, observation, now)
        classification = observation["classification"]
        positive = observation.get("positive_evidence")
        occurred_at = timestamp(positive.get("occurred_at")) if isinstance(positive, dict) else None
        can_resolve = classification == "healthy" and occurred_at is not None and occurred_at <= timestamp(now)
        if classification != "confirmed" and not can_resolve:
            continue
        active = db_records.first(db_read_supervision_incidents.find_for_apply_observations(conn, *key))
        if classification == "confirmed":
            incident_id = _confirmed(conn, key, observation, active, now)
            if incident_id is None:
                continue
            transition = "opened"
        elif active and active[1] in {
            IncidentStatus.OPEN.value,
            IncidentStatus.DIAGNOSING.value,
            IncidentStatus.NEEDS_ATTENTION.value,
        }:
            if observation["rule_id"] == "ready_not_dispatched":
                expected_attempt = json.loads(active[2]).get("attempt_id")
                opened_at = timestamp(active[3])
                if (
                    not expected_attempt
                    or positive.get("attempt_id") != expected_attempt
                    or not opened_at
                    or occurred_at < opened_at
                ):
                    continue
            incident_id = active[0]
            db_write_supervision_incidents.update_for_apply_observations(conn, now, now, _json(positive), incident_id)
            transition = "resolved"
        else:
            continue
        incident = _one(db_read_supervision_incidents.find_record_by_incident_id(conn, incident_id))
        changes.append({"transition": transition, "incident": incident})
    return changes


def repair_task(conn, task_id):
    """Return automatic maintenance provenance, if any, for a dispatch target."""
    row = db_records.first(db_read_tasks.find_for_repair_task(conn, task_id))
    return json.loads(row[0]) if row and row[0] else None


def dismiss_terminal_tasks(conn, task_ids, now):
    """Recheck terminal ownership and known effects in the caller's writer."""
    changes = []
    for candidate in db_read_supervision_retirement.candidates(conn, task_ids, now):
        evidence = dict(candidate)
        occurred_at = timestamp(evidence["terminal_at"])
        if occurred_at is None or occurred_at > timestamp(now):
            continue
        actions = db_read_supervision_retirement.publications(conn, evidence["task_id"])
        if not _settled_task(evidence, actions, now):
            continue
        incident_id = evidence.pop("incident_id")
        evidence.update(reason_code="terminal_task_no_remaining_work", action_ids=[a["action_id"] for a in actions])
        db_write_supervision_incidents.dismiss_terminal(conn, incident_id, now, _json(evidence))
        incident = _one(db_read_supervision_incidents.find_record_by_incident_id(conn, incident_id))
        changes.append({"transition": "dismissed", "incident": incident})
    return changes


def settled_merged_reviews(conn, workstream_ids, now):
    """Prove obsolete review requests have no remaining work without changing history."""
    candidates = db_read_supervision_retirement.merged_reviews(conn, workstream_ids, now)
    if len(candidates) > 100:
        return []
    proofs = []
    for candidate in candidates:
        actions = db_read_supervision_retirement.publications(conn, candidate["task_id"])
        if _settled_task(candidate, actions, now):
            proofs.append({**dict(candidate), "action_ids": [action["action_id"] for action in actions]})
    return proofs


def _settled_task(evidence, actions, now):
    occurred_at = timestamp(evidence["terminal_at"])
    if occurred_at is None or occurred_at > timestamp(now):
        return False
    if len(actions) > 100 or not all(_settled_publication(action, now) for action in actions):
        return False
    if evidence["task_lifecycle"] != "failed":
        return True
    if not actions:
        return False
    for action in actions:
        metadata = json.loads(action["result_metadata"] or "{}")
        publish = json.loads(action["metadata_json"]).get("publish", {})
        audit = metadata.get("audit") if isinstance(metadata, dict) else None
        if (
            action["action_type"] != "submit_pr_review"
            or action["publish_status"] != "skipped"
            or action["audit_status"] != "accepted"
            or publish.get("reason_code") != "superseded"
            or not timestamp(publish.get("skipped_at"))
            or timestamp(publish["skipped_at"]) > timestamp(now)
            or not isinstance(audit, dict)
            or audit.get("status") != "accepted"
            or action["result_task_id"] != evidence["task_id"]
        ):
            return False
    return True


def _settled_publication(action, now):
    try:
        payload = json.loads(action["metadata_json"])
        metadata = json.loads(action["result_metadata"] or "{}")
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, dict) or action["intent_evidence_valid"] == 0:
        return False
    publish = payload.get("publish", {})
    if not isinstance(publish, dict):
        return False
    if publish.get("reconcile_required"):
        return False
    if action["publish_status"] == "skipped":
        return action["intent_status"] in (None, "confirmed_absent")
    receipt = publish.get("receipt")
    if not isinstance(metadata, dict) or not isinstance(metadata.get("audit"), dict):
        return False
    audit = metadata["audit"]
    scope = audit.get("action_scope")
    confirmed = timestamp(receipt.get("confirmed_at")) if isinstance(receipt, dict) else None
    return bool(
        action["publish_status"] == "published"
        and action["audit_status"] == "accepted"
        and audit.get("status") == "accepted"
        and action["result_task_id"] == action["task_id"]
        and payload.get("type") == action["action_type"]
        and isinstance(scope, dict)
        and scope
        and isinstance(receipt, dict)
        and receipt.get("schema_version") == 1
        and confirmed
        and confirmed <= timestamp(now)
        and supervision_receipts.matches({"result_id": action["result_id"]}, action, scope, receipt)
    )
