"""Read original native GitHub action identity, prepared request and canonical receipt."""

from dataclasses import dataclass
from hashlib import sha256
import re

from sqlalchemy import select

from robert_agent.foundation.contracts.delivery import AuditStatus, GitHubActionType, PublicationStatus, PublishStatus
from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.persistence import records, statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.rounds.source_evidence import context_digest, json_object


def required_action(conn, source_result_id, action_id):
    link, action, root, intent, prepared, result, member, repo = (
        tables[name]
        for name in (
            "source_required_actions",
            "github_actions",
            "source_results",
            "publication_intents",
            "github_publication_preparations",
            "worker_results",
            "source_round_tasks",
            "repos",
        )
    )
    return records.first(
        statements.read(
            conn,
            select(
                action,
                root.c.source_result_id.label("root_id"),
                root.c.source_id,
                root.c.round_id,
                root.c.accepted_at,
                root.c.evidence_json.label("root_evidence_json"),
                link.c.position,
                result.c.attempt_id,
                member.c.repo_id,
                repo.c.full_name.label("repository"),
                intent.c.publication_id,
                intent.c.status.label("intent_status"),
                intent.c.request_fingerprint,
                intent.c.started_at.label("intent_started_at"),
                intent.c.finished_at.label("intent_finished_at"),
                intent.c.remote_evidence_json,
                prepared.c.prepared_evidence_json,
            )
            .select_from(
                link.join(root, root.c.source_result_id == link.c.source_result_id)
                .join(action, action.c.action_id == link.c.action_id)
                .join(result, result.c.result_id == root.c.worker_result_id)
                .join(member, member.c.task_id == result.c.task_id)
                .join(repo, repo.c.repo_id == member.c.repo_id)
                .outerjoin(intent, intent.c.action_id == action.c.action_id)
                .outerjoin(prepared, prepared.c.publication_id == intent.c.publication_id)
            )
            .where(
                link.c.source_result_id == source_result_id,
                link.c.action_id == action_id,
                action.c.provider == "github",
                action.c.source_result_id.is_(None),
                action.c.result_id == result.c.result_id,
                action.c.task_id == result.c.task_id,
                member.c.round_id == root.c.round_id,
                action.c.audit_status == AuditStatus.ACCEPTED.value,
                action.c.action_type.in_((GitHubActionType.PUSH_EXISTING_PR.value, GitHubActionType.OPEN_PR.value)),
            ),
        )
    )


def request_identity(row):
    evidence = json_object(row["root_evidence_json"]) or {}
    target = json_object(row["target_json"])
    if (
        target is None
        or target.get("host") != "github.com"
        or target.get("provider") != "github"
        or target.get("repo_id") != row["repo_id"]
        or target.get("repository") != row["repository"]
        or evidence.get("task_id") != row["task_id"]
        or evidence.get("attempt_id") != row["attempt_id"]
        or evidence.get("repo_id") != row["repo_id"]
    ):
        return None
    return {
        **{
            field: row[field]
            for field in (
                "root_id",
                "source_id",
                "round_id",
                "task_id",
                "attempt_id",
                "result_id",
                "action_id",
                "action_type",
                "repo_id",
                "repository",
                "position",
            )
        },
        "submission_fingerprint": evidence.get("submission_fingerprint"),
        "target": target,
    }


def prepared_request(row):
    """Return only the fingerprinted, original source/action-owned request."""
    packet = json_object(row["prepared_evidence_json"])
    identity = request_identity(row)
    if (
        not packet
        or not identity
        or type(packet.get("version")) is not int
        or packet["version"] != 1
        or packet.get("identity") != identity
        or not isinstance(packet.get("effect"), dict)
        or not _matches(packet.get("publisher"), r"[a-z0-9][a-z0-9-]{0,38}")
        or not _matches(packet.get("audit_scope_sha256"), r"[0-9a-f]{64}")
        or not _matches(identity["submission_fingerprint"], r"[0-9a-f]{64}")
    ):
        return None
    try:
        return packet if _valid_effect(packet) and context_digest(packet) == row["request_fingerprint"] else None
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return None


def _matches(value, pattern):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def _valid_effect(packet):
    identity, effect = packet["identity"], packet["effect"]
    target = identity["target"]
    if (
        effect.get("repo") != identity["repository"]
        or effect.get("remote") != "origin"
        or not _matches(effect.get("head_repository"), r"[a-z0-9][a-z0-9_.-]*/[a-z0-9_.-]+")
        or not all(_matches(effect.get(field), r"[0-9a-f]{40}") for field in ("head_sha", "base_sha"))
        or not all(_matches(effect.get(field), r"[A-Za-z0-9][A-Za-z0-9._/-]{0,180}") for field in ("branch", "base"))
        or effect.get("head") != effect.get("branch")
    ):
        return False
    if identity["action_type"] == GitHubActionType.OPEN_PR:
        body, marker = effect.get("body"), packet.get("marker")
        return (
            target.get("kind") == "repository"
            and effect.get("pr_number") is None
            and isinstance(effect.get("title"), str)
            and bool(effect["title"])
            and isinstance(body, str)
            and isinstance(marker, str)
            and marker == f"<!-- robert-workstream effect={context_digest(identity)[:32]} -->"
            and body.endswith(marker)
            and body.count(marker) == 1
            and sha256(body.encode()).hexdigest() == packet.get("body_sha256")
        )
    if target.get("kind") == "repository":
        return effect.get("pr_number") is None and not effect.get("expected_remote_sha")
    return (
        target.get("kind") == "pull_request"
        and type(effect.get("pr_number")) is int
        and effect["pr_number"] > 0
        and str(effect["pr_number"]) == target.get("external_id")
        and _matches(effect.get("expected_remote_sha"), r"[0-9a-f]{40}")
    )


@dataclass(frozen=True, slots=True)
class ConfirmedGitHubEffect:
    action_id: str
    publication_id: str
    request_fingerprint: str
    external_id: str
    url: str | None
    observed_at: str


def canonical_remote_proof(packet, proof):
    """Check the normalized independent remote facts against the saved request."""
    if not isinstance(proof, dict):
        return False
    identity, effect = packet["identity"], packet["effect"]
    branch = {
        "repository": effect["head_repository"],
        "ref": f"refs/heads/{effect['branch']}",
        "sha": effect["head_sha"],
    }
    if identity["action_type"] == GitHubActionType.PUSH_EXISTING_PR and not effect.get("pr_number"):
        return proof == {"branch": branch}
    pr = proof.get("pr")
    if not isinstance(pr, dict):
        return False
    number = pr.get("number")
    if type(number) is not int or number <= 0:
        return False
    expected_pr = {
        "number": number,
        "url": f"https://github.com/{identity['repository']}/pull/{number}",
        "author": packet["publisher"],
        "head_repository": effect["head_repository"],
        "head_branch": effect["branch"],
        "head_sha": effect["head_sha"],
        "base_repository": identity["repository"].casefold(),
        "base_branch": effect["base"],
        "state": pr.get("state"),
    }
    if pr.get("state") not in ("open", "closed", "merged"):
        return False
    if identity["action_type"] == GitHubActionType.OPEN_PR:
        expected_pr.update(
            title=effect["title"],
            body_sha256=packet["body_sha256"],
            marker=packet["marker"],
        )
        return proof == {"pr": expected_pr}
    return number == effect["pr_number"] and proof == {"branch": branch, "pr": expected_pr}


def _confirmed_details(row):
    if (
        row is None
        or row["intent_status"] != PublicationStatus.PUBLISHED
        or row["publish_status"] != PublishStatus.PUBLISHED
    ):
        return None
    packet = prepared_request(row)
    remote = json_object(row["remote_evidence_json"]) or {}
    receipt = remote.get("receipt")
    if packet is None or remote.get("status") != "confirmed_effect" or not isinstance(receipt, dict):
        return None
    accepted, started, observed, finished = map(
        utc_timestamp,
        (
            row["accepted_at"],
            row["intent_started_at"],
            receipt.get("observed_at"),
            row["intent_finished_at"],
        ),
    )
    proof = receipt.get("proof")
    try:
        matches = canonical_remote_proof(packet, proof)
    except (KeyError, TypeError, ValueError):
        return None
    if (
        not matches
        or None in (accepted, started, observed, finished)
        or not accepted <= started <= observed <= finished
        or receipt.get("identity") != packet["identity"]
        or receipt.get("publication_id") != row["publication_id"]
        or receipt.get("request_fingerprint") != row["request_fingerprint"]
    ):
        return None
    pr = proof.get("pr")
    external_id = str(pr["number"]) if row["action_type"] == GitHubActionType.OPEN_PR else packet["effect"]["branch"]
    if row["external_id"] != external_id or (pr is not None and row["target_url"] != pr["url"]):
        return None
    confirmed = ConfirmedGitHubEffect(
        row["action_id"],
        row["publication_id"],
        row["request_fingerprint"],
        external_id,
        pr["url"] if pr else None,
        receipt["observed_at"],
    )
    return confirmed, packet, pr


def confirmed_effect(conn, source_result_id, action_id):
    """Return stable settlement evidence for a current or historical native action."""
    details = _confirmed_details(required_action(conn, source_result_id, action_id))
    return details[0] if details is not None else None


def confirmed_pr(conn, source_result_id, action_id):
    """Read the canonical PR observation and the request from the same row."""
    details = _confirmed_details(required_action(conn, source_result_id, action_id))
    return details if details is not None and details[2] is not None else None
