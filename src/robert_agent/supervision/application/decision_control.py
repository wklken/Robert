"""Local operator replies and read-only decision status."""

from robert_agent.foundation.contracts.supervision import DecisionStatus, DecisionRunStatus

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import json

from robert_agent.persistence import database, transactions
from robert_agent.persistence.queries import supervision_decisions as queries
from robert_agent.supervision import decisions, store
from robert_agent.adapters.processes import identity
from robert_agent.persistence.stores import work_item_events, notifications


def status(config):
    with closing(database.connect(f"{Path(config['db_path']).resolve().as_uri()}?mode=ro", uri=True)) as conn:
        rows = queries.decision_states(conn, [r["full_name"] for r in config.get("repos", ())])
        return [
            {
                "workstream_id": row["scope_id"],
                **{k: v for k, v in json.loads(row["metadata_json"]).items() if k != "bundle"},
            }
            for row in rows
        ]


def reply(config, workstream_id, decision_id, body):
    now = datetime.now(timezone.utc).isoformat()
    with closing(database.connect(f"{Path(config['db_path']).resolve().as_uri()}?mode=rw", uri=True)) as conn, conn:
        transactions.begin(conn, immediate=True)
        if decisions.snapshot(conn, workstream_id, config, now) is None:
            raise ValueError("workstream is not available in this configuration")
        previous = decisions.read(conn, workstream_id)
        decisions.reply(conn, config, workstream_id, decision_id, body, now)
        if previous.get("question_event_id"):
            notifications.resolve_attention(conn, previous["question_event_id"])
            work_item_events.insert_event(
                conn,
                decision_id + "-reply",
                previous["question_work_item_id"],
                "user_response",
                "operator",
                "local-cli",
                body,
                previous["question_event_id"],
                decision_id + "-reply",
                now,
                json.dumps({"kind": "supervisor_reply"}),
            )
    return {
        "ok": True,
        "status": DecisionStatus.REPLY_RECEIVED.value,
        "workstream_id": workstream_id,
        "decision_id": decision_id,
    }


def runner_status(config):
    with closing(database.connect(f"{Path(config['db_path']).resolve().as_uri()}?mode=ro", uri=True)) as conn:
        state = (store.get_checkpoint(conn, *decisions.RUNNER_KEY) or {}).get("metadata", {})
    return state


def resolve_runner(config, decision_id, *, confirm_process_stopped=False):
    """Local operator attests to unknown children; known live identities still fence."""
    if not confirm_process_stopped:
        raise ValueError("Stop the supervisor and its model processes before confirming recovery")
    state = runner_status(config)
    claim = state.get("claim") or {}
    if claim.get("bundle_id") != decision_id:
        raise ValueError("runner claim changed or is no longer waiting")
    # Process inspection is outside the writer. Unknown identity requires the
    # explicit operator attestation; never infer death from an expired deadline.
    for field in ("owner_identity", "process_identity"):
        recorded = claim.get(field)
        if not recorded:
            continue
        if identity.inspect_recorded_identity(recorded) == "alive_same":
            raise ValueError("supervisor or model process is still running")
        if field == "process_identity" and identity.live_process_group(recorded["process_group_id"]):
            raise ValueError("model process group is still running")
    now = datetime.now(timezone.utc).isoformat()
    with closing(database.connect(f"{Path(config['db_path']).resolve().as_uri()}?mode=rw", uri=True)) as conn, conn:
        transactions.begin(conn, immediate=True)
        current = (store.get_checkpoint(conn, *decisions.RUNNER_KEY) or {}).get("metadata", {})
        if current.get("claim") != claim:
            raise ValueError("runner claim changed during inspection")
        resolution = {
            "decision_id": decision_id,
            "resolved_at": now,
            "authority": "local_operator",
            "process_stop_confirmed": True,
        }
        store.update_checkpoint(conn, "decision_resolution", "installation", decision_id, now=now, metadata=resolution)
        store.update_checkpoint(
            conn,
            *decisions.RUNNER_KEY,
            now=now,
            metadata={
                **current,
                "claim": None,
                "last_status": DecisionRunStatus.OPERATOR_RESOLVED.value,
                "next_check_at": now,
                "resolution": resolution,
            },
        )
    return {"ok": True, "status": "resolved", "decision_id": decision_id}
