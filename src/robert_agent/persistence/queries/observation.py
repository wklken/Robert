from robert_agent.persistence.expressions import sql_constant
from sqlalchemy import (
    select,
    bindparam,
    and_,
    or_,
    not_,
    null,
    func,
    tuple_,
    LABEL_STYLE_NONE,
)

from robert_agent.persistence.predicates import runnable_task

from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def task_page(conn, cursor, limit, since, repos=(), has_incidents=True):
    table1 = tables["tasks"].alias("t")
    table2 = tables["workstreams"].alias("w")
    table3 = tables["repos"].alias("r")
    table4 = tables["github_sources"].alias("s")
    table5 = tables["work_items"].alias("wi")
    query6 = (
        select(sql_constant(1))
        .select_from(table5)
        .where(
            and_(
                (table5.c["workstream_id"] == table2.c["workstream_id"]),
                not_(table5.c["canceled_at"].is_(null())),
            )
        )
        .correlate_except(table5)
        .set_label_style(LABEL_STYLE_NONE)
    )
    query7 = (
        select(
            table1.c["task_id"],
            table1.c["lifecycle"],
            table1.c["updated_at"].label("task_updated_at"),
            table1.c["route_id"],
            table1.c["expected_output"],
            table2.c["lifecycle"].label("stream_lifecycle"),
            table2.c["active_task_id"],
            table2.c["repo_id"],
            table3.c["full_name"],
            table3.c["enabled"].label("repo_enabled"),
            func.coalesce(
                (func.lower(table4.c["state"]) == sql_constant("closed")),
                sql_constant(0),
            ).label("source_closed"),
            query6.exists().label("canceled"),
            sql_constant(1).label("runnable"),
            func.json_extract(table1.c["metadata_json"], sql_constant("$.supervision_lineage_key")).label(
                "lineage_key"
            ),
            func.json_extract(table1.c["metadata_json"], sql_constant("$.audit_retry_required")).label(
                "audit_retry_required"
            ),
            func.json_extract(table1.c["metadata_json"], sql_constant("$.dispatch_wait")).label("dispatch_wait"),
            func.json_extract(table1.c["metadata_json"], sql_constant("$.wait_state.lease_expires_at")).label(
                "repository_busy_until"
            ),
            func.json_extract(table1.c["metadata_json"], sql_constant("$.pending_handoff")).label("pending_handoff"),
        )
        .select_from(
            table1.join(
                table2,
                (table2.c["workstream_id"] == table1.c["workstream_id"]),
                isouter=False,
            )
            .join(table3, (table3.c["repo_id"] == table2.c["repo_id"]), isouter=False)
            .join(
                table4,
                (table4.c["source_id"] == table2.c["primary_source_id"]),
                isouter=True,
            )
        )
        .where((table1.c["task_id"] > bindparam("p0")))
        .order_by(table1.c["task_id"].asc())
        .limit(bindparam("p1"))
        .correlate_except(table1, table2, table3, table4)
        .set_label_style(LABEL_STYLE_NONE)
    )
    cols = list(query7.selected_columns)
    cols[12] = runnable_task(table1).label("runnable")
    query7 = query7.with_only_columns(*cols)
    eligible = or_(
        table1.c.lifecycle.in_(
            (
                sql_constant("detected"),
                sql_constant("authorized"),
                sql_constant("classified"),
                sql_constant("queued"),
                sql_constant("running"),
                sql_constant("waiting_for_user"),
            )
        ),
        and_(
            table2.c.active_task_id == table1.c.task_id,
            table2.c.lifecycle == sql_constant("active"),
        ),
        func.julianday(table1.c.updated_at) >= func.julianday(since),
    )
    if has_incidents:
        i = tables["supervision_incidents"]
        eligible = or_(
            eligible,
            select(sql_constant(1))
            .where(
                i.c.scope_type == sql_constant("task"),
                i.c.scope_id == table1.c.task_id,
                i.c.status.not_in((sql_constant("resolved"), sql_constant("dismissed"))),
            )
            .exists(),
        )
    query7 = query7.where(eligible)
    if repos:
        query7 = query7.where(table3.c.full_name.in_(repos))
    return statements.read(conn, query7, {"p0": cursor, "p1": limit})


def receipt_page(conn, scope_type, scope_id, since, limit, repos=()):
    table1 = tables["supervision_checkpoints"].alias("c")
    table2 = tables["repos"].alias("r")
    table3 = tables["github_sources"].alias("gs")
    table5 = tables["supervision_incidents"].alias("i")
    query6 = (
        select(sql_constant(1))
        .select_from(table5)
        .where(
            and_(
                table5.c["scope_type"] == table1.c["scope_type"],
                table5.c["scope_id"] == table1.c["scope_id"],
                not_(table5.c["status"].in_([sql_constant("resolved"), sql_constant("dismissed")])),
            )
        )
        .correlate_except(table5)
        .set_label_style(LABEL_STYLE_NONE)
    )
    query4 = (
        select(
            table1.c["scope_type"],
            table1.c["scope_id"],
            table1.c["lineage_key"],
            table1.c["metadata_json"],
            table1.c["updated_at"].label("disposition_at"),
            func.coalesce(
                (func.lower(table3.c["state"]) == sql_constant("closed")),
                sql_constant(0),
            ).label("source_closed"),
            table2.c["enabled"].label("repo_enabled"),
        )
        .select_from(
            table1.join(
                table2,
                (table2.c["repo_id"] == func.json_extract(table1.c["metadata_json"], sql_constant("$.repo_id"))),
                isouter=False,
            ).join(
                table3,
                (table3.c["source_id"] == func.json_extract(table1.c["metadata_json"], sql_constant("$.source_id"))),
                isouter=True,
            )
        )
        .where(
            and_(
                table1.c["scope_type"].in_([sql_constant("github_event"), sql_constant("work_item_event")]),
                table1.c["rule_id"] == sql_constant("admission"),
                tuple_(table1.c["scope_type"], table1.c["scope_id"]) > tuple_(bindparam("p0"), bindparam("p1")),
                or_(
                    or_(
                        or_(
                            func.julianday(table1.c["updated_at"]) >= func.julianday(bindparam("p2")),
                            func.json_extract(table1.c["metadata_json"], sql_constant("$.disposition")).is_(null()),
                        ),
                        func.json_extract(table1.c["metadata_json"], sql_constant("$.disposition")).in_(
                            [sql_constant("deferred"), sql_constant("linked_pending")]
                        ),
                    ),
                    query6.exists(),
                ),
            )
        )
        .order_by(table1.c["scope_type"].asc(), table1.c["scope_id"].asc())
        .limit(bindparam("p3"))
        .correlate_except(table1, table2, table3)
        .set_label_style(LABEL_STYLE_NONE)
    )
    if repos:
        query4 = query4.where(table2.c.full_name.in_(repos))
    return statements.read(conn, query4, {"p0": scope_type, "p1": scope_id, "p2": since, "p3": limit})


def round_exists(conn, kind, round_id, repo_id):
    session = tables[{"review_round": "review_sessions", "vote_round": "vote_sessions"}[kind]]
    repo = tables["repos"]
    return bool(
        statements.read(
            conn,
            select(sql_constant(1))
            .select_from(session.join(repo, repo.c.full_name == session.c.repo))
            .where(session.c.round_id == round_id, repo.c.repo_id == repo_id)
            .limit(1),
        )
    )
