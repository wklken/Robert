"""Closed root-owned GitHub feedback identity, target and rendering recipe."""

from dataclasses import dataclass
import re

from robert_agent.foundation.contracts.delivery import AuditStatus, OutputType, SourceControllerAction
from robert_agent.persistence.rounds.source_evidence import context_digest, json_object

TARGET_FIELDS = (
    "provider",
    "host",
    "repo_id",
    "repository",
    "pr_number",
    "pr_url",
    "pr_source_id",
    "pr_workstream_id",
    "origin_workstream_id",
    "creation_result_id",
    "creation_action_id",
    "creation_publication_id",
    "creation_request_fingerprint",
)
_CODE_OUTPUTS = frozenset({OutputType.NEW_PR, OutputType.UPDATE_EXISTING_PR})


@dataclass(frozen=True, slots=True)
class FeedbackAction:
    action_id: str
    target: dict
    metadata: dict


def code_digest(candidate):
    """Reference only the immutable code/check projection from the accepted audit."""
    if candidate["output_type"] not in _CODE_OUTPUTS:
        return None
    metadata = json_object(candidate["metadata_json"]) or {}
    audit = metadata.get("audit")
    scope = audit.get("action_scope") if isinstance(audit, dict) else None
    projection = scope.get("native_github") if isinstance(scope, dict) else None
    if (
        not isinstance(audit, dict)
        or audit.get("ok") is not True
        or audit.get("status") != AuditStatus.ACCEPTED
        or not isinstance(projection, dict)
        or not projection
    ):
        raise ValueError("Source GitHub feedback lacks audited code projection")
    return context_digest(projection)


def planned_actions(root_id, return_targets, code_sha256):
    """Make at most one immutable obligation per canonical receipt-linked PR."""
    if not isinstance(return_targets, list) or (
        code_sha256 is not None and (not isinstance(code_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", code_sha256))
    ):
        return None
    by_pr, requests = {}, set()
    for entry in return_targets:
        if not isinstance(entry, dict) or set(entry) != {"request_id", "target"}:
            return None
        request_id, target = entry["request_id"], entry["target"]
        if (
            not isinstance(request_id, str)
            or request_id in requests
            or not isinstance(target, dict)
            or any(key not in target for key in TARGET_FIELDS)
        ):
            return None
        requests.add(request_id)
        identity = {key: target[key] for key in TARGET_FIELDS}
        number, repository = identity["pr_number"], identity["repository"]
        if (
            type(number) is not int
            or number < 1
            or any(not isinstance(value, str) or not value for key, value in identity.items() if key != "pr_number")
            or identity["provider"] != "github"
            or identity["host"] != "github.com"
            or identity["pr_url"].lower() != f"https://github.com/{repository}/pull/{number}".lower()
        ):
            return None
        pr = (repository.casefold(), number)
        if pr in by_pr and by_pr[pr] != identity:
            return None
        by_pr[pr] = identity

    result = []
    for pr in sorted(by_pr):
        identity = by_pr[pr]
        action_id = "source-feedback:" + context_digest([root_id, pr, identity])
        marker = "rbt-fb-" + context_digest([action_id, code_sha256])[:32]
        recipe = {
            "version": 1,
            "mode": "code_checks" if code_sha256 else "status_only",
            "marker": marker,
        }
        if code_sha256 is not None:
            recipe["code_sha256"] = code_sha256
        result.append(FeedbackAction(action_id, identity, {"feedback": recipe}))
    return tuple(result)


def candidate_metadata(row):
    """Read immutable candidate data while allowing existing local queue pacing."""
    metadata = json_object(row["metadata_json"]) or {}
    pacing = metadata.get("publish")
    if (
        isinstance(pacing, dict)
        and pacing.get("status") in {"deferred", "attention"}
        and set(pacing) <= {"status", "reason_code", "retryable", "next_retry_at", "failed_at"}
    ):
        metadata = {key: value for key, value in metadata.items() if key != "publish"}
    return metadata


def action_matches(root, row, planned):
    return (
        row["action_id"] == planned.action_id
        and row["provider"] == "github"
        and row["action_type"] == SourceControllerAction.GITHUB_FEEDBACK
        and row["audit_status"] == AuditStatus.ACCEPTED
        and row["source_result_id"] == root["source_result_id"]
        and row["task_id"] is None
        and row["result_id"] is None
        and json_object(row["target_json"]) == planned.target
        and candidate_metadata(row) == planned.metadata
    )


def fixed_body(action):
    """Render the only public text for a readonly, question, or no-code result."""
    recipe = action.metadata["feedback"]
    marker = recipe.get("marker")
    if not isinstance(marker, str) or not re.fullmatch(r"rbt-fb-[0-9a-f]{32}", marker):
        return None
    if recipe != {"version": 1, "mode": "status_only", "marker": marker}:
        return None
    return "Robert processed this request. No code changes were made.\n\n<!-- " + marker + " -->"
