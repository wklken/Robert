"""Shared queries operations for pr_remediation_episodes."""

from robert_agent.foundation.contracts.work import RemediationStatus

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import select, bindparam, and_, null, case, LABEL_STYLE_NONE


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def find_status_by_episode_id(conn, subject_id):
    query1 = (
        select(tables["pr_remediation_episodes"].c["status"])
        .select_from(tables["pr_remediation_episodes"])
        .where((tables["pr_remediation_episodes"].c["episode_id"] == bindparam("p0")))
        .correlate_except(tables["pr_remediation_episodes"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": subject_id})


def find_workstream_id_and_last_task_id_by_episode_id(conn, subject_id):
    query1 = (
        select(
            tables["pr_remediation_episodes"].c["workstream_id"],
            tables["pr_remediation_episodes"].c["last_task_id"],
        )
        .select_from(tables["pr_remediation_episodes"])
        .where((tables["pr_remediation_episodes"].c["episode_id"] == bindparam("p0")))
        .correlate_except(tables["pr_remediation_episodes"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": subject_id})


def find_last_task_id_by_episode_id(conn, subject_id):
    query1 = (
        select(tables["pr_remediation_episodes"].c["last_task_id"])
        .select_from(tables["pr_remediation_episodes"])
        .where((tables["pr_remediation_episodes"].c["episode_id"] == bindparam("p0")))
        .correlate_except(tables["pr_remediation_episodes"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": subject_id})


def find_status_and_metadata_json_by_episode_id(conn, episode):
    query1 = (
        select(
            tables["pr_remediation_episodes"].c["status"],
            tables["pr_remediation_episodes"].c["metadata_json"],
        )
        .select_from(tables["pr_remediation_episodes"])
        .where((tables["pr_remediation_episodes"].c["episode_id"] == bindparam("p0")))
        .correlate_except(tables["pr_remediation_episodes"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": episode})


def find_for_collect_pr_remediation_decisions(conn, repo_id):
    query2 = (
        select(tables["workstreams"].c["workstream_id"])
        .select_from(tables["workstreams"])
        .where((tables["workstreams"].c["repo_id"] == bindparam("p0")))
        .correlate_except(tables["workstreams"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    query1 = (
        select(tables["pr_remediation_episodes"].c["episode_id"])
        .select_from(tables["pr_remediation_episodes"])
        .where(
            and_(
                tables["pr_remediation_episodes"].c["workstream_id"].in_(query2),
                tables["pr_remediation_episodes"]
                .c["status"]
                .in_(
                    [
                        sql_constant(RemediationStatus.RESOLVED.value),
                        sql_constant(RemediationStatus.EXHAUSTED.value),
                        sql_constant(RemediationStatus.SUPERSEDED.value),
                        sql_constant(RemediationStatus.CANCELED.value),
                    ]
                ),
            )
        )
        .correlate_except(tables["pr_remediation_episodes"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": repo_id})


def find_for_collect_pr_remediation_decisions_2(conn, workstream_id, value_2):
    query1 = (
        select(
            tables["pr_remediation_episodes"].c["episode_id"],
            tables["pr_remediation_episodes"].c["episode_kind"],
        )
        .select_from(tables["pr_remediation_episodes"])
        .where(
            and_(
                tables["pr_remediation_episodes"].c["workstream_id"] == bindparam("p0"),
                tables["pr_remediation_episodes"].c["subject_key"] == bindparam("p1"),
                tables["pr_remediation_episodes"].c["status"] == sql_constant(RemediationStatus.OPEN.value),
            )
        )
        .correlate_except(tables["pr_remediation_episodes"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": workstream_id, "p1": value_2})


def load_episode(conn, episode_id):
    query1 = (
        select(
            tables["pr_remediation_episodes"].c["episode_id"],
            tables["pr_remediation_episodes"].c["workstream_id"],
            tables["pr_remediation_episodes"].c["episode_kind"],
            tables["pr_remediation_episodes"].c["subject_key"],
            tables["pr_remediation_episodes"].c["observed_head_sha"],
            tables["pr_remediation_episodes"].c["observed_base_sha"],
            tables["pr_remediation_episodes"].c["result_head_sha"],
            tables["pr_remediation_episodes"].c["status"],
            tables["pr_remediation_episodes"].c["attempt_count"],
            tables["pr_remediation_episodes"].c["failure_signature"],
            tables["pr_remediation_episodes"].c["first_seen_at"],
            tables["pr_remediation_episodes"].c["updated_at"],
            tables["pr_remediation_episodes"].c["terminal_at"],
            tables["pr_remediation_episodes"].c["last_task_id"],
            tables["pr_remediation_episodes"].c["metadata_json"],
        )
        .select_from(tables["pr_remediation_episodes"])
        .where((tables["pr_remediation_episodes"].c["episode_id"] == bindparam("p0")))
        .correlate_except(tables["pr_remediation_episodes"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": episode_id})


def find_for_episodes_for_workstream(conn, workstream_id):
    query1 = (
        select(
            tables["pr_remediation_episodes"].c["episode_id"],
            tables["pr_remediation_episodes"].c["workstream_id"],
            tables["pr_remediation_episodes"].c["episode_kind"],
            tables["pr_remediation_episodes"].c["subject_key"],
            tables["pr_remediation_episodes"].c["observed_head_sha"],
            tables["pr_remediation_episodes"].c["observed_base_sha"],
            tables["pr_remediation_episodes"].c["result_head_sha"],
            tables["pr_remediation_episodes"].c["status"],
            tables["pr_remediation_episodes"].c["attempt_count"],
            tables["pr_remediation_episodes"].c["failure_signature"],
            tables["pr_remediation_episodes"].c["first_seen_at"],
            tables["pr_remediation_episodes"].c["updated_at"],
            tables["pr_remediation_episodes"].c["terminal_at"],
            tables["pr_remediation_episodes"].c["last_task_id"],
            tables["pr_remediation_episodes"].c["metadata_json"],
        )
        .select_from(tables["pr_remediation_episodes"])
        .where((tables["pr_remediation_episodes"].c["workstream_id"] == bindparam("p0")))
        .order_by(
            tables["pr_remediation_episodes"].c["first_seen_at"].asc(),
            tables["pr_remediation_episodes"].c["episode_id"].asc(),
        )
        .correlate_except(tables["pr_remediation_episodes"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": workstream_id})


def find_for_next_system_decisions(conn, repo_id):
    table1 = tables["pr_remediation_episodes"].alias("e")
    table2 = tables["workstreams"].alias("w")
    query3 = (
        select(table1.c["episode_id"])
        .select_from(
            table1.join(
                table2,
                (table2.c["workstream_id"] == table1.c["workstream_id"]),
                isouter=False,
            )
        )
        .where(
            and_(
                table2.c["repo_id"] == bindparam("p0"),
                table2.c["active_task_id"].is_(null()),
                table1.c["status"] == sql_constant(RemediationStatus.OPEN.value),
            )
        )
        .order_by(
            case(
                (sql_constant("merge_conflict"), sql_constant(0)),
                value=table1.c["episode_kind"],
                else_=sql_constant(1),
            ).asc(),
            table1.c["first_seen_at"].asc(),
            table1.c["episode_id"].asc(),
        )
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": repo_id})


def find_for_cancel_disabled_episodes(conn, repo_id):
    table1 = tables["pr_remediation_episodes"].alias("e")
    table2 = tables["workstreams"].alias("w")
    query3 = (
        select(table1.c["episode_id"])
        .select_from(
            table1.join(
                table2,
                (table2.c["workstream_id"] == table1.c["workstream_id"]),
                isouter=False,
            )
        )
        .where(
            and_(
                (table2.c["repo_id"] == bindparam("p0")),
                table1.c["status"].in_(
                    [
                        sql_constant(RemediationStatus.OPEN.value),
                        sql_constant(RemediationStatus.QUEUED.value),
                        sql_constant(RemediationStatus.REMEDIATING.value),
                        sql_constant(RemediationStatus.WAITING_FOR_SIGNAL.value),
                    ]
                ),
            )
        )
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": repo_id})


def find_record_by_episode_id(conn, episode_id):
    query1 = (
        select(tables["pr_remediation_episodes"])
        .select_from(tables["pr_remediation_episodes"])
        .where((tables["pr_remediation_episodes"].c["episode_id"] == bindparam("p0")))
        .correlate_except(tables["pr_remediation_episodes"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": episode_id})
