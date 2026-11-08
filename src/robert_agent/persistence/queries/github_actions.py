"""Shared queries operations for github_actions."""

from robert_agent.foundation.contracts.delivery import AuditStatus, GitHubActionType, PublicationStatus, PublishStatus
from robert_agent.foundation.contracts.execution import AttemptStatus
from robert_agent.foundation.contracts.supervision import RecoveryActionKind, RecoveryStatus

from functools import cache

from robert_agent.persistence.queries import sqlite_master

from robert_agent.persistence.expressions import sql_constant

from robert_agent.persistence.predicates import (
    publication_ready,
    execution_attention_free,
    runnable_task,
    earlier_action,
)

from sqlalchemy import (
    select,
    literal_column,
    bindparam,
    and_,
    or_,
    not_,
    null,
    func,
    LABEL_STYLE_NONE,
)


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def find_for_evidence(conn):
    query1 = (
        select(
            tables["github_actions"].c["action_type"],
            tables["github_actions"].c["audit_status"],
            tables["github_actions"].c["publish_status"],
            tables["github_actions"].c["target_url"],
        )
        .select_from(tables["github_actions"])
        .order_by(
            tables["github_actions"].c["created_at"].asc(),
            tables["github_actions"].c["action_id"].asc(),
        )
        .correlate_except(tables["github_actions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {})


def find_for_action_counts(conn):
    query1 = (
        select(
            tables["github_actions"].c["publish_status"],
            tables["github_actions"].c["metadata_json"],
        )
        .select_from(tables["github_actions"])
        .order_by(tables["github_actions"].c["action_id"].asc())
        .correlate_except(tables["github_actions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {})


def find_for_control_plane_state_gate(conn):
    query1 = (
        select(func.count(literal_column("*")))
        .select_from(tables["github_actions"])
        .where(
            and_(
                (tables["github_actions"].c["audit_status"] == sql_constant(AuditStatus.ACCEPTED.value)),
                (tables["github_actions"].c["publish_status"] == sql_constant(PublishStatus.NOT_PUBLISHED.value)),
            )
        )
        .correlate_except(tables["github_actions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {})


def find_for_current_evidence(conn, task_id):
    query1 = (
        select(
            tables["github_actions"].c["audit_status"],
            tables["github_actions"].c["publish_status"],
            func.count(literal_column("*")),
        )
        .select_from(tables["github_actions"])
        .where((tables["github_actions"].c["task_id"] == bindparam("p0")))
        .group_by(
            tables["github_actions"].c["audit_status"],
            tables["github_actions"].c["publish_status"],
        )
        .correlate_except(tables["github_actions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id})


def find_for_action_state(conn):
    query1 = (
        select(
            tables["github_actions"].c["publish_status"],
            tables["github_actions"].c["external_id"],
            tables["github_actions"].c["target_url"],
            tables["github_actions"].c["metadata_json"],
        )
        .select_from(tables["github_actions"])
        .where((tables["github_actions"].c["action_id"] == sql_constant("action-production-canary")))
        .correlate_except(tables["github_actions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {})


def find_for_prepare_publication_intent(conn, action_id):
    query1 = (
        select(sql_constant(1))
        .select_from(tables["github_actions"])
        .where((tables["github_actions"].c["action_id"] == bindparam("p0")))
        .correlate_except(tables["github_actions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": action_id})


def find_for_sibling_push_metadata(conn, result_id):
    query1 = (
        select(tables["github_actions"].c["metadata_json"])
        .select_from(tables["github_actions"])
        .where(
            and_(
                (tables["github_actions"].c["result_id"] == bindparam("p0")),
                (tables["github_actions"].c["action_type"] == sql_constant(GitHubActionType.PUSH_EXISTING_PR.value)),
            )
        )
        .order_by(
            tables["github_actions"].c["created_at"].desc(),
            tables["github_actions"].c["action_id"].desc(),
        )
        .correlate_except(tables["github_actions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": result_id})


def find_for_prepare_publication_intent_2(conn, action_id):
    query1 = (
        select(
            tables["github_actions"].c["action_id"],
            tables["github_actions"].c["result_id"],
            tables["github_actions"].c["task_id"],
            tables["github_actions"].c["action_type"],
            tables["github_actions"].c["target_url"],
            tables["github_actions"].c["external_id"],
            tables["github_actions"].c["audit_status"],
            tables["github_actions"].c["publish_status"],
            tables["github_actions"].c["created_at"],
            tables["github_actions"].c["metadata_json"],
        )
        .select_from(tables["github_actions"])
        .where((tables["github_actions"].c["action_id"] == bindparam("p0")))
        .correlate_except(tables["github_actions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": action_id})


def find_for_skip_pending_result_actions(conn, result_id, excluding_action_id):
    query1 = (
        select(
            tables["github_actions"].c["action_id"],
            tables["github_actions"].c["result_id"],
            tables["github_actions"].c["task_id"],
            tables["github_actions"].c["action_type"],
            tables["github_actions"].c["target_url"],
            tables["github_actions"].c["external_id"],
            tables["github_actions"].c["audit_status"],
            tables["github_actions"].c["publish_status"],
            tables["github_actions"].c["created_at"],
            tables["github_actions"].c["metadata_json"],
        )
        .select_from(tables["github_actions"])
        .where(
            and_(
                tables["github_actions"].c["result_id"] == bindparam("p0"),
                tables["github_actions"].c["action_id"] != bindparam("p1"),
                tables["github_actions"].c["audit_status"] == sql_constant(AuditStatus.ACCEPTED.value),
                tables["github_actions"].c["publish_status"] == sql_constant(PublishStatus.NOT_PUBLISHED.value),
            )
        )
        .correlate_except(tables["github_actions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": result_id, "p1": excluding_action_id})


def find_metadata_json_by_action_id(conn, action_id):
    query1 = (
        select(tables["github_actions"].c["metadata_json"])
        .select_from(tables["github_actions"])
        .where((tables["github_actions"].c["action_id"] == bindparam("p0")))
        .correlate_except(tables["github_actions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": action_id})


def find_for_retire_unstarted_actions(conn, task_id):
    query1 = (
        select(tables["github_actions"])
        .select_from(tables["github_actions"])
        .where(
            and_(
                (tables["github_actions"].c["task_id"] == bindparam("p0")),
                (tables["github_actions"].c["publish_status"] == sql_constant(PublishStatus.NOT_PUBLISHED.value)),
            )
        )
        .correlate_except(tables["github_actions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id})


def find_record_by_action_id(conn, action_id):
    query1 = (
        select(tables["github_actions"])
        .select_from(tables["github_actions"])
        .where((tables["github_actions"].c["action_id"] == bindparam("p0")))
        .correlate_except(tables["github_actions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": action_id})


def find_metadata_json_and_target_url_and_external_id_by_result_id(conn, result_id):
    query1 = (
        select(
            tables["github_actions"].c["metadata_json"],
            tables["github_actions"].c["target_url"],
            tables["github_actions"].c["external_id"],
        )
        .select_from(tables["github_actions"])
        .where((tables["github_actions"].c["result_id"] == bindparam("p0")))
        .order_by(
            tables["github_actions"].c["created_at"].asc(),
            tables["github_actions"].c["action_id"].asc(),
        )
        .correlate_except(tables["github_actions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": result_id})


def find_audit_status_and_publish_status_by_result_id(conn, result_id):
    query1 = (
        select(
            tables["github_actions"].c["audit_status"],
            tables["github_actions"].c["publish_status"],
        )
        .select_from(tables["github_actions"])
        .where((tables["github_actions"].c["result_id"] == bindparam("p0")))
        .correlate_except(tables["github_actions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": result_id})


def find_for_build_dashboard_data(conn, history_limit):
    query1 = (
        select(
            tables["github_actions"].c["action_id"],
            tables["github_actions"].c["result_id"],
            tables["github_actions"].c["task_id"],
            tables["github_actions"].c["action_type"],
            tables["github_actions"].c["target_url"],
            tables["github_actions"].c["external_id"],
            tables["github_actions"].c["audit_status"],
            tables["github_actions"].c["publish_status"],
            tables["github_actions"].c["created_at"],
            tables["github_actions"].c["metadata_json"],
        )
        .select_from(tables["github_actions"])
        .order_by(
            tables["github_actions"].c["created_at"].desc(),
            tables["github_actions"].c["action_id"].desc(),
        )
        .limit(bindparam("p0"))
        .correlate_except(tables["github_actions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": history_limit})


def find_for_publication_history(conn, task_id):
    query1 = (
        select(
            tables["github_actions"].c["action_id"],
            tables["github_actions"].c["action_type"],
            tables["github_actions"].c["audit_status"],
            tables["github_actions"].c["publish_status"],
            tables["github_actions"].c["created_at"],
            tables["github_actions"].c["metadata_json"],
        )
        .select_from(tables["github_actions"])
        .where((tables["github_actions"].c["task_id"] == bindparam("p0")))
        .order_by(
            tables["github_actions"].c["created_at"].asc(),
            tables["github_actions"].c["action_id"].asc(),
        )
        .correlate_except(tables["github_actions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id})


def find_for_actions_for_task(conn, task_id):
    query1 = (
        select(
            tables["github_actions"].c["action_id"],
            tables["github_actions"].c["action_type"],
            tables["github_actions"].c["target_url"],
            tables["github_actions"].c["external_id"],
            tables["github_actions"].c["audit_status"],
            tables["github_actions"].c["publish_status"],
            tables["github_actions"].c["created_at"],
        )
        .select_from(tables["github_actions"])
        .where((tables["github_actions"].c["task_id"] == bindparam("p0")))
        .order_by(
            tables["github_actions"].c["created_at"].desc(),
            tables["github_actions"].c["action_id"].desc(),
        )
        .correlate_except(tables["github_actions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id})


def find_for_action_counts_for_task(conn, task_id):
    query1 = (
        select(
            tables["github_actions"].c["audit_status"],
            tables["github_actions"].c["publish_status"],
            func.count(literal_column("*")).label("count"),
        )
        .select_from(tables["github_actions"])
        .where((tables["github_actions"].c["task_id"] == bindparam("p0")))
        .group_by(
            tables["github_actions"].c["audit_status"],
            tables["github_actions"].c["publish_status"],
        )
        .correlate_except(tables["github_actions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id})


def find_for_summarize_database(conn):
    query1 = (
        select(func.count(literal_column("*")))
        .select_from(tables["github_actions"])
        .where(
            tables["github_actions"]
            .c["audit_status"]
            .in_([sql_constant(AuditStatus.FAILED.value), sql_constant(AuditStatus.POLICY_VIOLATION.value)])
        )
        .correlate_except(tables["github_actions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {})


def find_for_summarize_database_2(conn):
    query1 = (
        select(func.count(literal_column("*")))
        .select_from(tables["github_actions"])
        .where(
            and_(
                tables["github_actions"].c["audit_status"] == sql_constant(AuditStatus.ACCEPTED.value),
                tables["github_actions"].c["publish_status"] == sql_constant(PublishStatus.NOT_PUBLISHED.value),
                func.json_extract(
                    tables["github_actions"].c["metadata_json"],
                    sql_constant("$.publish.status"),
                )
                == sql_constant("publish_failed"),
            )
        )
        .correlate_except(tables["github_actions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {})


def find_for_summarize_database_3(conn):
    query1 = (
        select(func.count(literal_column("*")))
        .select_from(tables["github_actions"])
        .where(
            and_(
                (tables["github_actions"].c["audit_status"] == sql_constant(AuditStatus.ACCEPTED.value)),
                (tables["github_actions"].c["publish_status"] == sql_constant(PublishStatus.SKIPPED.value)),
            )
        )
        .correlate_except(tables["github_actions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {})


def find_for_action_counts_2(conn):
    query1 = (
        select(func.count(literal_column("*")))
        .select_from(tables["github_actions"])
        .where((tables["github_actions"].c["publish_status"] == sql_constant(PublishStatus.PUBLISHED.value)))
        .correlate_except(tables["github_actions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {})


def find_for_action_counts_3(conn):
    query1 = (
        select(func.count(literal_column("*")))
        .select_from(tables["github_actions"])
        .where(
            (
                func.json_extract(
                    tables["github_actions"].c["metadata_json"],
                    sql_constant("$.publish.status"),
                )
                == sql_constant("publish_failed")
            )
        )
        .correlate_except(tables["github_actions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {})


def find_for_action_counts_4(conn):
    query1 = (
        select(func.count(literal_column("*")))
        .select_from(tables["github_actions"])
        .where(
            (
                func.json_extract(
                    tables["github_actions"].c["metadata_json"],
                    sql_constant("$.publish.deduplicated"),
                )
                == sql_constant(1)
            )
        )
        .correlate_except(tables["github_actions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {})


def find_for_retrigger_failed_github_task(conn, task_id):
    query1 = (
        select(sql_constant(1))
        .select_from(tables["github_actions"])
        .where(
            and_(
                tables["github_actions"].c["task_id"] == bindparam("p0"),
                tables["github_actions"]
                .c["audit_status"]
                .in_([sql_constant(AuditStatus.PENDING.value), sql_constant(AuditStatus.ACCEPTED.value)]),
                tables["github_actions"].c["publish_status"] == sql_constant(PublishStatus.NOT_PUBLISHED.value),
            )
        )
        .correlate_except(tables["github_actions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id})


def find_publish_status_and_metadata_json_by_result_id(conn, result_id):
    query1 = (
        select(
            tables["github_actions"].c["publish_status"],
            tables["github_actions"].c["metadata_json"],
        )
        .select_from(tables["github_actions"])
        .where((tables["github_actions"].c["result_id"] == bindparam("p0")))
        .correlate_except(tables["github_actions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": result_id})


def find_for_delivery(conn, result_id):
    table1 = tables["github_actions"].alias("ga")
    table2 = tables["publication_intents"].alias("pi")
    query3 = (
        select(
            table1.c["action_id"],
            table1.c["action_type"],
            func.json_extract(table1.c["metadata_json"], sql_constant("$.publish.receipt")).label("receipt"),
        )
        .select_from(table1.join(table2, (table2.c["action_id"] == table1.c["action_id"]), isouter=False))
        .where(
            and_(
                table1.c["result_id"] == bindparam("p0"),
                table1.c["action_type"].in_(("push_existing_pr", "open_pr")),
                table1.c["audit_status"] == sql_constant(AuditStatus.ACCEPTED.value),
                table1.c["publish_status"] == sql_constant(PublishStatus.PUBLISHED.value),
                table2.c["status"] == sql_constant(PublicationStatus.PUBLISHED.value),
            )
        )
        .order_by(table1.c["created_at"].asc(), table1.c["action_id"].asc())
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": result_id})


def find_for_ready_actions(conn, now, limit, repo_id=None, task_ids=None, action_ids=None):
    action, task, stream = (
        tables["github_actions"],
        tables["tasks"],
        tables["workstreams"],
    )
    earlier = action.alias("supervision_earlier")
    intent = tables["publication_intents"].alias("supervision_intent")
    blocked_prior = (
        select(1)
        .select_from(earlier.join(intent, intent.c.action_id == earlier.c.action_id))
        .where(
            earlier_action(earlier, action),
            func.json_extract(
                intent.c.remote_evidence_json,
                sql_constant("$.supervision_retry_required"),
            )
            == sql_constant(1),
        )
        .correlate_except(earlier, intent)
        .exists()
    )
    owner = (
        select(1)
        .where(task.c.task_id == action.c.task_id, execution_attention_free(task))
        .correlate_except(task)
        .exists()
    )
    stmt = select(
        *(
            action.c[name]
            for name in (
                "action_id",
                "result_id",
                "task_id",
                "action_type",
                "target_url",
                "external_id",
                "audit_status",
                "publish_status",
                "created_at",
                "metadata_json",
            )
        )
    ).where(publication_ready(action, now), ~blocked_prior, owner)
    if repo_id:
        scoped = (
            select(1)
            .select_from(task.join(stream, stream.c.workstream_id == task.c.workstream_id))
            .where(task.c.task_id == action.c.task_id, stream.c.repo_id == repo_id)
            .correlate_except(task, stream)
            .exists()
        )
        stmt = stmt.where(scoped)
    if task_ids is not None:
        stmt = stmt.where(action.c.task_id.in_(task_ids))
    if action_ids is not None:
        stmt = stmt.where(action.c.action_id.in_(action_ids))
    if sqlite_master.table_exists(conn, "supervision_recovery_actions"):
        recovery = tables["supervision_recovery_actions"]
        pending = tables["publication_intents"].alias("pending_recovery")
        executing = (
            select(1)
            .where(
                pending.c.action_id == action.c.action_id,
                pending.c.status == sql_constant(PublicationStatus.EXECUTING.value),
            )
            .correlate_except(pending)
            .exists()
        )
        blocked = (
            select(1)
            .where(
                recovery.c.scope_type == sql_constant("task"),
                recovery.c.scope_id == action.c.task_id,
                recovery.c.action_kind.in_(
                    (
                        sql_constant(RecoveryActionKind.PUBLICATION_RETRY.value),
                        sql_constant(RecoveryActionKind.PUBLICATION_RECONCILE.value),
                    )
                ),
                recovery.c.status.in_(
                    (
                        sql_constant(RecoveryStatus.APPLYING.value),
                        sql_constant(RecoveryStatus.OBSERVING.value),
                        sql_constant(RecoveryStatus.UNKNOWN.value),
                    )
                ),
                or_(
                    recovery.c.status.in_(
                        (sql_constant(RecoveryStatus.APPLYING.value), sql_constant(RecoveryStatus.UNKNOWN.value))
                    ),
                    recovery.c.target_id == action.c.action_id,
                ),
                ~and_(recovery.c.target_id == action.c.action_id, executing),
            )
            .correlate_except(recovery)
            .exists()
        )
        stmt = stmt.where(~blocked)
    return statements.read(conn, stmt.order_by(action.c.created_at, action.c.action_id).limit(limit))


def find_for_publication_current(conn, target_id, scope_id, result_id, attempt_id):
    table1 = tables["github_actions"].alias("a")
    table2 = tables["publication_intents"].alias("pi")
    table3 = tables["worker_results"].alias("wr")
    table4 = tables["attempts"].alias("attempt")
    table6 = tables["attempts"].alias("current")
    query7 = (
        select(func.max(table6.c["attempt_no"]))
        .select_from(table6)
        .where((table6.c["task_id"] == table1.c["task_id"]))
        .correlate_except(table6)
        .set_label_style(LABEL_STYLE_NONE)
    )
    query5 = (
        select(
            table1.c["metadata_json"],
            table2.c["publication_id"],
            table2.c["request_fingerprint"],
            func.json_extract(table3.c["metadata_json"], sql_constant("$.submission_fingerprint")).label(
                "submission_fingerprint"
            ),
            func.json_extract(table3.c["metadata_json"], sql_constant("$.audit.action_scope")).label("audited_scope"),
        )
        .select_from(
            table1.join(table2, (table2.c["action_id"] == table1.c["action_id"]), isouter=False)
            .join(table3, (table3.c["result_id"] == table1.c["result_id"]), isouter=False)
            .join(
                table4,
                and_(
                    (table4.c["attempt_id"] == table3.c["attempt_id"]),
                    (table4.c["task_id"] == table1.c["task_id"]),
                ),
                isouter=False,
            )
        )
        .where(
            and_(
                table1.c["action_id"] == bindparam("p0"),
                table1.c["task_id"] == bindparam("p1"),
                table1.c["result_id"] == bindparam("p2"),
                table4.c["attempt_id"] == bindparam("p3"),
                table1.c["audit_status"] == sql_constant(AuditStatus.ACCEPTED.value),
                table1.c["publish_status"] == sql_constant(PublishStatus.NOT_PUBLISHED.value),
                table2.c["status"] == sql_constant(PublicationStatus.EXECUTING.value),
                func.json_extract(table3.c["metadata_json"], sql_constant("$.audit.status"))
                == sql_constant("accepted"),
                table4.c["status"] == sql_constant(AttemptStatus.COMPLETED.value),
                or_(
                    func.json_type(table4.c["metadata_json"], sql_constant("$.dispatch.pid")).is_not(
                        sql_constant("integer")
                    ),
                    not_(
                        func.json_extract(
                            table4.c["metadata_json"],
                            sql_constant("$.process_stopped_at"),
                        ).is_(null())
                    ),
                ),
                table4.c["attempt_no"] == query7.scalar_subquery(),
            )
        )
        .correlate_except(table1, table2, table3, table4)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(
        conn,
        query5,
        {"p0": target_id, "p1": scope_id, "p2": result_id, "p3": attempt_id},
    )


def find_for_verified_publication(conn, target_id, scope_id, result_id):
    table1 = tables["github_actions"].alias("a")
    table2 = tables["publication_intents"].alias("pi")
    table3 = tables["worker_results"].alias("wr")
    query4 = (
        select(
            table1.c["action_id"],
            table1.c["task_id"],
            table1.c["result_id"],
            table1.c["action_type"],
            table1.c["external_id"],
            table1.c["target_url"],
            table1.c["publish_status"],
            table1.c["audit_status"],
            table1.c["metadata_json"],
            table2.c["status"].label("intent_status"),
            table2.c["publication_id"],
            table2.c["request_fingerprint"],
            table2.c["remote_evidence_json"],
            func.json_extract(table3.c["metadata_json"], sql_constant("$.audit.action_scope")).label("audited_scope"),
            func.json_extract(table3.c["metadata_json"], sql_constant("$.submission_fingerprint")).label(
                "submission_fingerprint"
            ),
        )
        .select_from(
            table1.join(table2, (table2.c["action_id"] == table1.c["action_id"]), isouter=False).join(
                table3, (table3.c["result_id"] == table1.c["result_id"]), isouter=False
            )
        )
        .where(
            and_(
                table1.c["action_id"] == bindparam("p0"),
                table1.c["task_id"] == bindparam("p1"),
                table1.c["result_id"] == bindparam("p2"),
            )
        )
        .correlate_except(table1, table2, table3)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query4, {"p0": target_id, "p1": scope_id, "p2": result_id})


def find_for_publication_facts(conn, now, result_id, limit):
    action, intent = tables["github_actions"], tables["publication_intents"]
    cols = [
        action.c[name]
        for name in (
            "action_id",
            "task_id",
            "action_type",
            "audit_status",
            "publish_status",
            "created_at",
            "external_id",
            "target_url",
            "metadata_json",
        )
    ]
    cols += [
        intent.c.status.label("intent_status"),
        intent.c.started_at.label("intent_started_at"),
        func.json_extract(intent.c.remote_evidence_json, sql_constant("$.receipt")).label("intent_receipt"),
    ]
    cols += [
        func.json_extract(action.c.metadata_json, "$.publish." + key).label(label)
        for key, label in [
            ("next_retry_at", "retry_at"),
            ("retryable", "retryable"),
            ("reconcile_required", "reconcile_required"),
            ("reason_code", "publish_reason"),
            ("published_at", "published_at"),
        ]
    ]
    cols.append(publication_ready(action, now).label("eligible"))
    stmt = (
        select(*cols)
        .select_from(action.outerjoin(intent, intent.c.action_id == action.c.action_id))
        .where(action.c.result_id == result_id)
        .order_by(action.c.created_at, action.c.action_id)
        .limit(limit)
    )
    return statements.read(conn, stmt)


def find_for_target(conn, now, target_id, task_id, result_id):
    action, intent = tables["github_actions"], tables["publication_intents"]
    stmt = (
        select(
            action.c.action_id,
            action.c.metadata_json,
            intent.c.publication_id,
            intent.c.request_fingerprint,
            intent.c.status,
        )
        .select_from(action.outerjoin(intent, intent.c.action_id == action.c.action_id))
        .where(
            publication_ready(action, now),
            action.c.action_id == target_id,
            action.c.task_id == task_id,
            action.c.result_id == result_id,
        )
    )
    return statements.read(conn, stmt)


def has_local_work_filtered(conn, items, now):
    action, task, stream = (
        tables["github_actions"],
        tables["tasks"],
        tables["workstreams"],
    )
    stmt = (
        select(1)
        .select_from(
            action.join(task, task.c.task_id == action.c.task_id).join(
                stream, stream.c.workstream_id == task.c.workstream_id
            )
        )
        .where(publication_ready(action, now), runnable_task(task))
    )
    if items:
        stmt = stmt.where(stream.c.repo_id.in_(items))
    return statements.read(conn, stmt.limit(1))


@cache
def _lifecycle_facts_filtered_statement():
    table1 = tables["github_actions"].alias("ga")
    table2 = tables["tasks"].alias("t")
    table3 = tables["publication_intents"].alias("pi")
    query4 = (
        select(table1, table3.c["status"].label("intent_status"))
        .select_from(
            table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=False).join(
                table3, (table3.c["action_id"] == table1.c["action_id"]), isouter=True
            )
        )
        .where(
            and_(
                table2.c["workstream_id"].in_(bindparam("items", expanding=True)),
                table1.c["publish_status"] == sql_constant(PublishStatus.NOT_PUBLISHED.value),
                table1.c["audit_status"].in_(
                    [sql_constant(AuditStatus.PENDING.value), sql_constant(AuditStatus.ACCEPTED.value)]
                ),
            )
        )
        .order_by(table1.c["created_at"].asc(), table1.c["action_id"].asc())
        .correlate_except(table1, table2, table3)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return query4


def lifecycle_facts_filtered(conn, items):
    return statements.read(conn, _lifecycle_facts_filtered_statement(), {"items": items})
