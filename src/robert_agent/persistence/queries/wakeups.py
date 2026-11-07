"""Shared queries operations for wakeups."""

from functools import cache

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import (
    select,
    literal_column,
    column,
    bindparam,
    and_,
    or_,
    null,
    case,
    func,
    LABEL_STYLE_NONE,
)


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def find_for_parent_allows_child(conn, retrigger_wakeup_id, parent_task_id):
    table1 = tables["wakeups"].alias("wu")
    table2 = tables["tasks"].alias("t")
    table3 = tables["workstreams"].alias("w")
    table4 = tables["github_sources"].alias("gs")
    query5 = (
        select(sql_constant(1))
        .select_from(
            table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=False)
            .join(
                table3,
                (table3.c["workstream_id"] == table2.c["workstream_id"]),
                isouter=False,
            )
            .join(
                table4,
                (table4.c["source_id"] == table3.c["primary_source_id"]),
                isouter=False,
            )
        )
        .where(
            and_(
                table1.c["wakeup_id"] == bindparam("p0"),
                table1.c["task_id"] == bindparam("p1"),
                table1.c["repo_id"] == table3.c["repo_id"],
                table1.c["status"] == sql_constant("pending"),
                table1.c["reason"] == sql_constant("manual_operator_request"),
                func.json_extract(table1.c["metadata_json"], sql_constant("$.kind"))
                == sql_constant("github_retrigger"),
                table2.c["task_kind"] == sql_constant("human_request"),
                table2.c["lifecycle"] == sql_constant("failed"),
                table3.c["lifecycle"] == sql_constant("failed"),
                table3.c["active_task_id"].is_(null()),
                func.lower(table4.c["state"]) == sql_constant("open"),
            )
        )
        .correlate_except(table1, table2, table3, table4)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query5, {"p0": retrigger_wakeup_id, "p1": parent_task_id})


def find_for_project_item(conn, task_id):
    query1 = (
        select(sql_constant(1))
        .select_from(tables["wakeups"])
        .where(
            and_(
                tables["wakeups"].c["task_id"] == bindparam("p0"),
                tables["wakeups"].c["status"] == sql_constant("pending"),
                func.json_extract(tables["wakeups"].c["metadata_json"], sql_constant("$.kind"))
                == sql_constant("github_retrigger"),
            )
        )
        .correlate_except(tables["wakeups"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id})


def find_for_get_work_item(conn, task_id):
    query1 = (
        select(sql_constant(1))
        .select_from(tables["wakeups"])
        .where(
            and_(
                tables["wakeups"].c["task_id"] == bindparam("p0"),
                tables["wakeups"].c["reason"] == sql_constant("manual_operator_request"),
                tables["wakeups"].c["status"] == sql_constant("pending"),
                func.json_extract(tables["wakeups"].c["metadata_json"], sql_constant("$.kind"))
                == sql_constant("github_retrigger"),
            )
        )
        .limit(sql_constant(1))
        .correlate_except(tables["wakeups"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id})


def find_for_retrigger_failed_github_task(conn, repo_id, task_id):
    query1 = (
        select(tables["wakeups"].c["dedupe_key"])
        .select_from(tables["wakeups"])
        .where(
            and_(
                tables["wakeups"].c["repo_id"] == bindparam("p0"),
                tables["wakeups"].c["reason"] == sql_constant("manual_operator_request"),
                tables["wakeups"].c["task_id"] == bindparam("p1"),
                tables["wakeups"].c["status"] == sql_constant("pending"),
                func.json_extract(tables["wakeups"].c["metadata_json"], sql_constant("$.kind"))
                == sql_constant("github_retrigger"),
            )
        )
        .correlate_except(tables["wakeups"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": repo_id, "p1": task_id})


def find_metadata_json_by_wakeup_id(conn, retrigger_wakeup_id):
    query1 = (
        select(tables["wakeups"].c["metadata_json"])
        .select_from(tables["wakeups"])
        .where((tables["wakeups"].c["wakeup_id"] == bindparam("p0")))
        .correlate_except(tables["wakeups"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": retrigger_wakeup_id})


def record_retrigger_preparation_failure(conn, wakeup_id):
    query1 = (
        select(tables["wakeups"].c["metadata_json"])
        .select_from(tables["wakeups"])
        .where(
            and_(
                (tables["wakeups"].c["wakeup_id"] == bindparam("p0")),
                (tables["wakeups"].c["status"] == sql_constant("pending")),
            )
        )
        .correlate_except(tables["wakeups"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": wakeup_id})


def find_for_prepare_github_retrigger_tasks(conn, repo_id, run_now, run_now_3):
    table1 = tables["wakeups"].alias("wu")
    table2 = tables["tasks"].alias("t")
    table3 = tables["workstreams"].alias("w")
    query4 = (
        select(
            table1.c["wakeup_id"],
            table1.c["task_id"],
            table2.c["workstream_id"],
            table2.c["route_id"],
            table1.c["metadata_json"],
        )
        .select_from(
            table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=False).join(
                table3,
                (table3.c["workstream_id"] == table2.c["workstream_id"]),
                isouter=False,
            )
        )
        .where(
            and_(
                table1.c["repo_id"] == bindparam("p0"),
                table1.c["reason"] == sql_constant("manual_operator_request"),
                table1.c["status"] == sql_constant("pending"),
                func.julianday(table1.c["not_before_at"]) <= func.julianday(bindparam("p1")),
                or_(
                    table1.c["expires_at"].is_(null()),
                    func.julianday(table1.c["expires_at"]) > func.julianday(bindparam("p2")),
                ),
                func.json_extract(table1.c["metadata_json"], sql_constant("$.kind"))
                == sql_constant("github_retrigger"),
                table2.c["lifecycle"] == sql_constant("failed"),
                table3.c["lifecycle"] == sql_constant("failed"),
                table3.c["active_task_id"].is_(null()),
            )
        )
        .order_by(table1.c["created_at"].asc(), table1.c["wakeup_id"].asc())
        .correlate_except(table1, table2, table3)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query4, {"p0": repo_id, "p1": run_now, "p2": run_now_3})


def find_for_scan(conn):
    table1 = tables["wakeups"].alias("w")
    table2 = tables["repos"].alias("r")
    query3 = (
        select(table2.c["full_name"])
        .distinct()
        .select_from(table1.join(table2, (table2.c["repo_id"] == table1.c["repo_id"]), isouter=False))
        .where(
            and_(
                table1.c["status"] == sql_constant("pending"),
                func.json_extract(table1.c["metadata_json"], sql_constant("$.kind"))
                == sql_constant("github_context_refresh"),
                func.julianday(table1.c["not_before_at"]) <= func.julianday(sql_constant("now")),
                or_(
                    table1.c["expires_at"].is_(null()),
                    func.julianday(table1.c["expires_at"]) > func.julianday(sql_constant("now")),
                ),
            )
        )
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {})


@cache
def _find_for_lifecycle_facts_statement():
    query1 = (
        select(sql_constant(1))
        .select_from(tables["wakeups"])
        .where(
            and_(
                tables["wakeups"].c["work_item_id"] == bindparam("p0"),
                tables["wakeups"].c["status"] == sql_constant("pending"),
                func.json_extract(tables["wakeups"].c["metadata_json"], sql_constant("$.kind"))
                == sql_constant("publication_retry"),
            )
        )
        .limit(sql_constant(1))
        .correlate_except(tables["wakeups"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return query1


def find_for_lifecycle_facts(conn, work_item_id):
    return statements.read(conn, _find_for_lifecycle_facts_statement(), {"p0": work_item_id})


def find_for_request_wakeup(conn, repo_id, reason, dedupe_key):
    query1 = (
        select(
            tables["wakeups"].c["wakeup_id"],
            tables["wakeups"].c["repo_id"],
            tables["wakeups"].c["reason"],
            tables["wakeups"].c["dedupe_key"],
            tables["wakeups"].c["work_item_id"],
            tables["wakeups"].c["task_id"],
            tables["wakeups"].c["attempt_id"],
            tables["wakeups"].c["result_id"],
            tables["wakeups"].c["source_run_id"],
            tables["wakeups"].c["consumed_run_id"],
            tables["wakeups"].c["status"],
            tables["wakeups"].c["not_before_at"],
            tables["wakeups"].c["expires_at"],
            tables["wakeups"].c["attempt_count"],
            tables["wakeups"].c["created_at"],
            tables["wakeups"].c["updated_at"],
            tables["wakeups"].c["metadata_json"],
        )
        .select_from(tables["wakeups"])
        .where(
            and_(
                tables["wakeups"].c["repo_id"] == bindparam("p0"),
                tables["wakeups"].c["reason"] == bindparam("p1"),
                tables["wakeups"].c["dedupe_key"] == bindparam("p2"),
            )
        )
        .correlate_except(tables["wakeups"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": repo_id, "p1": reason, "p2": dedupe_key})


def find_for_summarize_wakeups(conn):
    query1 = (
        select(tables["wakeups"].c["status"], func.count(literal_column("*")))
        .select_from(tables["wakeups"])
        .group_by(tables["wakeups"].c["status"])
        .correlate_except(tables["wakeups"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {})


def find_for_summarize_wakeups_2(conn):
    query1 = (
        select(
            case(
                (
                    and_(
                        (tables["wakeups"].c["reason"] == sql_constant("manual_operator_request")),
                        case(
                            (
                                func.json_valid(tables["wakeups"].c["metadata_json"]),
                                func.json_extract(
                                    tables["wakeups"].c["metadata_json"],
                                    sql_constant("$.kind"),
                                ),
                            )
                        ).in_(
                            [
                                sql_constant("github_observation_ready"),
                                sql_constant("github_context_refresh"),
                            ]
                        ),
                    ),
                    func.json_extract(tables["wakeups"].c["metadata_json"], sql_constant("$.kind")),
                ),
                else_=tables["wakeups"].c["reason"],
            ).label("logical_reason"),
            func.count(literal_column("*")),
        )
        .select_from(tables["wakeups"])
        .group_by(column("logical_reason"))
        .correlate_except(tables["wakeups"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {})


def find_for_summarize_wakeups_3(conn, now, now_2):
    query1 = (
        select(func.count(literal_column("*")))
        .select_from(tables["wakeups"])
        .where(
            and_(
                tables["wakeups"].c["status"] == sql_constant("pending"),
                tables["wakeups"].c["not_before_at"] <= bindparam("p0"),
                or_(
                    tables["wakeups"].c["expires_at"].is_(null()),
                    tables["wakeups"].c["expires_at"] > bindparam("p1"),
                ),
            )
        )
        .correlate_except(tables["wakeups"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": now, "p1": now_2})


def has_due_context_refresh_filtered(conn, items, value_1, value_2):
    query1 = (
        select(sql_constant(1))
        .select_from(tables["wakeups"])
        .where(
            and_(
                tables["wakeups"].c["repo_id"].in_(bindparam("items", expanding=True)),
                tables["wakeups"].c["status"] == sql_constant("pending"),
                func.json_extract(tables["wakeups"].c["metadata_json"], sql_constant("$.kind"))
                == sql_constant("github_context_refresh"),
                func.julianday(tables["wakeups"].c["not_before_at"]) <= func.julianday(bindparam("p0")),
                or_(
                    tables["wakeups"].c["expires_at"].is_(null()),
                    func.julianday(tables["wakeups"].c["expires_at"]) > func.julianday(bindparam("p1")),
                ),
            )
        )
        .limit(sql_constant(1))
        .correlate_except(tables["wakeups"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": value_1, "p1": value_2, "items": items})


def list_wakeups(conn, status=None, limit=20, now=None):
    query1 = (
        select(
            tables["wakeups"].c["wakeup_id"],
            tables["wakeups"].c["repo_id"],
            tables["wakeups"].c["reason"],
            tables["wakeups"].c["dedupe_key"],
            tables["wakeups"].c["work_item_id"],
            tables["wakeups"].c["task_id"],
            tables["wakeups"].c["attempt_id"],
            tables["wakeups"].c["result_id"],
            tables["wakeups"].c["source_run_id"],
            tables["wakeups"].c["consumed_run_id"],
            tables["wakeups"].c["status"],
            tables["wakeups"].c["not_before_at"],
            tables["wakeups"].c["expires_at"],
            tables["wakeups"].c["attempt_count"],
            tables["wakeups"].c["created_at"],
            tables["wakeups"].c["updated_at"],
            tables["wakeups"].c["metadata_json"],
        )
        .select_from(tables["wakeups"])
        .order_by(
            case(
                (sql_constant("pending"), sql_constant(0)),
                (sql_constant("expired"), sql_constant(1)),
                (sql_constant("consumed"), sql_constant(2)),
                value=tables["wakeups"].c["status"],
                else_=sql_constant(3),
            ).asc(),
            tables["wakeups"].c["not_before_at"].asc(),
            tables["wakeups"].c["created_at"].asc(),
            tables["wakeups"].c["wakeup_id"].asc(),
        )
        .limit(bindparam("p0"))
        .correlate_except(tables["wakeups"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    if status:
        query1 = query1.where(tables["wakeups"].c.status == status)
    if now:
        query1 = query1.where(
            or_(
                tables["wakeups"].c.expires_at.is_(None),
                tables["wakeups"].c.expires_at > now,
            )
        )
    return statements.read(conn, query1, {"p0": limit})
