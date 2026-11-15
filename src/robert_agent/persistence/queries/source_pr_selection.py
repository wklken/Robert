"""Read bounded current PR selection and canonical creation history."""

from sqlalchemy import func, or_, select

from robert_agent.foundation.contracts.delivery import GitHubActionType, PublishStatus
from robert_agent.persistence import records, statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.expressions import valid_json_or_empty
from robert_agent.persistence.queries import source_pr_lineage
from robert_agent.persistence.rounds.source_evidence import context_digest, json_object


def current(conn, round_id, repo_id):
    selection = tables["source_pr_selections"]
    return records.first(
        statements.read(conn, select(selection).where(selection.c.round_id == round_id, selection.c.repo_id == repo_id))
    )


def creation_history(conn, source_id, repo_id, *, limit=12):
    """Return complete, exact creation links or None if scope is incomplete."""
    origin = source_pr_lineage.origin_workstream(conn, source_id, repo_id)
    action, required, root, member, stream = (
        tables[name]
        for name in ("github_actions", "source_required_actions", "source_results", "source_round_tasks", "workstreams")
    )
    published = statements.read(
        conn,
        select(action.c.action_id)
        .select_from(
            required.join(action, action.c.action_id == required.c.action_id)
            .join(root, root.c.source_result_id == required.c.source_result_id)
            .outerjoin(member, (member.c.task_id == action.c.task_id) & (member.c.round_id == root.c.round_id))
        )
        .where(
            root.c.source_id == source_id,
            or_(
                member.c.repo_id == repo_id,
                func.json_extract(valid_json_or_empty(root.c.evidence_json), "$.repo_id") == repo_id,
            ),
            action.c.provider == "github",
            action.c.action_type == GitHubActionType.OPEN_PR.value,
            action.c.publish_status == PublishStatus.PUBLISHED.value,
        )
        .order_by(action.c.action_id)
        .limit(limit + 1),
    )
    if origin is None:
        return None if published else ()
    links = statements.read(
        conn,
        select(stream.c.workstream_id, stream.c.metadata_json)
        .where(stream.c.origin_workstream_id == origin[0], stream.c.repo_id == repo_id)
        .order_by(stream.c.workstream_id)
        .limit(limit + 1),
    )
    if len(published) != len(links) or len(links) > limit:
        return None
    result = []
    for row in links:
        creation = (json_object(row["metadata_json"]) or {}).get("tapd_creation")
        action_id = creation.get("action_id") if isinstance(creation, dict) else None
        if not isinstance(action_id, str):
            return None
        link = source_pr_lineage.confirmed_pr_lineage(
            conn,
            source_id=source_id,
            repo_id=repo_id,
            workstream_id=row["workstream_id"],
            action_id=action_id,
        )
        if link is None:
            return None
        result.append(link)
    if sorted(link.action_id for link in result) != [row[0] for row in published]:
        return None
    if len({link.number for link in result}) != len(result):
        return None
    return tuple(sorted(result, key=lambda link: link.number))


def bound_decision(conn, *, source_id, round_id, repo_id, fingerprint):
    """Verify the exact persisted selection and its current creation membership."""
    selection = current(conn, round_id, repo_id)
    saved = json_object(selection["decision_json"]) if selection else None
    if not saved or saved.get("fingerprint") != fingerprint:
        return None
    proof = {key: value for key, value in saved.items() if key != "fingerprint"}
    try:
        computed = context_digest(proof)
    except (TypeError, ValueError, RecursionError):
        return None
    if (
        computed != fingerprint
        or saved.get("source_id") != source_id
        or saved.get("round_id") != round_id
        or saved.get("repo_id") != repo_id
        or saved.get("status") not in {"fresh", "reuse"}
    ):
        return None
    history = creation_history(conn, source_id, repo_id)
    if history is None or saved.get("expected_actions") != [item.action_id for item in history]:
        return None
    observations = saved.get("observations")
    if not isinstance(observations, list) or len(observations) != len(history):
        return None
    for link, observation in zip(history, observations, strict=True):
        if not isinstance(observation, dict) or any(
            observation.get(field) != getattr(link, field)
            for field in ("action_id", "publication_id", "request_fingerprint", "workstream_id", "number")
        ):
            return None
    return saved


def effect_selection(conn, row):
    """Return admitted attempt's current selection, or legacy/no-proof distinction."""
    attempt, result = (tables[name] for name in ("attempts", "worker_results"))
    value = records.first(
        statements.read(
            conn,
            select(attempt.c.metadata_json, result.c.metadata_json)
            .select_from(attempt.join(result, result.c.attempt_id == attempt.c.attempt_id))
            .where(
                attempt.c.task_id == row["task_id"],
                attempt.c.attempt_id == row["attempt_id"],
                result.c.result_id == row["result_id"],
                result.c.task_id == row["task_id"],
            ),
        )
    )
    metadata = json_object(value[0]) if value else None
    if metadata is None:
        return False, None
    candidate = json_object(value[1]) or {}
    attempt_scope = metadata.get("native_workspace") or {}
    audited_scope = candidate
    for field in ("audit", "action_scope", "native_github", "purpose"):
        audited_scope = audited_scope.get(field) or {}
        if not isinstance(audited_scope, dict):
            return False, None
    if not isinstance(attempt_scope, dict):
        return False, None
    required = attempt_scope.get("pr_selection_required") is True or audited_scope.get("pr_selection_required") is True
    fingerprint = metadata.get("native_pr_selection")
    if not required and fingerprint is None:
        # Pre-selection attempts retain their frozen native publication contract.
        return "native_route_resolution" not in metadata, None
    if not isinstance(fingerprint, str) or any(
        proof.get("pr_selection_required") is not True or proof.get("pr_selection") != fingerprint
        for proof in (attempt_scope, audited_scope)
    ):
        return False, None
    decision = bound_decision(
        conn,
        source_id=row["source_id"],
        round_id=row["round_id"],
        repo_id=row["repo_id"],
        fingerprint=fingerprint,
    )
    return decision is not None, decision
