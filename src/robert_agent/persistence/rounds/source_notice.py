"""Identify the persisted fixed conflict notice and its local retirement proof."""

from hashlib import sha256
import json
import re

from robert_agent.foundation.contracts.delivery import AuditStatus, PublicationStatus, PublishStatus, SourceResultKind
from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.persistence.rounds.source_evidence import context_digest, json_object

REASON = "repository_conflict"
BODY = "此单据匹配到多个仓库规则。请确认目标仓库后再继续。"
KEY_PREFIX = "tapd-notice:"
RETIREMENT = "fixed_notice_retirement"
LOCAL_DISPOSITION = "local_never_started"


def identity(source_id, repositories, generation):
    signature = sha256(json.dumps([REASON, repositories]).encode()).hexdigest()
    return _key(source_id, signature, generation), _evidence(signature, generation)


def action_id(result_id):
    return "action-" + sha256(result_id.encode()).hexdigest()


def _key(source_id, signature, generation):
    legacy = f"{KEY_PREFIX}{source_id}:{signature}"
    return f"{legacy}:{generation}" if generation else legacy


def _evidence(signature, generation):
    evidence = {"reason": REASON, "episode": signature}
    if generation:
        evidence["generation"] = generation
    return evidence


def exact_candidate(root, action, source_id, target):
    """Check the complete stored shape rather than a notice body alone."""
    if (
        root["source_id"] != source_id
        or root["kind"] != SourceResultKind.CONTROL_NOTICE
        or any(root[name] is not None for name in ("round_id", "worker_result_id", "model_run_id"))
        or json_object(root["payload_json"]) != {"reason": REASON}
    ):
        return False
    evidence = json_object(root["evidence_json"]) or {}
    signature = evidence.get("episode")
    generation = evidence.get("generation", 0)
    if (
        not isinstance(signature, str)
        or re.fullmatch(r"[0-9a-f]{64}", signature) is None
        or type(generation) is not int
        or generation < 0
        or evidence != _evidence(signature, generation)
    ):
        return False
    key = _key(source_id, signature, generation)
    result_id = "source-result:" + sha256(key.encode()).hexdigest()
    metadata = json_object(action["metadata_json"]) or {}
    return (
        root["idempotency_key"] == key
        and root["source_result_id"] == result_id
        and action["source_result_id"] == result_id
        and action["action_id"] == action_id(result_id)
        and action["position"] == 0
        and action["task_id"] is None
        and action["result_id"] is None
        and action["audit_status"] == AuditStatus.ACCEPTED
        and action["provider"] == "tapd"
        and action["action_type"] == "comment"
        and json_object(action["target_json"]) == target
        and {key: value for key, value in metadata.items() if key not in ("publish", RETIREMENT)}
        == {"target_id": "ticket", "body": BODY}
    )


def _unwritten(action):
    return action["external_id"] is None and action["target_url"] is None and action["intent_started_at"] is None


def _no_remote_proof(action):
    return json_object(action["remote_evidence_json"]) == {}


def _prepared_intact(action):
    packet = action["prepared_evidence_json"]
    if packet is None:
        return True
    saved = json_object(packet)
    try:
        return (
            saved is not None
            and saved.get("action_id") == action["action_id"]
            and saved.get("target") == json_object(action["target_json"])
            and context_digest(saved) == action["intent_fingerprint"]
        )
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return False


def never_started(action):
    return (
        action["publish_status"] == PublishStatus.NOT_PUBLISHED
        and RETIREMENT not in (json_object(action["metadata_json"]) or {})
        and _unwritten(action)
        and action["intent_finished_at"] is None
        and action["intent_error_json"] is None
        and (
            action["publication_id"] is None
            or (
                action["intent_status"] == PublicationStatus.PREPARED
                and _no_remote_proof(action)
                and _prepared_intact(action)
            )
        )
    )


def retirement_proof(action, revision, now):
    return {
        "disposition": LOCAL_DISPOSITION,
        "source_result_id": action["source_result_id"],
        "action_id": action["action_id"],
        "publication_id": action["publication_id"],
        "observation_revision": revision,
        "recorded_at": now,
    }


def retired(action):
    metadata = json_object(action["metadata_json"]) or {}
    proof = metadata.get(RETIREMENT)
    if not isinstance(proof, dict):
        return False
    revision, now = proof.get("observation_revision"), proof.get("recorded_at")
    return (
        isinstance(revision, str)
        and re.fullmatch(r"[0-9a-f]{64}", revision) is not None
        and utc_timestamp(now) is not None
        and proof == retirement_proof(action, revision, now)
        and action["publish_status"] == PublishStatus.SKIPPED
        and _unwritten(action)
        and (
            (
                action["publication_id"] is None
                and action["intent_finished_at"] is None
                and action["intent_error_json"] is None
                and action["remote_evidence_json"] is None
            )
            or (
                action["intent_status"] == PublicationStatus.FAILED
                and action["intent_finished_at"] == now
                and json_object(action["intent_error_json"]) == proof
                and _no_remote_proof(action)
                and _prepared_intact(action)
            )
        )
    )
