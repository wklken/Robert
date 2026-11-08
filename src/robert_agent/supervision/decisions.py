"""Durable model decisions; consumers retain all domain authorization and effects.

Candidates and decisions use named supervision checkpoints. Every mutation uses
the caller's writer transaction; model output never acts as a user credential.
"""

from robert_agent.foundation.contracts.supervision import (
    AUTOMATIC_DECISION_ACTIONS,
    NONEXECUTING_DECISION_ACTIONS,
    DecisionAction,
    DecisionStatus,
    SupervisionMode,
)
from robert_agent.foundation.contracts.work import TaskLifecycle, WorkstreamLifecycle

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from importlib.resources import files
import json
import re
from uuid import uuid4

from robert_agent.persistence.queries import supervision_decisions as queries
from robert_agent.persistence.stores import notifications
from robert_agent.security import redaction
from robert_agent.supervision import store
from robert_agent.supervision.rules import timestamp
from robert_agent.supervision.policy import current_config
from robert_agent.work.questions import record_supervisor_question


RUNNER_KEY = ("decision_runner", "installation", "current")


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value):
    return sha256(encode(value).encode()).hexdigest()


def enabled(config):
    return (config.get("supervision") or {}).get("mode") == SupervisionMode.DECIDE.value


def policy_version(config):
    return digest(
        {"supervision": config.get("supervision"), "daemon": config.get("daemon"), "repos": config.get("repos", [])}
    )


def read(conn, root_id):
    return (store.get_checkpoint(conn, "decision", root_id, "current") or {}).get("metadata", {})


def write(conn, root_id, value, now):
    if not conn.in_transaction:
        raise ValueError("decision mutation requires a writer transaction")
    store.update_checkpoint(conn, "decision", root_id, "current", now=now, metadata=value)


def _safe(value, depth=0):
    if depth > 20:
        raise ValueError("decision evidence nesting exceeds bound")
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            try:
                return encode(_safe(json.loads(value), depth + 1))
            except (ValueError, RecursionError):
                # Truncated or malformed structured evidence is never sent raw.
                return "[structured evidence omitted]"
        safe = redaction.redact_text(value)
        if not safe["ok"]:
            return "[sensitive evidence omitted]"
        safe["text"].encode("utf-8")
        return safe["text"]
    if isinstance(value, dict):
        return {
            key: _safe(child, depth + 1)
            for key, child in value.items()
            if not re.search(
                r"(?:^|_)(?:password|passwd|secret|credentials?|api_key|token|authorization|cookie)(?:$|_)", key, re.I
            )
        }
    if isinstance(value, (list, tuple)):
        return [_safe(child, depth + 1) for child in value]
    return value


def offer(conn, config, *, workstream_id, action, target_id, facts, now):
    """Publish a bounded action candidate. This grants no authority to run it."""
    if not enabled(config):
        return None
    if action not in AUTOMATIC_DECISION_ACTIONS or not isinstance(target_id, str) or not target_id:
        raise ValueError("unsupported decision candidate")
    if not conn.in_transaction:
        raise ValueError("candidate observation requires a writer transaction")
    root = queries.root_stream(conn, workstream_id)
    if not root or root["lifecycle"] == WorkstreamLifecycle.CANCELED:
        return None
    key = digest([workstream_id, action, target_id])
    safe = _safe(facts)
    if len(encode(safe).encode()) > 12000:
        return None
    value = {
        "id": key,
        "workstream_id": workstream_id,
        "action": action,
        "target_id": target_id,
        "generation": digest(facts),
        "facts": safe,
        "observed_at": now,
        "policy_version": policy_version(config),
    }
    store.update_checkpoint(conn, "decision_candidate", root["workstream_id"], key, now=now, metadata=value)
    return value


def withdraw_repairs(conn, config, workstream_id, now):
    """A fresh PR scan replaces its previous repair offers in the same writer."""
    if not enabled(config):
        return
    if not conn.in_transaction:
        raise ValueError("candidate withdrawal requires a writer transaction")
    root = queries.root_stream(conn, workstream_id)
    if not root:
        return
    for row in queries.checkpoints(conn, "decision_candidate", root["workstream_id"]):
        candidate = json.loads(row["metadata_json"])
        if candidate.get("workstream_id") == workstream_id and candidate.get("action") in {
            DecisionAction.REPAIR_CI.value,
            DecisionAction.RESOLVE_CONFLICT.value,
        }:
            store.update_checkpoint(
                conn,
                "decision_candidate",
                root["workstream_id"],
                candidate["id"],
                now=now,
                metadata={**candidate, "withdrawn": True},
            )


def snapshot(conn, root_id, config, now):
    context = queries.context(conn, root_id)
    if (
        not context
        or not context["repository"]["enabled"]
        or context["streams"][0]["lifecycle"] == WorkstreamLifecycle.CANCELED
    ):
        return None
    repo = next((r for r in config.get("repos", ()) if r["full_name"] == context["repository"]["full_name"]), None)
    if not repo or not repo.get("enabled", True):
        return None
    candidates = []
    for row in queries.checkpoints(conn, "decision_candidate", root_id, now=now, policy_version=policy_version(config)):
        item = json.loads(row["metadata_json"])
        observed = timestamp(item.get("observed_at"))
        if (
            observed
            and not item.get("withdrawn")
            and observed <= timestamp(now) <= observed + timedelta(seconds=900)
            and item.get("policy_version") == policy_version(config)
        ):
            candidates.append({k: v for k, v in item.items() if k != "observed_at"})
    previous = read(conn, root_id)
    # Heartbeats and sampling timestamps are intentionally absent from context.
    return {
        "root_id": root_id,
        "context": context,
        "candidates": candidates,
        "reply": previous.get("reply"),
        "policy_version": policy_version(config),
    }


def decision_history(previous):
    history = list(previous.get("history", []))[-7:]
    if previous.get("decision"):
        history.append(
            {
                "decision": previous["decision"],
                "status": previous.get("status"),
                "receipt": previous.get("receipt"),
                "decided_at": previous.get("decided_at"),
            }
        )
    return history


def _brief(value, chars=512):
    if isinstance(value, str):
        return value if len(value) <= chars else value[:chars] + " [truncated]"
    if isinstance(value, list):
        return [_brief(v, chars) for v in value[:8]] + ([{"omitted_rows": len(value) - 8}] if len(value) > 8 else [])
    if isinstance(value, dict):
        return {k: _brief(v, chars) for k, v in value.items()}
    return value


def packet(snapshot_value, previous):
    evidence = [{"id": key, "data": value} for key, value in snapshot_value["context"].items()]
    evidence.append({"id": "decision_history", "data": decision_history(previous)})
    evidence.append({"id": "user_reply", "data": snapshot_value.get("reply")})
    value = {
        "bundle_id": "decision-" + uuid4().hex,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "root_id": snapshot_value["root_id"],
        "input_generation": digest(snapshot_value),
        "evidence": evidence,
        "allowed_actions": sorted(
            NONEXECUTING_DECISION_ACTIONS
            if can_finish(snapshot_value)
            else NONEXECUTING_DECISION_ACTIONS - {DecisionAction.FINISH_SUPERVISION.value}
        ),
        "candidates": snapshot_value["candidates"][:16],
        "instructions": files("robert_agent.resources").joinpath("supervision-decision.md").read_text(),
    }
    value = _safe(value)
    # Preserve routing IDs and fingerprints; summarize only descriptive evidence.
    for chars in (512, 160, 48):
        if len(encode(value).encode()) <= 48000:
            break
        value["evidence"] = [{**e, "data": _brief(e["data"], chars)} for e in value["evidence"]]
        value["candidates"] = [{**c, "facts": _brief(c["facts"], chars)} for c in value["candidates"]]
    if len(encode(value).encode()) > 48000:
        raise ValueError("decision identifiers exceed byte limit")
    return {**value, "bundle_sha256": digest(value)}


def can_finish(snapshot_value):
    context = snapshot_value["context"]
    if snapshot_value["candidates"] or any(
        t["lifecycle"] not in {TaskLifecycle.COMPLETED, TaskLifecycle.CANCELED, TaskLifecycle.IGNORED}
        for t in context["tasks"]
    ):
        return False
    proof = context["completion"]
    return bool(proof["has_goal"] and not proof["unfinished_goal"] and not proof["unfinished_task"])


def validate(raw, request):
    def unique(pairs):
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError("duplicate decision field")
        return value

    result = json.loads(raw, object_pairs_hook=unique)
    if isinstance(result, dict) and set(result) == {"bundle_id", "bundle_sha256", "error"}:
        error = result["error"]
        if (
            result["bundle_id"] != request["bundle_id"]
            or result["bundle_sha256"] != request["bundle_sha256"]
            or not isinstance(error, dict)
            or set(error) - {"code", "retry_after_seconds"}
            or error.get("code") != "rate_limited"
            or type(error.get("retry_after_seconds", 0)) is not int
            or not 0 <= error.get("retry_after_seconds", 0) <= 86400
        ):
            raise ValueError("invalid model transport outcome")
        return result
    fields = {
        "bundle_id",
        "bundle_sha256",
        "action",
        "candidate_id",
        "reason",
        "evidence_ids",
        "expected_progress",
        "recheck_seconds",
        "message",
    }
    if not isinstance(result, dict) or set(result) != fields:
        raise ValueError("invalid decision fields")
    if result["bundle_id"] != request["bundle_id"] or result["bundle_sha256"] != request["bundle_sha256"]:
        raise ValueError("decision does not match evidence")
    action = result["action"]
    if not isinstance(action, str) or action not in AUTOMATIC_DECISION_ACTIONS | NONEXECUTING_DECISION_ACTIONS:
        raise ValueError("unknown decision action")
    if action in AUTOMATIC_DECISION_ACTIONS:
        candidate = next(
            (c for c in request["candidates"] if c["id"] == result["candidate_id"] and c["action"] == action), None
        )
        if not candidate:
            raise ValueError("decision selected an unavailable action")
    elif result["candidate_id"] is not None:
        raise ValueError("non-execution decision cannot target a candidate")
    elif action not in request["allowed_actions"]:
        raise ValueError("goal completion is not supported by current evidence")
    ids = result["evidence_ids"]
    allowed = {item["id"] for item in request["evidence"]}
    if not isinstance(ids, list) or not ids or any(not isinstance(i, str) or i not in allowed for i in ids):
        raise ValueError("unknown decision evidence")
    if type(result["recheck_seconds"]) is not int or not 30 <= result["recheck_seconds"] <= 3600:
        raise ValueError("decision recheck must be between 30 and 3600 seconds")
    for key in ("reason", "expected_progress", "message"):
        if not isinstance(result[key], str) or len(result[key]) > 2000:
            raise ValueError("invalid decision text")
    if not result["reason"].strip() or not result["expected_progress"].strip():
        raise ValueError("decision needs a reason and expected progress")
    if action in {DecisionAction.ASK_USER.value, DecisionAction.NOTIFY_USER.value} and not result["message"].strip():
        raise ValueError("user interaction needs a message")
    return _safe(result)


def approve(conn, config, request, output, now):
    config = current_config(conn, config)
    if config is None:
        return False
    root_id = request["root_id"]
    current = snapshot(conn, root_id, config, now)
    if not enabled(config) or not current or digest(current) != request["input_generation"]:
        return False
    if output["action"] == DecisionAction.FINISH_SUPERVISION.value and not can_finish(current):
        return False
    previous = read(conn, root_id)
    history = decision_history(previous)
    action = output["action"]
    state = {
        "decision": output,
        "decided_at": now,
        "status": DecisionStatus.APPROVED.value if action in AUTOMATIC_DECISION_ACTIONS else action,
        "input_generation": request["input_generation"],
        "policy_version": current["policy_version"],
        "candidate": next((c for c in request["candidates"] if c["id"] == output["candidate_id"]), None),
        "next_check_at": (timestamp(now) + timedelta(seconds=output["recheck_seconds"])).isoformat(),
        "history": history,
        "expires_at": (timestamp(now) + timedelta(seconds=900)).isoformat(),
        "reply": previous.get("reply"),
        "bundle": request,
    }
    if action in {DecisionAction.ASK_USER.value, DecisionAction.NOTIFY_USER.value}:
        notification_id = request["bundle_id"]
        tasks = current["context"]["tasks"]
        unchanged_notice = (
            previous.get("notification_id")
            and previous.get("input_generation") == request["input_generation"]
            and (previous.get("decision") or {}).get("action") == action
        )
        if unchanged_notice:
            state["notification_id"] = previous["notification_id"]
            write(conn, root_id, state, now)
            return True
        notifications.insert_notification(
            conn,
            notification_id,
            tasks[0]["task_id"] if tasks else None,
            "supervision_question" if action == DecisionAction.ASK_USER.value else "supervision_notice",
            now,
            encode(
                {
                    "reason_code": action,
                    "required_action": output["message"],
                    "retryable": False,
                    "workstream_id": root_id,
                    "repo_id": current["context"]["repository"]["repo_id"],
                    "repository_scoped": True,
                    "decision_id": request["bundle_id"],
                }
            ),
        )
        state["notification_id"] = notification_id
        goals = current["context"]["goals"]
        if (
            action == DecisionAction.ASK_USER.value
            and goals
            and not goals[0]["canceled_at"]
            and not goals[0]["completed_at"]
        ):
            state["question_work_item_id"] = goals[0]["work_item_id"]
            state["question_event_id"] = record_supervisor_question(
                conn,
                work_item_id=goals[0]["work_item_id"],
                root_id=root_id,
                decision_id=request["bundle_id"],
                body=output["message"],
                now=now,
            )
    write(conn, root_id, state, now)
    store.update_checkpoint(conn, "decision_record", root_id, request["bundle_id"], now=now, metadata=state)
    return True


def permit(conn, config, *, workstream_id, action, target_id, facts, now):
    """Check a current model grant; domain consumers still fence/charge effects."""
    was_enabled = enabled(config)
    config = current_config(conn, config)
    if config is None:
        return False
    if not enabled(config):
        return not was_enabled
    if not config.get("daemon", {}).get("enabled", True):
        return False
    candidate = offer(
        conn, config, workstream_id=workstream_id, action=action, target_id=target_id, facts=facts, now=now
    )
    root = queries.root_stream(conn, workstream_id)
    if not candidate or not root:
        return False
    state = read(conn, root["workstream_id"])
    selected = state.get("candidate") or {}
    expiry = timestamp(state.get("expires_at"))
    if (
        state.get("status") != DecisionStatus.APPROVED.value
        or not expiry
        or timestamp(now) >= expiry
        or state.get("policy_version") != policy_version(config)
        or selected.get("id") != candidate["id"]
        or selected.get("generation") != candidate["generation"]
    ):
        return False
    return state.get("input_generation") == digest(snapshot(conn, root["workstream_id"], config, now))


def executed(conn, config, workstream_id, receipt, now):
    if not enabled(config):
        return
    root = queries.root_stream(conn, workstream_id)
    state = read(conn, root["workstream_id"]) if root else {}
    if state.get("status") == DecisionStatus.APPROVED.value:
        state = {**state, "status": DecisionStatus.VERIFYING.value, "receipt": _safe(receipt)}
        write(conn, root["workstream_id"], state, now)
        store.update_checkpoint(
            conn, "decision_record", root["workstream_id"], state["decision"]["bundle_id"], now=now, metadata=state
        )


def reply(conn, config, root_id, decision_id, body, now):
    state = read(conn, root_id)
    if state.get("status") != DecisionStatus.ASK_USER or (state.get("decision") or {}).get("bundle_id") != decision_id:
        raise ValueError("supervisor question changed or is no longer waiting")
    if not isinstance(body, str) or not body.strip() or len(body) > 8000:
        raise ValueError("reply must contain 1 to 8000 characters")
    write(
        conn,
        root_id,
        {**state, "status": DecisionStatus.REPLY_RECEIVED.value, "reply": _safe(body), "next_check_at": now},
        now,
    )


def has_ready_worker_retry(conn, config, repo_ids, now):
    """Observe a durable current grant without offering, claiming, or executing it."""
    if not enabled(config) or not config.get("daemon", {}).get("enabled", True):
        return False
    for row in queries.approved_worker_retries(conn, repo_ids, now, policy_version(config)):
        state = json.loads(row["metadata_json"])
        current = snapshot(conn, row["scope_id"], config, now)
        if current and state.get("input_generation") == digest(current):
            return True
    return False
