"""Validate durable taskless model process and exit evidence without I/O."""

from hashlib import sha256
import json
import re

from robert_agent.foundation.contracts.execution import ModelRunStatus, SourceModelExit
from robert_agent.foundation.timestamps import utc_timestamp


def json_object(encoded):
    try:
        value = json.loads(encoded)
    except (TypeError, ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def frozen_prompt_packet(row):
    """Return the persisted packet only when its bytes match the run binding."""
    packet = json_object(row["input_packet_json"])
    if packet is None or type(packet.get("version")) is not int or packet["version"] not in (1, 2):
        return None
    payload = packet.get("payload")
    if not isinstance(payload, str):
        return None
    envelope = json_object(payload)
    body = envelope.pop("prompt", None) if envelope is not None else None
    binding = {key: row[key] for key in ("round_id", "run_id", "input_version", "prompt_sha256")}
    if not isinstance(body, str) or envelope != binding:
        return None
    try:
        encoded = body.encode("utf-8")
    except UnicodeError:
        return None
    if sha256(encoded).hexdigest() != row["prompt_sha256"]:
        return None
    if packet["version"] == 2:
        context = packet.get("context")
        if not isinstance(context, dict):
            return None
        try:
            digest = context_digest(context)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return None
        stage_block = body.partition("\n## Stage\n\n")[2].partition("\n## ")[0]
        if f"source_context_sha256: {digest}" not in stage_block.splitlines():
            return None
    return packet


def context_digest(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
    return sha256(encoded).hexdigest()


def model_identity_facts(identity):
    """Validate the stored, isolated process group identity shape without OS queries."""
    if not isinstance(identity, dict):
        return None
    pid = identity.get("pid")
    pgid = identity.get("process_group_id")
    start = identity.get("process_start_token")
    fingerprint = identity.get("command_fingerprint")
    source = identity.get("identity_source")
    if (
        type(pid) is not int
        or pid <= 1
        or type(pgid) is not int
        or pgid != pid
        or not isinstance(start, str)
        or not start.strip()
        or not isinstance(fingerprint, str)
        or re.fullmatch("[0-9a-f]{64}", fingerprint) is None
        or source not in ("proc", "ps")
    ):
        return None
    return {
        "pid": pid,
        "process_group_id": pgid,
        "process_start_token": start,
        "command_fingerprint": fingerprint,
        "identity_source": source,
    }


def source_model_blocker(row):
    if (row["launch_id"] is None) != (row["launch_token"] is None):
        return "model_evidence_invalid"
    if row["launch_id"] is not None and (
        not isinstance(row["launch_id"], str)
        or not row["launch_id"].strip()
        or type(row["launch_token"]) is not int
        or row["launch_token"] < 1
    ):
        return "model_evidence_invalid"
    if row["slot_reserved"] or row["status"] in (ModelRunStatus.STARTING, ModelRunStatus.RUNNING):
        raw = row["dispatch_json"]
        if raw is not None and model_identity_facts(json_object(raw)) is None:
            return "model_identity_invalid"
        if row["launch_id"] is not None:
            return "model_exit_unconfirmed"
        return "model_slot_reserved"
    if row["status"] == ModelRunStatus.WAITING:
        return (
            "model_waiting" if row["launch_id"] is None and row["dispatch_json"] is None else "model_evidence_invalid"
        )
    evidence = json_object(row["exit_json"])
    if not evidence or utc_timestamp(evidence.get("observed_at")) is None:
        return "model_exit_unconfirmed"
    kind = evidence.get("kind")
    if (
        kind == SourceModelExit.NOT_LAUNCHED
        and row["launch_id"] is None
        and row["dispatch_json"] is None
        and row["status"] == ModelRunStatus.INTERRUPTED
    ):
        return None
    if not row["launch_id"] or evidence.get("launch_id") != row["launch_id"]:
        return "model_exit_unconfirmed"
    if kind == SourceModelExit.GROUP_EXITED:
        identity = model_identity_facts(json_object(row["dispatch_json"]))
        if identity is not None and identity == model_identity_facts(evidence.get("identity")):
            return None
    elif (
        kind in (SourceModelExit.SPAWN_FAILED, SourceModelExit.STARTUP_GROUP_CLEANED)
        and row["dispatch_json"] is None
        and evidence.get("owner_token") == row["launch_token"]
        and row["status"] in (ModelRunStatus.FAILED, ModelRunStatus.INTERRUPTED)
        and (kind != SourceModelExit.STARTUP_GROUP_CLEANED or model_identity_facts(evidence.get("identity")))
    ):
        return None
    return "model_exit_unconfirmed"
