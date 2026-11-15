"""Materialized root result, required effect and canonical receipt facts."""

import re
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import and_, func, or_, select, tuple_

from robert_agent.foundation.contracts.delivery import (
    AuditStatus,
    PublicationStatus,
    PublishStatus,
    SourceControllerAction,
    SourceResultKind,
)
from robert_agent.foundation.contracts.work import RequestEventState, SourceIngressPhase, SourceRoundStatus
from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.persistence import records, statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.expressions import valid_json_or_empty
from robert_agent.persistence.queries import (
    request_events,
    source_github_publication,
    source_members,
    source_publication,
)
from robert_agent.persistence.queries import source_feedback as feedback_receipts
from robert_agent.persistence.rounds import source_feedback, source_notice
from robert_agent.persistence.rounds.source_evidence import context_digest, json_object


def for_round(conn, round_id):
    result = tables["source_results"]
    return records.first(statements.read(conn, select(result).where(result.c.round_id == round_id)))


def by_id(conn, source_result_id):
    result = tables["source_results"]
    return records.first(statements.read(conn, select(result).where(result.c.source_result_id == source_result_id)))


def for_source(conn, source_id, source_result_id):
    result = tables["source_results"]
    return records.first(
        statements.read(
            conn,
            select(result).where(result.c.source_id == source_id, result.c.source_result_id == source_result_id),
        )
    )


def delivery_round(conn, source_id, round_id):
    round_ = tables["source_rounds"]
    return records.first(
        statements.read(conn, select(round_).where(round_.c.source_id == source_id, round_.c.round_id == round_id))
    )


def by_idempotency_key(conn, key):
    result = tables["source_results"]
    return records.first(statements.read(conn, select(result).where(result.c.idempotency_key == key)))


def pending_fixed_notice_results(source_id):
    """Select pending fixed notice roots so obsolete notices can still be retired."""
    root, link, action = (tables[name] for name in ("source_results", "source_required_actions", "github_actions"))
    return (
        select(root.c.source_result_id)
        .select_from(
            root.join(link, link.c.source_result_id == root.c.source_result_id).join(
                action, action.c.action_id == link.c.action_id
            )
        )
        .where(
            root.c.source_id == source_id,
            root.c.kind == SourceResultKind.CONTROL_NOTICE.value,
            root.c.idempotency_key.startswith(source_notice.KEY_PREFIX),
            action.c.publish_status == PublishStatus.NOT_PUBLISHED.value,
        )
    )


def pending_fixed_notices(conn, source_id):
    return tuple(row[0] for row in statements.read(conn, pending_fixed_notice_results(source_id)))


def fixed_notice_action(conn, source, root):
    """Return the one exact ordinary action, with no dependence on its current phase."""
    actions = required_actions(conn, root["source_result_id"])
    if len(actions) == 1 and source_notice.exact_candidate(
        root, actions[0], source["source_id"], source_target(source)
    ):
        return actions[0]
    return None


def locally_retired_fixed_notice(conn, source_id, source_result_id, action_id):
    root = for_source(conn, source_id, source_result_id)
    source = tapd_source(conn, source_id) if root is not None else None
    action = fixed_notice_action(conn, source, root) if source is not None else None
    return action is not None and action["action_id"] == action_id and source_notice.retired(action)


def worker_candidate(conn, result_id):
    result, task, attempt = (tables[name] for name in ("worker_results", "tasks", "attempts"))
    latest = (
        select(attempt.c.attempt_id)
        .where(attempt.c.task_id == task.c.task_id)
        .order_by(attempt.c.attempt_no.desc())
        .limit(1)
        .correlate(task)
        .scalar_subquery()
    )
    return records.first(
        statements.read(
            conn,
            select(
                result,
                task.c.lifecycle,
                task.c.task_kind,
                task.c.workstream_id,
                attempt.c.status.label("attempt_status"),
            )
            .select_from(
                result.join(task, task.c.task_id == result.c.task_id).join(
                    attempt, and_(attempt.c.attempt_id == result.c.attempt_id, attempt.c.task_id == task.c.task_id)
                )
            )
            .where(result.c.result_id == result_id, attempt.c.attempt_id == latest),
        )
    )


def latest_worker_result_id(conn, task_id):
    result = tables["worker_results"]
    row = records.first(
        statements.read(
            conn,
            select(result.c.result_id)
            .where(result.c.task_id == task_id)
            .order_by(result.c.created_at.desc(), result.c.result_id.desc())
            .limit(1),
        )
    )
    return row[0] if row is not None else None


def candidate_actions(conn, result_id):
    action, intent = (tables[name] for name in ("github_actions", "publication_intents"))
    return statements.read(
        conn,
        select(action, intent.c.publication_id)
        .select_from(action.outerjoin(intent, intent.c.action_id == action.c.action_id))
        .where(action.c.result_id == result_id)
        .order_by(action.c.created_at, action.c.action_id),
    )


def required_actions(conn, source_result_id):
    link, action, intent, prepared, github_prepared = (
        tables[name]
        for name in (
            "source_required_actions",
            "github_actions",
            "publication_intents",
            "tapd_publication_preparations",
            "github_publication_preparations",
        )
    )
    return statements.read(
        conn,
        select(
            action,
            link.c.position,
            intent.c.publication_id,
            intent.c.status.label("intent_status"),
            intent.c.started_at.label("intent_started_at"),
            intent.c.finished_at.label("intent_finished_at"),
            intent.c.remote_evidence_json,
            intent.c.request_fingerprint.label("intent_fingerprint"),
            intent.c.error_json.label("intent_error_json"),
            intent.c.idempotency_key.label("intent_key"),
            intent.c.created_at.label("intent_created_at"),
            intent.c.updated_at.label("intent_updated_at"),
            github_prepared.c.prepared_evidence_json.label("github_prepared_evidence_json"),
            prepared.c.prepared_evidence_json,
        )
        .select_from(
            link.join(action, action.c.action_id == link.c.action_id)
            .outerjoin(intent, intent.c.action_id == action.c.action_id)
            .outerjoin(prepared, prepared.c.publication_id == intent.c.publication_id)
            .outerjoin(github_prepared, github_prepared.c.publication_id == intent.c.publication_id)
        )
        .where(link.c.source_result_id == source_result_id)
        .order_by(link.c.position),
    )


def feedback_plan(conn, root):
    """Derive only the controller feedback expected for this exact mixed root."""
    evidence = json_object(root["evidence_json"]) or {}
    mixed = root["round_id"] is not None and source_members.round_github_request_ids(conn, root["round_id"])
    if not mixed:
        return None if "github_return_targets" in evidence else ()
    if root["kind"] not in (SourceResultKind.ANSWER, SourceResultKind.QUESTION):
        return None
    saved = evidence.get("github_return_targets")
    try:
        actual = source_members.handled_return_targets(conn, root["round_id"], evidence.get("handled_members"))
        if actual is None or saved != actual:
            return None
        candidate = worker_candidate(conn, root["worker_result_id"]) if root["worker_result_id"] else None
        if root["worker_result_id"] and candidate is None:
            return None
        code = source_feedback.code_digest(candidate) if actual and candidate is not None else None
        if evidence.get("github_feedback_code_sha256") != code:
            return None
        return source_feedback.planned_actions(root["source_result_id"], actual, code)
    except (KeyError, TypeError, ValueError, RecursionError):
        return None


def required_action_sets(conn, root, actions):
    """Prove the immutable audited prefix and controller feedback suffix."""
    plan = feedback_plan(conn, root)
    if plan is None or not actions or len(actions) <= len(plan):
        return None
    prefix_size = len(actions) - len(plan)
    prefix, feedback = tuple(actions[:prefix_size]), tuple(actions[prefix_size:])
    if (
        any(action["position"] != i for i, action in enumerate(actions))
        or any(action["action_type"] == SourceControllerAction.GITHUB_FEEDBACK for action in prefix)
        or any(
            not source_feedback.action_matches(root, action, planned)
            for action, planned in zip(feedback, plan, strict=True)
        )
    ):
        return None
    if "github_return_targets" not in (json_object(root["evidence_json"]) or {}):
        return prefix, feedback

    source = tapd_source(conn, root["source_id"])
    if (
        source is None
        or prefix[-1]["provider"] != "tapd"
        or prefix[-1]["action_type"] != "comment"
        or json_object(prefix[-1]["target_json"]) != source_target(source)
        or any(row["audit_status"] != AuditStatus.ACCEPTED for row in prefix)
    ):
        return None
    if root["worker_result_id"]:
        candidate = worker_candidate(conn, root["worker_result_id"])
        if candidate is None:
            return None
        metadata = json_object(candidate["metadata_json"]) or {}
        audit = metadata.get("audit")
        if (
            not isinstance(audit, dict)
            or audit.get("status") != AuditStatus.ACCEPTED
            or audit.get("ok") is not True
            or audit.get("planned_actions")
            != [
                {
                    "provider": row["provider"],
                    "type": row["action_type"],
                    "target_id": (json_object(row["metadata_json"]) or {}).get("target_id"),
                }
                for row in prefix
            ]
            or tuple(row["action_id"] for row in prefix)
            != tuple(row["action_id"] for row in candidate_actions(conn, candidate["result_id"]))
            or any(
                row["task_id"] != candidate["task_id"]
                or row["result_id"] != candidate["result_id"]
                or row["source_result_id"] is not None
                for row in prefix
            )
        ):
            return None
    elif (
        len(prefix) != 1
        or prefix[0]["source_result_id"] != root["source_result_id"]
        or prefix[0]["task_id"] is not None
        or prefix[0]["result_id"] is not None
        or source_feedback.candidate_metadata(prefix[0])
        != {"target_id": "ticket", "body": (json_object(root["payload_json"]) or {}).get("body")}
    ):
        return None
    return prefix, feedback


def completion_candidates(
    conn,
    *,
    scopes,
    owner,
    now,
    limit=20,
    after=None,
    repositories=None,
    repo_id=None,
    task_ids=None,
    action_ids=None,
    source_result_id=None,
    linkage_only=False,
    include_mixed=True,
):
    """Bound recovery inspection to scoped, active roots claiming settled effects.

    The caller still proves canonical receipts, consumed authority and blockers
    under the claimed writer before mutating a round.
    """
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("invalid source completion limit")
    root, round_, source, session, cancel, link, action, intent, progress, task, stream, repo = (
        tables[name]
        for name in (
            "source_results",
            "source_rounds",
            "sources",
            "source_sessions",
            "source_round_cancellations",
            "source_required_actions",
            "github_actions",
            "publication_intents",
            "tapd_coordination",
            "tasks",
            "workstreams",
            "repos",
        )
    )
    links = select(link.c.action_id).where(link.c.source_result_id == root.c.source_result_id).correlate(root)
    unfinished = (
        links.select_from(
            link.join(action, action.c.action_id == link.c.action_id).outerjoin(
                intent, intent.c.action_id == action.c.action_id
            )
        )
        .where(
            or_(
                action.c.audit_status != AuditStatus.ACCEPTED.value,
                and_(
                    action.c.publish_status != PublishStatus.PUBLISHED.value,
                    or_(intent.c.publication_id.is_(None), intent.c.status != PublicationStatus.PUBLISHED.value),
                ),
            )
        )
        .exists()
    )
    frozen, ingress = (tables[name] for name in ("source_round_requests", "ingress_requests"))
    mixed = (
        select(frozen.c.request_id)
        .select_from(frozen.join(ingress, ingress.c.request_id == frozen.c.request_id))
        .where(frozen.c.round_id == root.c.round_id, ingress.c.provider == "github")
        .correlate(root)
        .exists()
    )
    model_repo = repo.alias("completion_model_repository")
    evidence = valid_json_or_empty(root.c.evidence_json)
    selected_repos = func.json_each(evidence, "$.repo_ids").table_valued("value")
    scoped_model = and_(
        mixed,
        root.c.model_run_id.is_not(None),
        action.c.source_result_id == root.c.source_result_id,
        action.c.task_id.is_(None),
        action.c.result_id.is_(None),
    )
    unmapped_model_repo = (
        select(1)
        .select_from(selected_repos.outerjoin(model_repo, model_repo.c.repo_id == selected_repos.c.value))
        .where(
            or_(
                model_repo.c.repo_id.is_(None),
                model_repo.c.enabled != 1,
                False
                if repositories is None
                else ~tuple_(source.c.project_key, model_repo.c.full_name).in_(repositories),
            )
        )
        .correlate(root, source)
        .exists()
    )
    model_repo_selected = (
        select(1).select_from(selected_repos).where(selected_repos.c.value == repo_id).correlate(root).exists()
    )
    scoped_action = (
        links.select_from(
            link.join(action, action.c.action_id == link.c.action_id)
            .outerjoin(task, task.c.task_id == action.c.task_id)
            .outerjoin(stream, stream.c.workstream_id == task.c.workstream_id)
            .outerjoin(repo, repo.c.repo_id == stream.c.repo_id)
        )
        .where(
            *source_publication.action_task_scope(
                action, stream, repo_id=None, task_ids=task_ids, action_ids=action_ids
            ),
            True
            if repo_id is None
            else or_(
                stream.c.repo_id == repo_id,
                and_(scoped_model, model_repo_selected),
            ),
            True
            if repositories is None
            else and_(
                source_publication.configured_repository(source, action, repo, repositories),
                or_(~scoped_model, ~unmapped_model_repo),
            ),
        )
        .correlate(root, source)
        .exists()
    )
    pr_stream = tables["workstreams"].alias("native_pr_link_target")
    native_prepared = tables["github_publication_preparations"]
    control = tables["workstreams"].alias("native_pr_link_control")
    owned_pr = source.alias("native_linked_pr")
    missing_pr_link = (
        links.select_from(
            link.join(action, action.c.action_id == link.c.action_id)
            .join(intent, intent.c.action_id == action.c.action_id)
            .join(native_prepared, native_prepared.c.publication_id == intent.c.publication_id)
        )
        .where(
            action.c.provider == "github",
            action.c.action_type == "open_pr",
            action.c.audit_status == AuditStatus.ACCEPTED.value,
            action.c.publish_status == PublishStatus.PUBLISHED.value,
            intent.c.status == PublicationStatus.PUBLISHED.value,
            ~select(pr_stream.c.workstream_id)
            .select_from(
                pr_stream.join(control, control.c.workstream_id == pr_stream.c.origin_workstream_id).join(
                    owned_pr, owned_pr.c.source_id == pr_stream.c.primary_source_id
                )
            )
            .where(
                control.c.primary_source_id == root.c.source_id,
                pr_stream.c.repo_id == func.json_extract(valid_json_or_empty(root.c.evidence_json), "$.repo_id"),
                owned_pr.c.provider == "github",
                func.lower(owned_pr.c.html_url) == func.lower(action.c.target_url),
                func.json_extract(valid_json_or_empty(pr_stream.c.metadata_json), "$.tapd_creation.action_id")
                == action.c.action_id,
                func.json_extract(valid_json_or_empty(pr_stream.c.metadata_json), "$.tapd_creation.source_result_id")
                == root.c.source_result_id,
                func.json_extract(valid_json_or_empty(pr_stream.c.metadata_json), "$.tapd_creation.publication_id")
                == intent.c.publication_id,
                func.json_extract(valid_json_or_empty(pr_stream.c.metadata_json), "$.tapd_creation.request_fingerprint")
                == intent.c.request_fingerprint,
            )
            .correlate(action, intent, root)
            .exists(),
        )
        .exists()
    )
    recovery = tables["source_delivery_recoveries"]
    recovering = (
        select(recovery.c.source_result_id)
        .where(
            recovery.c.source_result_id == root.c.source_result_id,
            recovery.c.source_id == root.c.source_id,
            recovery.c.completed_at.is_(None),
            recovery.c.suspended_at.is_(None),
        )
        .correlate(root)
        .exists()
    )
    query = (
        select(
            root.c.source_result_id,
            root.c.source_id,
            root.c.round_id,
            root.c.accepted_at,
            source.c.host,
            source.c.project_key,
            source.c.source_type,
            source.c.external_id,
        )
        .select_from(
            root.join(round_, and_(round_.c.round_id == root.c.round_id, round_.c.source_id == root.c.source_id))
            .join(source, source.c.source_id == root.c.source_id)
            .outerjoin(session, session.c.source_id == root.c.source_id)
            .outerjoin(progress, progress.c.source_id == root.c.source_id)
        )
        .where(
            source.c.provider == "tapd",
            tuple_(source.c.host, source.c.project_key).in_(scopes),
            root.c.kind.in_((SourceResultKind.ANSWER.value, SourceResultKind.QUESTION.value)),
            source_publication.available_lease(session, owner, now),
            or_(
                progress.c.source_id.is_(None),
                progress.c.round_id.is_(None),
                progress.c.round_id != round_.c.round_id,
                and_(
                    progress.c.status == "publication_pending",
                    or_(missing_pr_link, and_(mixed, ~unfinished)),
                ),
                progress.c.next_due_at.is_(None),
                func.julianday(progress.c.next_due_at) <= func.julianday(now),
            ),
            scoped_action,
            missing_pr_link
            if linkage_only
            else or_(
                missing_pr_link,
                and_(
                    or_(
                        and_(
                            round_.c.status == SourceRoundStatus.ACTIVE.value,
                            ~select(cancel.c.round_id).where(cancel.c.round_id == round_.c.round_id).exists(),
                        ),
                        and_(round_.c.status == SourceRoundStatus.CANCELED.value, recovering),
                    ),
                    ~unfinished,
                    True if include_mixed else ~mixed,
                ),
            ),
        )
        .order_by(root.c.accepted_at, root.c.source_result_id)
        .limit(limit)
    )
    if source_result_id is not None:
        query = query.where(root.c.source_result_id == source_result_id)
    if after is not None:
        query = query.where(tuple_(root.c.accepted_at, root.c.source_result_id) > tuple_(*after))
    return tuple(statements.read(conn, query))


RECEIPT_FIELDS = (
    "position",
    "action_id",
    "source_result_id",
    "task_id",
    "result_id",
    "provider",
    "action_type",
    "audit_status",
    "publish_status",
    "target_json",
    "metadata_json",
    "external_id",
    "target_url",
    "publication_id",
    "intent_status",
    "intent_fingerprint",
    "intent_started_at",
    "intent_finished_at",
    "prepared_evidence_json",
    "github_prepared_evidence_json",
    "remote_evidence_json",
)


def receipt_vector(actions):
    """Stable ordered facts of canonical receipts, excluding unrelated pacing diagnostics."""
    return [{field: row[field] for field in RECEIPT_FIELDS} for row in actions]


def confirmed_required_effects(conn, source_result_id, target, *, before_position=None):
    """Prove the exact ordered receipt set or the prefix before a new action."""
    root = by_id(conn, source_result_id)
    accepted = utc_timestamp(root["accepted_at"]) if root is not None else None
    if accepted is None:
        return None
    actions = required_actions(conn, source_result_id)
    if required_action_sets(conn, root, actions) is None or (
        before_position is not None
        and (not 0 <= before_position < len(actions) or actions[before_position]["position"] != before_position)
    ):
        return None
    prefix = actions if before_position is None else actions[:before_position]
    previous = accepted
    for position, row in enumerate(prefix):
        started, finished = map(utc_timestamp, (row["intent_started_at"], row["intent_finished_at"]))
        if row["position"] != position or started is None or finished is None or started < previous:
            return None
        if row["action_type"] == SourceControllerAction.GITHUB_FEEDBACK:
            receipt = feedback_receipts.confirmed_receipt(conn, source_result_id, row["action_id"])
            confirmed = utc_timestamp(receipt.observed_at) if receipt is not None else None
        elif row["provider"] == "github":
            receipt = source_github_publication.confirmed_effect(conn, source_result_id, row["action_id"])
            confirmed = utc_timestamp(receipt.observed_at) if receipt is not None else None
        elif row["provider"] == "tapd":
            prepared = json_object(row["prepared_evidence_json"])
            try:
                matched = (
                    prepared is not None
                    and context_digest(prepared) == row["intent_fingerprint"]
                    and row["action_type"] == "comment"
                    and row["audit_status"] == AuditStatus.ACCEPTED
                    and confirmed_comment(row, target) is not None
                )
            except (ValueError, TypeError, UnicodeError):
                return None
            remote = json_object(row["remote_evidence_json"]) if matched else None
            confirmed = utc_timestamp(remote["receipt"]["confirmed_at"]) if remote else None
        else:
            return None
        if confirmed is None or not started <= confirmed <= finished:
            return None
        previous = finished
    return tuple(prefix)


def source_target(source, *, external_id_key="external_id"):
    return {
        "provider": "tapd",
        "host": source["host"],
        "workspace_id": source["project_key"],
        "kind": source["source_type"],
        "external_id": source[external_id_key],
    }


def tapd_source(conn, source_id):
    source = tables["sources"]
    return records.first(
        statements.read(conn, select(source).where(source.c.source_id == source_id, source.c.provider == "tapd"))
    )


@dataclass(frozen=True, slots=True)
class TapdPublicationEvidence:
    own_comments: frozenset[tuple[str, str]]
    unknown_intents: tuple[str, ...]


def confirmed_comment(row, target):
    """Return only the canonical comment identity supported by matching stored proofs."""
    if row["intent_status"] != PublicationStatus.PUBLISHED or row["publish_status"] != PublishStatus.PUBLISHED:
        return None
    prepared = json_object(row["prepared_evidence_json"])
    remote = json_object(row["remote_evidence_json"])
    receipt = remote.get("receipt") if remote else None
    if (
        prepared is None
        or not isinstance(receipt, dict)
        or remote.get("status") != "confirmed_effect"
        or json_object(row["target_json"]) != target
    ):
        return None
    entry_type = "stories" if target["kind"] == "story" else "bug"
    external_id = receipt.get("external_id")
    identity_evidence = receipt.get("identity_evidence_id")
    principal = receipt.get("publisher_principal")
    receipt_id = receipt.get("receipt_id")
    body_digest = receipt.get("body_digest")
    if (
        receipt.get("action_id") != row["action_id"]
        or prepared.get("action_id") != row["action_id"]
        or receipt.get("target") != target
        or prepared.get("target") != target
        or receipt.get("entry_type") != entry_type
        or not isinstance(external_id, str)
        or re.fullmatch(r"[0-9]{1,64}", external_id) is None
        or row["external_id"] != external_id
        or not isinstance(receipt_id, str)
        or re.fullmatch(r"rbt-[a-z0-9]{12,64}", receipt_id) is None
        or prepared.get("receipt_id") != receipt_id
        or not isinstance(body_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", body_digest) is None
        or prepared.get("body_digest") != body_digest
        or not isinstance(prepared.get("body"), str)
        or receipt_id not in prepared["body"]
        or type(prepared.get("normalization_version")) is not int
        or prepared["normalization_version"] != 1
        or not isinstance(principal, str)
        or not principal.strip()
        or prepared.get("publisher_principal") != principal
        or not isinstance(identity_evidence, str)
        or not identity_evidence.strip()
        or utc_timestamp(receipt.get("confirmed_at")) is None
    ):
        return None
    if any(not isinstance(prepared.get(field), str) or not prepared[field] for field in ("bot_name", "reply_name")):
        return None
    publisher_proof = prepared.get("publisher_identity_evidence_id")
    body_proof = receipt.get("own_body_evidence_id")
    observed = utc_timestamp(prepared.get("publisher_observed_at"))
    confirmed = utc_timestamp(receipt.get("confirmed_at"))
    if (
        not isinstance(publisher_proof, str)
        or not publisher_proof.strip()
        or not isinstance(body_proof, str)
        or not body_proof.strip()
        or observed is None
        or confirmed < observed
    ):
        return None
    return entry_type, external_id


def tapd_publication_evidence(conn, source_id):
    """Return known owned comment IDs and unresolved effects for an exact real source."""
    source = tapd_source(conn, source_id)
    if source is None:
        raise ValueError("Unknown TAPD source")
    owner, link, action, intent, prepared, round_, member = (
        tables[name]
        for name in (
            "source_results",
            "source_required_actions",
            "github_actions",
            "publication_intents",
            "tapd_publication_preparations",
            "source_rounds",
            "source_round_tasks",
        )
    )
    roots = select(owner.c.source_result_id).where(owner.c.source_id == source_id)
    tasks = (
        select(member.c.task_id)
        .select_from(member.join(round_, round_.c.round_id == member.c.round_id))
        .where(round_.c.source_id == source_id)
    )
    rows = statements.read(
        conn,
        select(
            action,
            intent.c.publication_id,
            intent.c.status.label("intent_status"),
            intent.c.remote_evidence_json,
            prepared.c.prepared_evidence_json,
        )
        .select_from(
            action.outerjoin(intent, intent.c.action_id == action.c.action_id).outerjoin(
                prepared, prepared.c.publication_id == intent.c.publication_id
            )
        )
        .where(
            or_(
                action.c.source_result_id.in_(roots),
                action.c.action_id.in_(select(link.c.action_id).where(link.c.source_result_id.in_(roots))),
                action.c.task_id.in_(tasks),
            ),
            action.c.provider == "tapd",
            action.c.action_type == "comment",
            or_(
                intent.c.status.in_(
                    (
                        PublicationStatus.EXECUTING.value,
                        PublicationStatus.ATTENTION.value,
                        PublicationStatus.PUBLISHED.value,
                    )
                ),
                action.c.publish_status == PublishStatus.PUBLISHED.value,
            ),
        )
        .order_by(action.c.action_id),
    )
    target = source_target(source)
    owned, unknown = set(), []
    for row in rows:
        comment = confirmed_comment(row, target)
        if comment is not None:
            owned.add(comment)
        else:
            unknown.append(row["publication_id"] or row["action_id"])
    return TapdPublicationEvidence(frozenset(owned), tuple(unknown))


def exact_consumed_ledger(conn, source_result_id, row_ids):
    ledger = tables["request_events"]
    rows = statements.read(
        conn,
        select(ledger.c.row_id).where(ledger.c.source_result_id == source_result_id),
    )
    return len(rows) == len(row_ids) and {row["row_id"] for row in rows} == set(row_ids)


def consumed_members(conn, root, round_):
    """Prove the exact handled group against the frozen round and its sole ledger."""
    frozen = json_object(round_["input_snapshot_json"]) or {}
    authority = frozen.get("authority")
    evidence = json_object(root["evidence_json"]) or {}
    expected = evidence.get("handled_members")
    if (
        not isinstance(authority, dict)
        or not isinstance(expected, list)
        or not expected
        or feedback_plan(conn, root) is None
    ):
        return ()
    groups = defaultdict(list)
    for row in request_events.source_round_events(conn, root["round_id"]):
        groups[row["request_id"]].append(row)
    consumed = {}
    for request_id, rows in groups.items():
        if not any(row["source_result_id"] == root["source_result_id"] for row in rows):
            continue
        if any(
            row["source_result_id"] != root["source_result_id"]
            or row["state"] != RequestEventState.CONSUMED
            or not row["consumed_at"]
            or row["result_id"] is not None
            or row["event_id"] != row["frozen_event_id"]
            or row["phase"] in {SourceIngressPhase.PAUSED, SourceIngressPhase.REVOKED}
            or not isinstance(authority.get(request_id), dict)
            or authority[request_id].get("event_id") != row["event_id"]
            or json_object(row["authorization_json"]) != authority[request_id]
            for row in rows
        ):
            return ()
        member = {
            "request_id": request_id,
            "content_version": rows[0]["content_version"],
            "events": [
                {"row_id": row["row_id"], "event_id": row["event_id"], "fingerprint": row["event_fingerprint"]}
                for row in rows
            ],
        }
        consumed[request_id] = (member, rows[0])
    if (
        len(consumed) != len(expected)
        or [member for member, _ in consumed.values()] != expected
        or not exact_consumed_ledger(
            conn, root["source_result_id"], tuple(event["row_id"] for member in expected for event in member["events"])
        )
    ):
        return ()
    return tuple(row for _, row in consumed.values())


def unknown_github_effect(conn, source_id, *, require_positive_receipt=False, started_effect=None):
    """Flag unsettled GitHub evidence, optionally checking canonical settlement."""
    action, intent, member, round_, link, root = (
        tables[name]
        for name in (
            "github_actions",
            "publication_intents",
            "source_round_tasks",
            "source_rounds",
            "source_required_actions",
            "source_results",
        )
    )
    genuine_feedback = and_(
        action.c.action_type == SourceControllerAction.GITHUB_FEEDBACK.value,
        action.c.source_result_id == root.c.source_result_id,
        action.c.task_id.is_(None),
        action.c.result_id.is_(None),
        root.c.source_id == source_id,
    )
    rows = statements.read(
        conn,
        select(action.c.action_id, action.c.action_type, intent.c.publication_id, link.c.source_result_id)
        .select_from(
            action.outerjoin(member, member.c.task_id == action.c.task_id)
            .outerjoin(round_, round_.c.round_id == member.c.round_id)
            .outerjoin(intent, intent.c.action_id == action.c.action_id)
            .outerjoin(link, link.c.action_id == action.c.action_id)
            .outerjoin(root, root.c.source_result_id == link.c.source_result_id)
        )
        .where(
            or_(round_.c.source_id == source_id, genuine_feedback),
            action.c.provider == "github",
            or_(
                intent.c.status.in_((PublicationStatus.EXECUTING, PublicationStatus.ATTENTION)),
                and_(
                    require_positive_receipt,
                    or_(
                        action.c.publish_status == PublishStatus.PUBLISHED,
                        intent.c.status == PublicationStatus.PUBLISHED,
                    ),
                ),
                and_(
                    action.c.publish_status == PublishStatus.PUBLISHED,
                    or_(intent.c.status.is_(None), intent.c.status != PublicationStatus.PUBLISHED),
                ),
                and_(
                    intent.c.status == PublicationStatus.PUBLISHED,
                    action.c.publish_status != PublishStatus.PUBLISHED,
                ),
            ),
        ),
    )
    return any(
        (row["action_id"], row["publication_id"]) != started_effect
        and (
            not require_positive_receipt
            or (
                feedback_receipts.confirmed_receipt
                if row["action_type"] == SourceControllerAction.GITHUB_FEEDBACK
                else source_github_publication.confirmed_effect
            )(conn, row["source_result_id"], row["action_id"])
            is None
        )
        for row in rows
    )


def source_control_notice_has_pending_action(conn, source_result_id):
    return any(row["publish_status"] == PublishStatus.NOT_PUBLISHED for row in required_actions(conn, source_result_id))
