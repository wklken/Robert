"""Typed recovery requests consumed under existing Robert domain ownership."""

from robert_agent.foundation.contracts.supervision import SupervisionMode

from robert_agent.supervision import decisions as model_decisions
from robert_agent.supervision.policy import current_config
from robert_agent.persistence.queries import supervision_decisions as decision_queries
from robert_agent.foundation.contracts.delivery import AuditStatus, PublicationStatus, PublishStatus
from robert_agent.foundation.contracts.execution import AttemptStatus
from robert_agent.foundation.contracts.supervision import RecoveryActionKind, RecoveryStatus
from robert_agent.foundation.contracts.work import TaskLifecycle, WorkstreamLifecycle

from robert_agent.persistence.queries import github_actions as db_read_github_actions
from robert_agent.persistence.queries import supervision_recovery_actions as db_read_supervision_recovery_actions
from robert_agent.persistence.queries import tasks as db_read_tasks

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import attempts as db_read_attempts
from robert_agent.persistence.queries import worker_results as db_read_worker_results

from robert_agent.persistence import transactions as db_transactions

from robert_agent.persistence.queries import supervision_incidents as db_read_supervision_incidents
from robert_agent.persistence.queries import task_events as db_read_task_events
from robert_agent.persistence.stores import notifications as db_write_notifications
from robert_agent.persistence.stores import supervision_incidents as db_write_supervision_incidents
from robert_agent.persistence.stores import supervision_recovery_actions as db_write_supervision_recovery_actions

from datetime import timedelta
import hashlib
import json
from uuid import uuid4

from robert_agent.security import redaction
from robert_agent.supervision import store as supervision_store
from robert_agent.configuration.supervision import RECOVERY_STAGES as STAGES
from robert_agent.supervision.rules import timestamp


MAX_STAGE_ATTEMPTS = 1
MAX_LINEAGE_ATTEMPTS = 3
ACTION_TIMEOUT_SECONDS = 300
RECHECK_SECONDS = 60
_ACTIVE = "status IN ('requested','applying','observing','unknown')"


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _digest(value):
    return hashlib.sha256(_encode(value).encode()).hexdigest()


def _after(now, seconds):
    return (timestamp(now) + timedelta(seconds=seconds)).isoformat()


def _due(now, value):
    parsed = timestamp(value)
    return value is None or (parsed is not None and parsed <= timestamp(now))


def _retry_facts(state):
    return {
        key: state[key]
        for key in (
            "attempt_count",
            "next_retry_at",
            "first_failed_at",
            "last_attempt_at",
            "retryable",
            "reconcile_required",
            "status",
        )
        if key in state
    }


def _action_payload_digest(metadata):
    body = json.loads(metadata)
    body.pop("publish", None)
    return _digest(body)


def _policy(config, repo, kind):
    settings = config.get("supervision", {})
    recovery = settings.get("recovery") or {}
    allowed = recovery.get("allowed_actions", ())
    repositories = recovery.get("repositories", ())
    configured = next((item for item in config.get("repos", ()) if item["full_name"] == repo), None)
    if (
        settings.get("mode") not in {SupervisionMode.RECOVER_KNOWN.value, SupervisionMode.DECIDE.value}
        or not config.get("daemon", {}).get("enabled", True)
        or kind not in STAGES
        or kind not in allowed
        or repo not in repositories
        or not configured
        or not configured.get("enabled", True)
    ):
        return None
    return _digest(
        [
            "supervision-recovery-v1",
            sorted(allowed),
            sorted(repositories),
            configured.get("github_account"),
            configured.get("default_base_branch"),
            sorted(configured.get("trusted_actors", ())),
        ]
    )


def _task(conn, task_id):
    return next((dict(row) for row in db_read_tasks.find_for_task(conn, task_id)), None)


def _target(conn, task, kind, target_id, now, generation):
    """Return immutable and eligibility facts used by both plan and claim."""
    if (
        not task
        or not task["lineage_key"]
        or not task["enabled"]
        or not task["runnable"]
        or task["canceled"]
        or task["source_state"] == "closed"
        or task["stream_lifecycle"] != WorkstreamLifecycle.ACTIVE
        or task["active_task_id"] != task["task_id"]
    ):
        return None
    task_id = task["task_id"]
    if kind == RecoveryActionKind.HANDOFF_RESUME:
        if target_id != task_id or task["lifecycle"] != TaskLifecycle.COMPLETED:
            return None
        handoff = json.loads(task["metadata_json"]).get("pending_handoff") or {}
        if (
            handoff.get("status") != "pending"
            or handoff.get("retryable") is False
            or not _due(now, handoff.get("next_retry_at"))
        ):
            return None
        if db_records.first(db_read_tasks.find_for_target(conn, task_id)):
            return None
        events = [row[0] for row in db_read_task_events.find_for_target(conn, task_id)]
        if not events or len(events) > 100:
            return None
        return {
            "workstream_id": task["workstream_id"],
            "event_ids": events,
            "retry_at": handoff.get("next_retry_at"),
            "attempt_count": handoff.get("attempt_count"),
        }
    if task["lifecycle"] not in {TaskLifecycle.QUEUED, TaskLifecycle.RUNNING}:
        return None
    attempt = next((dict(row) for row in db_read_attempts.find_for_target(conn, task_id)), None)
    if not attempt:
        return None
    if kind == RecoveryActionKind.SPOOL_REPLAY:
        if (
            not isinstance(generation, str)
            or not generation
            or attempt["attempt_id"] != target_id
            or attempt["status"] not in {AttemptStatus.PREPARED, AttemptStatus.RUNNING, AttemptStatus.STALE}
            or db_records.first(db_read_worker_results.find_for_target(conn, task_id, target_id))
        ):
            return None
        return {"attempt_id": target_id, "spool_fingerprint": generation}
    if attempt["status"] != AttemptStatus.COMPLETED or (type(attempt["pid"]) is int and not attempt["stopped_at"]):
        return None
    result = next(
        (dict(row) for row in db_read_worker_results.find_for_target_2(conn, task_id, attempt["attempt_id"])), None
    )
    if not result:
        return None
    meta = json.loads(result["metadata_json"])
    result_facts = {
        "attempt_id": attempt["attempt_id"],
        "result_id": result["result_id"],
        "submission_fingerprint": meta.get("submission_fingerprint"),
    }
    if kind == RecoveryActionKind.AUDIT_RETRY:
        state = meta.get("audit_observation") or {}
        if (
            result["result_id"] != target_id
            or meta.get("audit") is not None
            or state.get("retryable") is False
            or not _due(now, state.get("next_retry_at"))
        ):
            return None
        return {**result_facts, "audit_retry": _retry_facts(state)}
    if (meta.get("audit") or {}).get("status") != AuditStatus.ACCEPTED.value:
        return None
    action = next(
        (
            dict(row)
            for row in db_read_github_actions.find_for_target(conn, now, target_id, task_id, result["result_id"])
        ),
        None,
    )
    if not action or (kind == RecoveryActionKind.PUBLICATION_RECONCILE) != (
        action["status"] == PublicationStatus.EXECUTING.value
    ):
        return None
    state = json.loads(action["metadata_json"]).get("publish") or {}
    return {
        **result_facts,
        "action_id": target_id,
        "publication_id": action["publication_id"],
        "request_fingerprint": action["request_fingerprint"],
        "intent_status": action["status"],
        "action_payload_digest": _action_payload_digest(action["metadata_json"]),
        "audited_scope_digest": _digest(meta["audit"].get("action_scope")),
        "publication_retry": _retry_facts(state),
    }


def _generation(task, target):
    return _digest([task["task_id"], task["lineage_key"], task["repo"], task["lifecycle"], target])


def _candidate(observation):
    if observation.get("scope_type") != "task" or not observation.get("lineage_key"):
        return None
    evidence = observation.get("evidence") or {}
    reason = observation.get("reason_code")
    if (
        reason == "publication_effect_unresolved"
        and evidence.get("intent_status") == PublicationStatus.EXECUTING.value
        and observation.get("classification") == "confirmed"
    ):
        return RecoveryActionKind.PUBLICATION_RECONCILE.value, evidence.get("action_id"), None
    if observation.get("classification") not in {"suspect", "confirmed"}:
        return None
    if reason == "result_waiting_ingestion":
        return RecoveryActionKind.SPOOL_REPLAY.value, evidence.get("attempt_id"), evidence.get("spool_fingerprint")
    if reason == "result_waiting_audit":
        return RecoveryActionKind.AUDIT_RETRY.value, evidence.get("result_id"), None
    if reason == "publication_waiting_consumer":
        return RecoveryActionKind.PUBLICATION_RETRY.value, evidence.get("action_id"), None
    if reason == "handoff_waiting_consumer":
        return RecoveryActionKind.HANDOFF_RESUME.value, observation["scope_id"], None
    return None


def _budget_block(conn, lineage, stage):
    if db_records.first(db_read_supervision_recovery_actions.find_for_budget_block_2(conn, lineage)):
        return "recovery_already_active"
    total, stage_total = db_records.first(
        db_read_supervision_recovery_actions.find_for_budget_block(conn, stage, lineage)
    )
    if total >= MAX_LINEAGE_ATTEMPTS or stage_total >= MAX_STAGE_ATTEMPTS:
        return "recovery_budget_exhausted"
    return None


def _reserve(conn, task, kind, target_id, target, policy, now, *, expected=None, rule=None):
    lineage, stage, task_id = task["lineage_key"], STAGES[kind], task["task_id"]
    blocked = _budget_block(conn, lineage, stage)
    if blocked:
        return None, blocked
    ordinal = db_records.first(db_read_supervision_recovery_actions.find_for_reserve(conn, lineage, task_id, kind))[0]
    version = _generation(task, target)
    key = _digest([lineage, task_id, kind, target_id, version])
    incident = (
        db_records.first(db_read_supervision_incidents.find_for_reserve(conn, task_id, lineage, rule)) if rule else None
    )
    action_id = "recovery-" + uuid4().hex
    inserted = db_write_supervision_recovery_actions.insert_for_reserve(
        conn,
        action_id,
        incident[0] if incident else None,
        lineage,
        task_id,
        stage,
        kind,
        ordinal,
        key,
        target_id,
        version,
        policy,
        _encode(target),
        _encode(expected or {}),
        now,
        _after(now, RECHECK_SECONDS),
    )
    if not inserted:
        return None, "recovery_already_recorded"
    return next(
        (dict(row) for row in db_read_supervision_recovery_actions.find_record_by_action_id(conn, action_id)), None
    ), None


def plan(conn, *, observations, now, config, limit=8):
    """Reserve at most ``limit`` requests. The caller owns a short writer transaction."""
    config = current_config(conn, config)
    if not config or (config.get("supervision") or {}).get("mode") not in {
        SupervisionMode.RECOVER_KNOWN.value,
        SupervisionMode.DECIDE.value,
    }:
        return []
    if not conn.in_transaction:
        db_transactions.begin(conn, immediate=True)
    requests = []
    for observation in observations:
        if len(requests) >= min(limit, 32):
            break
        candidate = _candidate(observation)
        if not candidate:
            continue
        kind, target_id, generation = candidate
        if not isinstance(target_id, str) or not target_id:
            continue
        task = _task(conn, observation["scope_id"])
        if not task or task["lineage_key"] != observation["lineage_key"]:
            continue
        policy = _policy(config, task["repo"], kind)
        target = _target(conn, task, kind, target_id, now, generation) if policy else None
        if not target:
            continue
        workstream_id = decision_queries.task(conn, task["task_id"])["workstream_id"]
        if not model_decisions.permit(
            conn,
            config,
            workstream_id=workstream_id,
            action=kind,
            target_id=target_id,
            facts={"task_id": task["task_id"], "target_generation": _generation(task, target)},
            now=now,
        ):
            continue
        action, reason = _reserve(
            conn,
            task,
            kind,
            target_id,
            target,
            policy,
            now,
            expected=observation.get("expected_progress"),
            rule=observation.get("rule_id"),
        )
        if action:
            model_decisions.executed(conn, config, workstream_id, {"recovery_action_id": action["action_id"]}, now)
            requests.append(action)
        elif reason == "recovery_budget_exhausted":
            _budget_notice(conn, task, kind, now)
    return requests


def _budget_notice(conn, task, kind, now):
    identifier = "supervision-budget-" + _digest([task["lineage_key"], STAGES[kind]])[:24]
    _notice(conn, identifier, task["task_id"], "recovery_budget_exhausted", now)


def _notice(conn, identifier, task_id, reason, now):
    db_write_notifications.insert_for_notice(
        conn,
        identifier,
        task_id,
        now,
        _encode(
            {
                "reason_code": reason,
                "retryable": False,
                "required_action": "Inspect the recovery evidence before attempting another action.",
            }
        ),
    )


def _attention(conn, action, reason, now):
    if action.get("incident_id"):
        db_write_supervision_incidents.update_for_attention(conn, reason, now, action["incident_id"])
    _notice(conn, "supervision-recovery-" + action["action_id"], action["scope_id"], reason, now)


def _blocked(reason):
    return {"status": "blocked", "reason_code": reason}


def blocks_consumer(conn, *, task_id, kind, target_id, now, config, native_retry=False):
    """Read-only scheduling hint. The execution boundary still calls begin."""
    del now  # Expiration is reconciled durably by verify, never inferred here.
    if kind not in STAGES or not supervision_store.has_schema(conn):
        return False
    active = db_records.first(db_read_supervision_recovery_actions.find_for_blocks_consumer(conn, task_id))
    if active:
        if tuple(active[:2]) == (kind, target_id):
            return active[2] != RecoveryStatus.REQUESTED.value
        return active[2] in {RecoveryStatus.APPLYING.value, RecoveryStatus.UNKNOWN.value} or native_retry
    # Scheduling is advisory. Native retries retain their own policy, and
    # actual supervised claims revalidate configuration at the effect boundary.
    return False


def begin(conn, *, task_id, kind, target_id, now, config, generation=None, native_retry=False):
    """Claim after original gates and within the consumer's existing domain lock.

    The caller commits this claim before external I/O. The legacy verdict carries
    no added authority and leaves all original policy and budgets in force.
    """
    if kind not in STAGES:
        raise ValueError("unsupported supervision recovery action")
    if not supervision_store.has_schema(conn):
        return {"status": "legacy"}
    # Ordinary domain work must not acquire a new writer or spend a recovery
    # budget. Only a previously planned supervision action grants extra work.
    if not next((dict(row) for row in db_read_supervision_recovery_actions.find_for_begin_4(conn, task_id)), None):
        if native_retry and model_decisions.enabled(config or {}):
            return _blocked("waiting_for_model_decision")
        return {"status": "legacy"}
    # Caller may already own a write transaction in atomic consumers.
    if not conn.in_transaction:
        db_transactions.begin(conn, immediate=True)
    action = next((dict(row) for row in db_read_supervision_recovery_actions.find_for_begin_3(conn, task_id)), None)
    if action and (action["action_kind"], action["target_id"]) != (kind, target_id):
        if action["status"] in {RecoveryStatus.APPLYING.value, RecoveryStatus.UNKNOWN.value}:
            return _blocked("recovery_scope_occupied")
        # Ordinary domain work may be the predecessor of a queued request.
        if not native_retry:
            return {"status": "legacy"}
        return _blocked("recovery_scope_occupied")
    if action and action["status"] != RecoveryStatus.REQUESTED.value:
        return _blocked("recovery_already_active")
    if not action:
        return {"status": "legacy"}
    task = _task(conn, task_id)
    config = current_config(conn, config)
    if config is None:
        return _blocked("recovery_policy_unavailable")
    policy = _policy(config, task["repo"], kind) if task else None
    target = _target(conn, task, kind, target_id, now, generation) if policy else None
    if (
        not target
        or policy != action["policy_version"]
        or _generation(task, target) != action["target_generation"]
        or _due(now, _after(action["created_at"], ACTION_TIMEOUT_SECONDS))
    ):
        db_write_supervision_recovery_actions.update_for_begin_2(conn, now, action["action_id"])
        return _blocked("recovery_precondition_changed")
    owner = "consumer-" + uuid4().hex
    changed = db_write_supervision_recovery_actions.update_for_begin(
        conn,
        owner,
        _after(now, ACTION_TIMEOUT_SECONDS),
        now,
        _after(now, RECHECK_SECONDS),
        action["action_id"],
        action["claim_generation"],
    )
    if not changed:
        return _blocked("recovery_claim_conflict")
    claimed = next(
        (dict(row) for row in db_read_supervision_recovery_actions.find_record_by_action_id(conn, action["action_id"])),
        None,
    )
    if claimed["incident_id"]:
        db_write_supervision_incidents.update_for_begin(conn, claimed["incident_id"])
    return {"status": "claimed", "claim": claimed}


def _fenced(conn, claim, config, now):
    row = next(
        (
            dict(row)
            for row in db_read_supervision_recovery_actions.find_for_fenced(
                conn, claim["action_id"], claim["claim_owner"], claim["claim_generation"]
            )
        ),
        None,
    )
    if not row or _due(now, row["claim_expires_at"]):
        return None, None
    task = _task(conn, row["scope_id"])
    config = current_config(conn, config)
    if not task or config is None or _policy(config, task["repo"], row["action_kind"]) != row["policy_version"]:
        return None, None
    return row, task


def current(conn, claim, *, config, now, generation=None):
    """Revalidate the same fence and target after preparatory I/O."""
    row, task = _fenced(conn, claim, config, now)
    if not row:
        return False
    target = _target(conn, task, row["action_kind"], row["target_id"], now, generation)
    return bool(target and _generation(task, target) == row["target_generation"])


def publication_current(conn, claim, *, config, now):
    """Fence the external effect after the owner's own intent/charge writes."""
    row, task = _fenced(conn, claim, config, now)
    if (
        not row
        or row["action_kind"] != RecoveryActionKind.PUBLICATION_RETRY.value
        or task["lineage_key"] != row["lineage_key"]
        or task["canceled"]
        or not task["enabled"]
        or not task["runnable"]
        or task["source_state"] == "closed"
        or task["lifecycle"] not in {TaskLifecycle.QUEUED, TaskLifecycle.RUNNING}
        or task["stream_lifecycle"] != WorkstreamLifecycle.ACTIVE
        or task["active_task_id"] != row["scope_id"]
    ):
        return False
    facts = json.loads(row["preconditions_json"])
    actual = next(
        (
            dict(row)
            for row in db_read_github_actions.find_for_publication_current(
                conn, row["target_id"], row["scope_id"], facts["result_id"], facts["attempt_id"]
            )
        ),
        None,
    )
    return bool(
        actual
        and actual["submission_fingerprint"] == facts["submission_fingerprint"]
        and _action_payload_digest(actual["metadata_json"]) == facts["action_payload_digest"]
        and _digest(json.loads(actual["audited_scope"] or "null")) == facts["audited_scope_digest"]
        and (not facts["publication_id"] or actual["publication_id"] == facts["publication_id"])
        and (not facts["request_fingerprint"] or actual["request_fingerprint"] == facts["request_fingerprint"])
    )


def _safe_receipt(receipt):
    encoded = _encode(receipt)
    safe = redaction.redact_text(encoded) if len(encoded) <= 16000 else {"ok": False}
    return safe["text"] if safe["ok"] else '{"details_omitted": true}'


def finish(conn, claim, *, outcome, receipt, now):
    """Persist the domain receipt under the same fence. Return False for stale writers."""
    if outcome not in {
        RecoveryStatus.OBSERVING.value,
        RecoveryStatus.FAILED.value,
        RecoveryStatus.REJECTED.value,
        RecoveryStatus.UNKNOWN.value,
    } or not isinstance(receipt, dict):
        raise ValueError("invalid recovery outcome or receipt")
    row = next(
        (
            dict(row)
            for row in db_read_supervision_recovery_actions.find_for_finish(
                conn, claim["action_id"], claim["claim_owner"], claim["claim_generation"]
            )
        ),
        None,
    )
    if not row or (row["status"] == RecoveryStatus.UNKNOWN.value and outcome == RecoveryStatus.REJECTED.value):
        return False
    changed = db_write_supervision_recovery_actions.update_for_finish(
        conn,
        outcome,
        _safe_receipt(receipt),
        now,
        _after(now, RECHECK_SECONDS)
        if outcome in {RecoveryStatus.OBSERVING.value, RecoveryStatus.UNKNOWN.value}
        else None,
        outcome,
        row["action_id"],
        row["status"],
        row["claim_owner"],
        row["claim_generation"],
    )
    if changed:
        if outcome == RecoveryStatus.OBSERVING.value and row["incident_id"]:
            db_write_supervision_incidents.update_for_finish(conn, row["incident_id"])
        elif outcome in {RecoveryStatus.FAILED.value, RecoveryStatus.UNKNOWN.value}:
            _attention(conn, row, "recovery_" + outcome, now)
        elif outcome == RecoveryStatus.REJECTED.value and row["incident_id"]:
            db_write_supervision_incidents.update_for_finish_2(conn, row["incident_id"])
    return bool(changed)


def _verified(conn, action):
    kind, task_id, target_id = action["action_kind"], action["scope_id"], action["target_id"]
    facts = json.loads(action["preconditions_json"])
    task = _task(conn, task_id)
    if (
        not task
        or task["canceled"]
        or task["lifecycle"] in {TaskLifecycle.CANCELED, TaskLifecycle.IGNORED}
        or task["lineage_key"] != action["lineage_key"]
    ):
        return None
    if kind == RecoveryActionKind.SPOOL_REPLAY:
        result = next(
            (
                dict(row)
                for row in db_read_worker_results.find_for_verified_2(
                    conn, task_id, target_id, facts.get("spool_fingerprint")
                )
            ),
            None,
        )
        return (
            {"result_id": result["result_id"], "attempt_id": target_id, "occurred_at": result["created_at"]}
            if result
            else None
        )
    if kind == RecoveryActionKind.AUDIT_RETRY:
        row = db_records.first(
            db_read_worker_results.find_for_verified(
                conn, task_id, target_id, facts.get("attempt_id"), facts.get("submission_fingerprint")
            )
        )
        return (
            {"result_id": target_id, "audit_status": AuditStatus.ACCEPTED.value, "occurred_at": row[0]} if row else None
        )
    if kind == RecoveryActionKind.HANDOFF_RESUME:
        child = (json.loads(task["metadata_json"]).get("pending_handoff") or {}).get("child_task_id")
        if not child:
            return None
        row = db_records.first(db_read_tasks.find_for_verified(conn, child, task_id, facts.get("workstream_id")))
        if row and row[0] in facts.get("event_ids", ()):
            return {"successor_task_id": child, "event_id": row[0], "occurred_at": row[1]}
        return None
    return _verified_publication(conn, action, facts)


def _verified_publication(conn, action, facts):
    row = next(
        (
            dict(row)
            for row in db_read_github_actions.find_for_verified_publication(
                conn, action["target_id"], action["scope_id"], facts["result_id"]
            )
        ),
        None,
    )
    if (
        not row
        or row["audit_status"] != AuditStatus.ACCEPTED
        or row["submission_fingerprint"] != facts["submission_fingerprint"]
        or (facts.get("publication_id") and facts["publication_id"] != row["publication_id"])
        or (facts.get("request_fingerprint") and facts["request_fingerprint"] != row["request_fingerprint"])
    ):
        return None
    evidence = json.loads(row["remote_evidence_json"])
    if (
        action["action_kind"] == RecoveryActionKind.PUBLICATION_RECONCILE.value
        and row["intent_status"] == PublicationStatus.CONFIRMED_ABSENT.value
        and evidence.get("status") == "confirmed_absent"
    ):
        return {"action_id": row["action_id"], "publication_id": row["publication_id"], "effect": "confirmed_absent"}
    receipt = (json.loads(row["metadata_json"]).get("publish") or {}).get("receipt")
    if row["publish_status"] != PublishStatus.PUBLISHED or not isinstance(receipt, dict) or not row["audited_scope"]:
        return None
    from robert_agent.supervision.receipts import matches

    if matches(
        facts, {**row, "intent_receipt": _encode(evidence.get("receipt"))}, json.loads(row["audited_scope"]), receipt
    ):
        return {
            "action_id": row["action_id"],
            "publication_id": row["publication_id"],
            "effect": "confirmed_effect",
            "occurred_at": receipt["confirmed_at"],
        }
    return None


def _unapplied_audit(conn, action, now):
    # Audit inspection is read-only. Its domain write and finish share one
    # transaction after fence validation, so an expired claim with neither
    # write can release the scope while keeping its budget charged.
    expires = timestamp(action["claim_expires_at"])
    if (
        action["action_kind"] != RecoveryActionKind.AUDIT_RETRY.value
        or action["status"] != RecoveryStatus.APPLYING.value
        or not action["claim_owner"]
        or action["claim_generation"] < 1
        or expires is None
        or expires > timestamp(now)
        or json.loads(action["receipt_json"]) != {}
    ):
        return False
    facts = json.loads(action["preconditions_json"])
    return (
        db_records.first(
            db_read_worker_results.find_for_unapplied_audit(
                conn,
                action["scope_id"],
                action["target_id"],
                facts.get("attempt_id"),
                facts.get("submission_fingerprint"),
            )
        )
        is not None
    )


def verify(conn, *, observations, now, limit=32):
    """Advance due action records from independently read domain evidence."""
    del observations  # A sampled absence or a model conclusion never verifies a recovery.
    if not conn.in_transaction:
        db_transactions.begin(conn, immediate=True)
    due = [
        dict(row) for row in db_read_supervision_recovery_actions.find_for_verify(conn, now, max(0, min(limit, 100)))
    ]
    changes = []
    for action in due:
        evidence = _verified(conn, action)
        state = action["status"]
        if evidence:
            db_write_supervision_recovery_actions.update_for_verify(
                conn, now, _encode(evidence), action["action_id"], state
            )
            if action["incident_id"] and evidence.get("effect") != "confirmed_absent":
                db_write_supervision_incidents.update_for_verify(
                    conn, now, now, _encode(evidence), action["incident_id"]
                )
            elif action["incident_id"]:
                db_write_supervision_incidents.update_for_verify_2(conn, action["incident_id"])
            changes.append(
                {"action_id": action["action_id"], "status": RecoveryStatus.SUCCEEDED.value, "evidence": evidence}
            )
            continue
        deadline = (
            action["claim_expires_at"]
            if state == RecoveryStatus.APPLYING.value
            else _after(action["finished_at"] or action["created_at"], ACTION_TIMEOUT_SECONDS)
        )
        if state in {
            RecoveryStatus.REQUESTED.value,
            RecoveryStatus.APPLYING.value,
            RecoveryStatus.OBSERVING.value,
        } and _due(now, deadline):
            if state == RecoveryStatus.REQUESTED.value:
                outcome, reason = RecoveryStatus.REJECTED.value, "recovery_request_expired"
            elif _unapplied_audit(conn, action, now):
                outcome, reason = RecoveryStatus.FAILED.value, "recovery_audit_claim_expired_without_effect"
            else:
                outcome, reason = RecoveryStatus.UNKNOWN.value, "recovery_verification_overdue"
            db_write_supervision_recovery_actions.update_for_verify_2(
                conn,
                outcome,
                _after(now, RECHECK_SECONDS) if outcome == RecoveryStatus.UNKNOWN.value else None,
                now,
                outcome,
                action["action_id"],
                state,
            )
            _attention(conn, action, reason, now)
            changes.append({"action_id": action["action_id"], "status": outcome})
        else:
            db_write_supervision_recovery_actions.set_next_check_at_by_action_id(
                conn, _after(now, RECHECK_SECONDS), action["action_id"]
            )
    return changes
