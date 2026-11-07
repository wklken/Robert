"""Shared queries operations for workstreams."""

from robert_agent.foundation.contracts.delivery import AuditStatus, PublishStatus
from robert_agent.foundation.contracts.work import TaskLifecycle, WorkstreamLifecycle

from functools import cache

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import (
    select,
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


def find_lifecycle_by_workstream_id(conn, issue_workstream_id):
    query1 = (
        select(tables["workstreams"].c["lifecycle"])
        .select_from(tables["workstreams"])
        .where((tables["workstreams"].c["workstream_id"] == bindparam("p0")))
        .correlate_except(tables["workstreams"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": issue_workstream_id})


def find_repo_id_by_workstream_id(conn, workstream_id):
    query1 = (
        select(tables["workstreams"].c["repo_id"])
        .select_from(tables["workstreams"])
        .where((tables["workstreams"].c["workstream_id"] == bindparam("p0")))
        .correlate_except(tables["workstreams"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": workstream_id})


@cache
def _find_for_derived_pr_statement():
    table1 = tables["workstreams"].alias("w")
    table2 = tables["github_sources"].alias("gs")
    query3 = (
        select(
            table2.c["number"],
            table2.c["html_url"],
            table2.c["state"],
            table1.c["workstream_id"],
        )
        .select_from(
            table1.join(
                table2,
                (table2.c["source_id"] == table1.c["primary_source_id"]),
                isouter=False,
            )
        )
        .where(
            and_(
                (
                    or_(
                        (table1.c["workstream_id"] == bindparam("p0")),
                        (table1.c["origin_workstream_id"] == bindparam("p1")),
                    )
                ),
                (table2.c["source_type"] == sql_constant("pull_request")),
            )
        )
        .order_by(table1.c["updated_at"].desc(), table2.c["number"].desc())
        .limit(sql_constant(1))
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return query3


def find_for_derived_pr(conn, root_workstream_id, root_workstream_id_2):
    return statements.read(
        conn, _find_for_derived_pr_statement(), {"p0": root_workstream_id, "p1": root_workstream_id_2}
    )


def find_for_build_workstream(conn, workstream_id):
    table1 = tables["workstreams"].alias("w")
    table2 = tables["repos"].alias("r")
    query3 = (
        select(
            table1.c["workstream_id"],
            table1.c["lifecycle"],
            table1.c["active_task_id"],
            table1.c["origin_workstream_id"],
            table1.c["created_at"],
            table1.c["updated_at"],
            table2.c["full_name"].label("repo"),
        )
        .select_from(table1.join(table2, (table2.c["repo_id"] == table1.c["repo_id"]), isouter=False))
        .where((table1.c["workstream_id"] == bindparam("p0")))
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": workstream_id})


def find_for_repair_allowed(conn, task_id):
    table1 = tables["workstreams"].alias("w")
    table2 = tables["tasks"].alias("t")
    query3 = (
        select(table1.c["repo_id"])
        .select_from(
            table1.join(
                table2,
                (table2.c["workstream_id"] == table1.c["workstream_id"]),
                isouter=False,
            )
        )
        .where((table2.c["task_id"] == bindparam("p0")))
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": task_id})


def find_for_ensure_github_work_item(conn, root_workstream_id):
    table1 = tables["workstreams"].alias("w")
    table2 = tables["github_sources"].alias("gs")
    query3 = (
        select(
            table1.c["primary_source_id"],
            table1.c["created_at"],
            table2.c["title"],
            table2.c["author_login"],
        )
        .select_from(
            table1.join(
                table2,
                (table2.c["source_id"] == table1.c["primary_source_id"]),
                isouter=True,
            )
        )
        .where((table1.c["workstream_id"] == bindparam("p0")))
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": root_workstream_id})


def find_for_active_or_latest_task(conn, workstream_id):
    table1 = tables["workstreams"].alias("w")
    table2 = tables["tasks"].alias("t")
    query3 = (
        select(table2)
        .select_from(
            table1.join(
                table2,
                (table2.c["task_id"] == table1.c["active_task_id"]),
                isouter=False,
            )
        )
        .where((table1.c["workstream_id"] == bindparam("p0")))
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": workstream_id})


def find_origin_workstream_id_by_workstream_id(conn, current):
    query1 = (
        select(tables["workstreams"].c["origin_workstream_id"])
        .select_from(tables["workstreams"])
        .where((tables["workstreams"].c["workstream_id"] == bindparam("p0")))
        .correlate_except(tables["workstreams"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": current})


def find_for_has_open_pull_request(conn, workstream_id, workstream_id_2):
    table1 = tables["workstreams"].alias("w")
    table2 = tables["github_sources"].alias("gs")
    query3 = (
        select(sql_constant(1))
        .select_from(
            table1.join(
                table2,
                (table2.c["source_id"] == table1.c["primary_source_id"]),
                isouter=False,
            )
        )
        .where(
            and_(
                or_(
                    table1.c["workstream_id"] == bindparam("p0"),
                    table1.c["origin_workstream_id"] == bindparam("p1"),
                ),
                table2.c["source_type"] == sql_constant("pull_request"),
                func.lower(func.coalesce(table2.c["state"], sql_constant("open"))) == sql_constant("open"),
            )
        )
        .limit(sql_constant(1))
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": workstream_id, "p1": workstream_id_2})


def find_lifecycle_and_active_task_id_by_workstream_id(conn, workstream_id):
    query1 = (
        select(
            tables["workstreams"].c["lifecycle"],
            tables["workstreams"].c["active_task_id"],
        )
        .select_from(tables["workstreams"])
        .where((tables["workstreams"].c["workstream_id"] == bindparam("p0")))
        .correlate_except(tables["workstreams"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": workstream_id})


def load_review_participants(conn, repo_id):
    query1 = (
        select(
            tables["workstreams"].c["workstream_id"],
            tables["workstreams"].c["metadata_json"],
        )
        .select_from(tables["workstreams"])
        .where(
            and_(
                (tables["workstreams"].c["repo_id"] == bindparam("p0")),
                (tables["workstreams"].c["lifecycle"] != sql_constant(WorkstreamLifecycle.CANCELED.value)),
            )
        )
        .correlate_except(tables["workstreams"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": repo_id})


def find_metadata_json_by_workstream_id(conn, workstream_id):
    query1 = (
        select(tables["workstreams"].c["metadata_json"])
        .select_from(tables["workstreams"])
        .where((tables["workstreams"].c["workstream_id"] == bindparam("p0")))
        .correlate_except(tables["workstreams"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": workstream_id})


def find_active_task_id_by_workstream_id(conn, workstream_id):
    query1 = (
        select(tables["workstreams"].c["active_task_id"])
        .select_from(tables["workstreams"])
        .where((tables["workstreams"].c["workstream_id"] == bindparam("p0")))
        .correlate_except(tables["workstreams"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": workstream_id})


def find_for_collect_pr_remediation_decisions(conn, repo_id):
    table1 = tables["workstreams"].alias("w")
    table2 = tables["github_sources"].alias("gs")
    query3 = (
        select(
            table1.c["workstream_id"],
            table1.c["origin_workstream_id"],
            table2.c["source_key"],
            table2.c["number"],
        )
        .select_from(
            table1.join(
                table2,
                (table2.c["source_id"] == table1.c["primary_source_id"]),
                isouter=False,
            )
        )
        .where(
            and_(
                table1.c["repo_id"] == bindparam("p0"),
                not_(table1.c["origin_workstream_id"].is_(null())),
                table2.c["source_type"] == sql_constant("pull_request"),
                table2.c["state"] == sql_constant("open"),
            )
        )
        .order_by(table1.c["created_at"].asc(), table1.c["workstream_id"].asc())
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": repo_id})


def find_for_waiting_task_state(conn, workstream_id):
    table1 = tables["workstreams"].alias("w")
    table2 = tables["tasks"].alias("t")
    query3 = (
        select(table2.c["task_id"])
        .select_from(
            table1.join(
                table2,
                (table2.c["task_id"] == table1.c["active_task_id"]),
                isouter=False,
            )
        )
        .where(
            and_(
                table1.c["workstream_id"] == bindparam("p0"),
                table1.c["lifecycle"] == sql_constant(WorkstreamLifecycle.WAITING_FOR_USER.value),
                table2.c["lifecycle"] == sql_constant(TaskLifecycle.WAITING_FOR_USER.value),
            )
        )
        .limit(sql_constant(1))
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": workstream_id})


def find_for_source_has_active_unpublished_result(conn, source_id, source_id_2):
    table1 = tables["workstreams"].alias("w")
    table2 = tables["workstream_sources"].alias("ws")
    table3 = tables["tasks"].alias("t")
    table4 = tables["worker_results"].alias("wr")
    table5 = tables["github_actions"].alias("ga")
    query6 = (
        select(sql_constant(1))
        .select_from(
            table1.join(
                table2,
                (table2.c["workstream_id"] == table1.c["workstream_id"]),
                isouter=True,
            )
            .join(
                table3,
                (table3.c["workstream_id"] == table1.c["workstream_id"]),
                isouter=False,
            )
            .join(table4, (table4.c["task_id"] == table3.c["task_id"]), isouter=False)
            .join(table5, (table5.c["result_id"] == table4.c["result_id"]), isouter=True)
        )
        .where(
            and_(
                (
                    or_(
                        (table1.c["primary_source_id"] == bindparam("p0")),
                        (table2.c["source_id"] == bindparam("p1")),
                    )
                ),
                (
                    or_(
                        table3.c["lifecycle"].in_(
                            [
                                sql_constant(TaskLifecycle.QUEUED.value),
                                sql_constant(TaskLifecycle.RUNNING.value),
                                sql_constant(TaskLifecycle.WAITING_FOR_USER.value),
                            ]
                        ),
                        func.json_extract(table4.c["metadata_json"], sql_constant("$.audit.status")).is_(null()),
                        and_(
                            and_(
                                not_(table5.c["action_id"].is_(null())),
                                table5.c["audit_status"] == sql_constant(AuditStatus.ACCEPTED.value),
                            ),
                            table5.c["publish_status"] != sql_constant(PublishStatus.PUBLISHED.value),
                        ),
                    )
                ),
            )
        )
        .limit(sql_constant(1))
        .correlate_except(table1, table2, table3, table4, table5)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query6, {"p0": source_id, "p1": source_id_2})


def find_for_scan_repo(conn, repo_id):
    table1 = tables["workstreams"].alias("w")
    table2 = tables["github_sources"].alias("gs")
    query3 = (
        select(table2.c["source_type"], table2.c["number"])
        .select_from(
            table1.join(
                table2,
                (table2.c["source_id"] == table1.c["primary_source_id"]),
                isouter=False,
            )
        )
        .where(
            and_(
                table1.c["repo_id"] == bindparam("p0"),
                not_(table1.c["origin_workstream_id"].is_(null())),
                table2.c["source_type"] == sql_constant("pull_request"),
                table2.c["state"] == sql_constant("open"),
            )
        )
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": repo_id})


@cache
def _find_for_related_workstreams_statement():
    query1 = (
        select(tables["workstreams"].c["workstream_id"])
        .select_from(tables["workstreams"])
        .where(
            or_(
                (tables["workstreams"].c["workstream_id"] == bindparam("p0")),
                (tables["workstreams"].c["origin_workstream_id"] == bindparam("p1")),
            )
        )
        .correlate_except(tables["workstreams"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return query1


def find_for_related_workstreams(conn, workstream_id, workstream_id_2):
    return statements.read(
        conn, _find_for_related_workstreams_statement(), {"p0": workstream_id, "p1": workstream_id_2}
    )


@cache
def _find_for_lifecycle_facts_statement():
    table1 = tables["workstreams"].alias("w")
    table2 = tables["tasks"].alias("t")
    query3 = (
        select(table2.c["task_id"])
        .select_from(
            table1.join(
                table2,
                and_(
                    (table2.c["task_id"] == table1.c["active_task_id"]),
                    (table2.c["workstream_id"] == table1.c["workstream_id"]),
                ),
                isouter=False,
            )
        )
        .where(
            and_(
                table1.c["workstream_id"] == bindparam("p0"),
                table1.c["lifecycle"] == sql_constant(WorkstreamLifecycle.ACTIVE.value),
                table2.c["lifecycle"].in_(
                    [
                        sql_constant(TaskLifecycle.DETECTED.value),
                        sql_constant(TaskLifecycle.AUTHORIZED.value),
                        sql_constant(TaskLifecycle.CLASSIFIED.value),
                        sql_constant(TaskLifecycle.QUEUED.value),
                        sql_constant(TaskLifecycle.RUNNING.value),
                    ]
                ),
            )
        )
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return query3


def find_for_lifecycle_facts(conn, workstream_id):
    return statements.read(conn, _find_for_lifecycle_facts_statement(), {"p0": workstream_id})


def load_active_workstreams(conn, repo_id):
    query1 = (
        select(tables["workstreams"].c["workstream_id"])
        .select_from(tables["workstreams"])
        .where(
            and_(
                tables["workstreams"].c["repo_id"] == bindparam("p0"),
                tables["workstreams"]
                .c["lifecycle"]
                .in_(
                    [
                        sql_constant(WorkstreamLifecycle.ACTIVE.value),
                        sql_constant(WorkstreamLifecycle.WAITING_FOR_USER.value),
                    ]
                ),
                not_(tables["workstreams"].c["active_task_id"].is_(null())),
            )
        )
        .correlate_except(tables["workstreams"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": repo_id})


def load_known_workstreams(conn, repo_id):
    query1 = (
        select(tables["workstreams"].c["workstream_id"])
        .select_from(tables["workstreams"])
        .where(
            and_(
                (tables["workstreams"].c["repo_id"] == bindparam("p0")),
                (
                    or_(
                        (tables["workstreams"].c["lifecycle"] != sql_constant(WorkstreamLifecycle.CANCELED.value)),
                        not_(
                            func.json_extract(
                                tables["workstreams"].c["metadata_json"],
                                sql_constant("$.remote_reconciliation"),
                            ).is_(null())
                        ),
                    )
                ),
            )
        )
        .correlate_except(tables["workstreams"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": repo_id})


def find_for_remote_source_rows(conn, repo_id):
    table1 = tables["workstreams"].alias("w")
    table2 = tables["github_sources"].alias("gs")
    table4 = tables["work_items"].alias("wi")
    query5 = (
        select(sql_constant(1))
        .select_from(table4)
        .where(
            and_(
                table4.c["origin_source_id"] == table2.c["source_id"],
                table4.c["completed_at"].is_(null()),
                table4.c["canceled_at"].is_(null()),
            )
        )
        .correlate_except(table4)
        .set_label_style(LABEL_STYLE_NONE)
    )
    query3 = (
        select(
            table1.c["workstream_id"],
            table1.c["active_task_id"],
            table2.c["source_id"],
            table2.c["source_key"],
            table2.c["source_type"],
            table2.c["number"],
        )
        .select_from(
            table1.join(
                table2,
                (table2.c["source_id"] == table1.c["primary_source_id"]),
                isouter=False,
            )
        )
        .where(
            and_(
                (table1.c["repo_id"] == bindparam("p0")),
                (
                    or_(
                        and_(
                            not_(table1.c["active_task_id"].is_(null())),
                            table1.c["lifecycle"].in_(
                                [
                                    sql_constant(WorkstreamLifecycle.ACTIVE.value),
                                    sql_constant(WorkstreamLifecycle.WAITING_FOR_USER.value),
                                ]
                            ),
                        ),
                        and_(
                            table2.c["source_type"] == sql_constant("pull_request"),
                            table2.c["state"] == sql_constant("open"),
                        ),
                        and_(
                            table2.c["source_type"] == sql_constant("issue"),
                            query5.exists(),
                        ),
                    )
                ),
            )
        )
        .order_by(table1.c["updated_at"].asc(), table1.c["workstream_id"].asc())
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": repo_id})
