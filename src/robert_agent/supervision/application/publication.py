"""Supervision fences around the existing publisher's exact action and task lock."""

from robert_agent.foundation.contracts.delivery import PublicationStatus
from robert_agent.foundation.contracts.supervision import RecoveryActionKind

from robert_agent.persistence import database as db_database

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import publication_intents as db_read_publication_intents
from robert_agent.persistence.queries import supervision_recovery_actions as db_read_supervision_recovery_actions
from robert_agent.persistence.queries import tasks as db_read_tasks
from robert_agent.persistence.queries import workstreams as db_read_workstreams
from robert_agent.workflows import routing as workflow_routing
from robert_agent.work.application import admission as work_admission

from contextlib import closing
import json
from pathlib import Path

from robert_agent.supervision import actions as supervision_actions
from robert_agent.supervision import store as supervision_store


def _connect(db):
    conn = db_database.connect(db)
    return conn


def needs_config(config):
    settings = (config or {}).get("supervision") or {}
    return settings.get("mode") == "recover_known" or bool(settings.get("repair"))


def fresh_config(config, db=None):
    if config and config.get("config_path"):
        from robert_agent.configuration import command as validate_config

        current = validate_config.validate_config(config["config_path"], skip_external=True)
        if not current.get("ok") or (db and Path(current["db_path"]).resolve() != Path(db).resolve()):
            return {}
        return current
    return config or {}


def _repair_allowed(conn, action, config):
    from robert_agent.workflows.maintenance import repair as supervision_repair

    repair = supervision_store.repair_task(conn, action["task_id"])
    if not repair:
        return False, True
    repo = db_records.first(db_read_workstreams.find_for_repair_allowed(conn, action["task_id"]))
    return True, bool(
        repo
        and supervision_repair.authorized_repair(
            conn, action["task_id"], config, repo[0], action_type=action["action_type"]
        )
    )


def begin(db, action, config, now):
    """Run inside the publisher's already-held task publication lock."""
    current = fresh_config(config, db)
    if config and config.get("config_path") and not current:
        return {"status": "blocked", "reason_code": "recovery_policy_unavailable"}
    config = current
    with closing(_connect(db)) as conn, conn:
        repair, allowed = _repair_allowed(conn, action, config)
        if not allowed:
            return {"status": "blocked", "reason_code": "maintenance_policy_rejected"}
        intent = db_records.first(
            db_read_publication_intents.find_status_and_remote_evidence_json_by_action_id(conn, action["action_id"])
        )
        executing = intent and intent["status"] == PublicationStatus.EXECUTING
        evidence = json.loads(intent["remote_evidence_json"] or "{}") if intent else {}
        needs_claim = bool(evidence.get("supervision_retry_required"))
        kind = (
            RecoveryActionKind.PUBLICATION_RECONCILE.value if executing else RecoveryActionKind.PUBLICATION_RETRY.value
        )
        native_retry = bool(
            executing
            or needs_claim
            or (json.loads(action["metadata_json"] or "{}").get("publish") or {}).get("attempt_count")
        )
        if (
            not native_retry
            and supervision_store.has_schema(conn)
            and db_records.first(db_read_supervision_recovery_actions.find_for_begin(conn, action["task_id"]))
        ):
            # The domain has already accepted this first publication. Do not
            # wait for an unrelated recovery's observational bookkeeping.
            return {"status": "legacy", "guard_required": repair, "reconcile_only": False}
        verdict = supervision_actions.begin(
            conn,
            task_id=action["task_id"],
            kind=kind,
            target_id=action["action_id"],
            config=config,
            now=now,
            native_retry=native_retry,
        )
        # A recovered task lock permits reading the same earlier remote effect,
        # even if the writer crashed after it charged its recovery claim.
        if verdict["status"] == "blocked" and executing and supervision_store.has_schema(conn):
            row = db_records.first(
                db_read_supervision_recovery_actions.find_for_begin_2(conn, action["task_id"], action["action_id"])
            )
            if row:
                return {"status": "reconcile_only", "claim": dict(row), "reconcile_only": True}
        if needs_claim and not executing and verdict["status"] != "claimed":
            return {"status": "blocked", "reason_code": verdict.get("reason_code", "recovery_policy_required")}
    return {
        **verdict,
        "guard_required": repair or verdict["status"] == "claimed",
        "reconcile_only": bool(executing and (needs_claim or verdict["status"] == "claimed")),
    }


def guard(db, action, state, config, now):
    """Check current policy and the durable claim just before a remote mutation."""
    config = fresh_config(config, db)
    with closing(_connect(db)) as conn:
        repair, allowed = _repair_allowed(conn, action, config)
        if not allowed or state.get("reconcile_only"):
            return False
        if state.get("claim") and state["status"] == "claimed":
            if not supervision_actions.publication_current(conn, state["claim"], config=config, now=now):
                return False
            if not repair:
                from robert_agent.execution import rounds as native_rounds

                row = db_records.first(db_read_tasks.find_for_guard(conn, action["task_id"]))
                repo = next((value for value in config.get("repos", ()) if row and value["full_name"] == row[0]), None)
                if not repo:
                    return False
                native = native_rounds.session(conn, task_id=action["task_id"])
                return (
                    workflow_routing.native_request_allowed(native, repo, config)
                    if native
                    else work_admission.ordinary_dispatch_allowed(conn, action["task_id"], repo)
                )
    return True


def finish(conn, state, *, outcome, action_id, now, reason=None):
    """Write the claim receipt in the same transaction as the publisher result."""
    claim = state.get("claim")
    if claim:
        receipt = {"action_id": action_id}
        if reason:
            receipt["reason_code"] = reason
        supervision_actions.finish(conn, claim, outcome=outcome, receipt=receipt, now=now)
