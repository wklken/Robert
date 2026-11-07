"""application supervision checks responsibilities."""

from robert_agent.persistence import errors as db_errors
from robert_agent.persistence import transactions as db_transactions

from robert_agent.persistence.stores import notifications as db_write_notifications
from contextlib import closing
import hashlib
import json
import subprocess
import sys
from robert_agent.security import redaction
from robert_agent.adapters.notifications import messaging as message_transport
from robert_agent.supervision import actions as supervision_actions
from robert_agent.supervision.application import observation as supervision_observe
from robert_agent.supervision import store as supervision_store
from robert_agent.configuration import supervision as supervision_config

from robert_agent.persistence import supervision as boundary_adapters_sqlite_supervision
from robert_agent.supervision import health as supervision_health
from robert_agent.supervision import decisions
from robert_agent.supervision.application import decision_control


def schema_ready(conn):
    if not supervision_store.has_schema(conn):
        raise ValueError("supervision_schema_missing")


def safe_observation(observation):
    result = dict(observation)
    for field in ("evidence", "positive_evidence"):
        if field in result:
            safe = redaction.redact_text(json.dumps(result[field], ensure_ascii=False))
            result[field] = json.loads(safe["text"]) if safe["ok"] else {"details_omitted": True}
            if field == "positive_evidence" and not safe["ok"]:
                result.pop(field)
    return result


def notify_changes(conn, changes, now):
    for change in changes:
        if change["transition"] != "opened":
            continue
        item = change["incident"]
        identifier = "supervision-" + item["incident_id"] + "-opened"
        scope = {}
        if item["scope_type"] in {"github_event", "work_item_event"}:
            checkpoint = supervision_store.get_checkpoint(conn, item["scope_type"], item["scope_id"], "admission")
            # Missing repository evidence must never turn a scoped incident into a global notice.
            scope = {"repository_scoped": True, "repo_id": (checkpoint or {}).get("metadata", {}).get("repo_id")}
        db_write_notifications.insert_for_notify_changes(
            conn,
            identifier,
            item["scope_id"] if item["scope_type"] == "task" else None,
            now,
            json.dumps(
                {
                    "reason_code": item.get("reason_code", item["rule_id"]),
                    "required_action": "Inspect the supervision incident and its evidence.",
                    "incident_id": item["incident_id"],
                    "retryable": False,
                    **scope,
                }
            ),
        )


def check(config, *, record=False, now=None):
    now = now or supervision_health.utc_now()
    settings = {**supervision_config.DEFAULTS, **config.get("supervision", {})}
    try:
        with closing(boundary_adapters_sqlite_supervision.connect(config["db_path"], readonly=True)) as conn:
            schema_ready(conn)
            state = supervision_store.get_checkpoint(conn, "service", "supervisor", "cursor") if record else None
        cursor = (state or {}).get("metadata", {}).get("cursor", "")
        sampled = supervision_observe.collect(
            config["db_path"], now=now, limit=settings["batch_size"], config=config, cursor=cursor
        )
        observations = [safe_observation(item) for item in sampled["observations"]]
        changes = []
        recovery = []
        if record:
            with closing(boundary_adapters_sqlite_supervision.connect(config["db_path"])) as conn, conn:
                db_transactions.begin(conn, immediate=True)
                observations = [item for item in observations if supervision_observe.is_current(conn, item)]
                changes = supervision_store.apply_observations(conn, observations, now)
                supervision_store.update_checkpoint(
                    conn, "service", "supervisor", "cursor", now=now, metadata={"cursor": sampled.get("cursor", "")}
                )
                supervision_actions.verify(conn, observations=observations, now=now)
                recovery = supervision_actions.plan(conn, observations=observations, now=now, config=config)
                queued_incidents = {item.get("incident_id") for item in recovery}
                notify_changes(
                    conn,
                    [
                        change
                        for change in changes
                        if change["incident"]["incident_id"] not in queued_incidents
                        and not (decisions.enabled(config) and change["incident"]["scope_type"] == "task")
                    ],
                    now,
                )
        result = {
            "ok": True,
            "status": "observed",
            "checked_at": now,
            "observations": observations,
            "changes": changes,
            "recovery_requests": len(recovery),
            "coverage_complete": sampled.get("coverage_complete", False),
        }
    except (OSError, db_errors.Error, ValueError) as exc:
        result = {
            "ok": False,
            "status": "unobservable",
            "checked_at": now,
            "error_class": type(exc).__name__,
            "safe_error": "Observation unavailable; inspect the database and supervision schema.",
        }
    if record:
        try:
            supervision_health.health(config, result, now)
        except OSError:
            result["health_persisted"] = False
            print("Robert supervision: observation health cannot be persisted", file=sys.stderr)
    return result


def status(config):
    try:
        with closing(boundary_adapters_sqlite_supervision.connect(config["db_path"], readonly=True)) as conn:
            schema_ready(conn)
            incidents = supervision_store.list_incidents(conn, status="active")
            for incident in incidents:
                analysis = supervision_store.get_checkpoint(conn, "diagnosis", incident["incident_id"], "analysis")
                incident["diagnosis"] = (analysis or {}).get("metadata")
                from robert_agent.workflows.maintenance import repair as supervision_repair

                incident["repair"] = supervision_repair.status(config, incident["incident_id"])
        return {
            "ok": True,
            "status": "ready",
            "health": supervision_health.read_health(config),
            "incidents": incidents,
            "decisions": decision_control.status(config),
            "decision_runner": decision_control.runner_status(config),
        }
    except (OSError, db_errors.Error, ValueError):
        return {
            "ok": False,
            "status": "unobservable",
            "health": supervision_health.read_health(config),
            "incidents": [],
        }


def deliver_health(db_path, path, *, runner=subprocess.run, config=None):
    """Deliver the one bounded DB-outage notice independently of SQLite."""
    if not message_transport.configured(config):
        return
    state_path = path.with_name("health-delivery.json")
    try:
        health = json.loads(path.read_text())
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
    except (OSError, ValueError):
        return
    if health.get("ok"):
        if state.get("notification_id"):
            try:
                with closing(boundary_adapters_sqlite_supervision.connect(db_path)) as conn, conn:
                    db_write_notifications.insert_for_deliver_health(
                        conn,
                        state["notification_id"],
                        "sent" if state.get("status") == "sent" else "resolved",
                        state["created_at"],
                        json.dumps({"delivery": state, "reason_code": "observation_unavailable"}),
                    )
            except (OSError, db_errors.Error):
                return
        return
    from robert_agent.supervision.rules import timestamp

    checked = timestamp(health.get("checked_at"))
    eligible = timestamp(health.get("notification_eligible_at"))
    # A stale single failure is not an outage: require a later failed observation.
    if health.get("consecutive_failures", 0) < 2 or not checked or not eligible or checked < eligible:
        return
    identifier = health.get("delivery_id")
    if not identifier:
        return
    fields = {
        "reason_code": "observation_unavailable",
        "required_action": "Inspect the Robert database and supervisor health.",
        "retryable": False,
    }
    digest = hashlib.sha256(json.dumps(["supervisor_unobservable", fields], sort_keys=True).encode()).hexdigest()[:16]
    delivery_id = identifier + ":" + digest
    if state.get("delivery_id") != delivery_id:
        state = {
            "notification_id": identifier,
            "delivery_id": delivery_id,
            "created_at": health["first_failed_at"],
            "attempt_count": 0,
        }
    if state.get("status") == "sent" or state["attempt_count"] >= 3:
        return
    state.update(status="sending", attempt_count=state["attempt_count"] + 1)
    # Without a durable claim we must not repeatedly send on every failed tick.
    try:
        supervision_health.atomic_json(state_path, state)
    except OSError:
        return
    payload = {"type": "supervisor_unobservable", "delivery_id": delivery_id, **fields}
    sent = message_transport.send(payload, config=config, runner=runner)
    state.update(status="sent" if sent else "failed")
    try:
        supervision_health.atomic_json(state_path, state)
    except OSError:
        pass
