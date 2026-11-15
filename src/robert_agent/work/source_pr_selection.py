"""Pure current GitHub target facts for a canonical TAPD-created PR lineage."""

import re
from dataclasses import asdict, dataclass

from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.persistence.queries.source_pr_lineage import same_pr_url


@dataclass(frozen=True, slots=True)
class ExistingPRTarget:
    workstream_id: str
    action_id: str
    number: int
    branch_name: str
    head_sha: str
    base_branch: str
    base_sha: str
    head_repository: str | None = None


def _text(value, pattern):
    return isinstance(value, str) and bool(re.fullmatch(pattern, value))


def valid_ref(value):
    return _text(value, r"[A-Za-z0-9][A-Za-z0-9._/-]{0,180}") and not (
        ".." in value
        or value.endswith(".")
        or any(
            not component or component.startswith(".") or component.endswith(".lock") for component in value.split("/")
        )
    )


def current_observation(lineage, raw, publisher):
    """Normalize an independent read of the exact previously proved PR."""
    if not isinstance(raw, dict):
        return None
    head, base, user = raw.get("head"), raw.get("base"), raw.get("user")
    if not all(isinstance(item, dict) for item in (head, base, user)):
        return None
    if not isinstance(head.get("repo"), dict) or not isinstance(base.get("repo"), dict):
        return None
    head_repo, base_repo = head["repo"].get("full_name"), base["repo"].get("full_name")
    head_ref, base_ref = head.get("ref"), base.get("ref")
    head_sha, base_sha = head.get("sha"), base.get("sha")
    author = user.get("login")
    state, merged, merged_at = raw.get("state"), raw.get("merged"), raw.get("merged_at")
    if (
        type(raw.get("number")) is not int
        or raw["number"] != lineage.number
        or not same_pr_url(raw.get("html_url"), lineage.url)
        or not _text(author, r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
        or author.casefold() != lineage.author.casefold()
        or not isinstance(publisher, str)
        or (state == "open" and author.casefold() != publisher.casefold())
        or not isinstance(base_repo, str)
        or base_repo.casefold() != lineage.repository.casefold()
        or not isinstance(head_repo, str)
        or head_repo.casefold() != lineage.head_repository.casefold()
        or not valid_ref(head_ref)
        or head_ref != lineage.head_branch
        or not valid_ref(base_ref)
        or not all(_text(sha, r"[0-9a-f]{40}") for sha in (head_sha, base_sha))
        or type(merged) is not bool
        or "merged_at" not in raw
        or not isinstance(state, str)
        or state not in {"open", "closed"}
        or (merged and (state != "closed" or utc_timestamp(merged_at) is None))
        or (not merged and merged_at is not None)
        or (lineage.creation_state == "merged" and not merged)
    ):
        return None
    return {
        "action_id": lineage.action_id,
        "publication_id": lineage.publication_id,
        "request_fingerprint": lineage.request_fingerprint,
        "workstream_id": lineage.workstream_id,
        "number": lineage.number,
        "url": lineage.url,
        "state": "merged" if merged else state,
        "author": author.casefold(),
        "head_repository": head_repo.casefold(),
        "head_branch": head_ref,
        "head_sha": head_sha,
        "base_repository": base_repo.casefold(),
        "base_branch": base_ref,
        "base_sha": base_sha,
        "original_base_branch": lineage.base_branch,
    }


def select_target(history, observations, default_base):
    """Hold on incomplete coverage or incompatible existing targets."""
    if history is None:
        return "held", "source_pr_lineage_unavailable", None
    if len(history) != len(observations):
        return "held", "source_pr_observation_unavailable", None
    if any(item["state"] == "open" and item["base_branch"] != item["original_base_branch"] for item in observations):
        return "held", "source_pr_target_retargeted", None
    if any(item["state"] == "closed" for item in observations):
        return "held", "source_pr_closed_unmerged", None
    opened = [item for item in observations if item["state"] == "open"]
    if len(opened) > 1:
        return "held", "source_pr_multiple_open", None
    if not opened:
        return (
            ("fresh", "source_pr_fresh_branch", None)
            if valid_ref(default_base)
            else ("held", "source_pr_default_base_unavailable", None)
        )
    item = opened[0]
    return (
        "reuse",
        "source_pr_open_reuse",
        ExistingPRTarget(
            item["workstream_id"],
            item["action_id"],
            item["number"],
            item["head_branch"],
            item["head_sha"],
            item["base_branch"],
            item["base_sha"],
            item["head_repository"],
        ),
    )


def normalize_observations(history, snapshots, publisher):
    """Keep only the independently verified prefix of the exact receipt set."""
    observations = []
    for link, raw in zip(history or (), snapshots):
        item = current_observation(link, raw, publisher)
        if item is None:
            break
        observations.append(item)
    return observations


def effect_matches_selection(decision, history, current):
    """Recheck approved scope live while leaving original selection and packet frozen."""
    status, _, target = select_target(history, current, decision["default_base_branch"])
    if status != decision["status"] or (asdict(target) if target else None) != decision["target"]:
        return False
    # Every previous terminal link must still be conclusively terminal. A
    # merged PR's base SHA can move independently from new development.
    return len(current) == len(decision["observations"]) and all(
        tuple(item[key] for key in ("action_id", "publication_id", "request_fingerprint", "state"))
        == tuple(old[key] for key in ("action_id", "publication_id", "request_fingerprint", "state"))
        for item, old in zip(current, decision["observations"], strict=True)
    )
