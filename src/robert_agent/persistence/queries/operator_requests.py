from robert_agent.foundation.contracts.work import TaskKind, TaskLifecycle, WakeupStatus

from robert_agent.persistence.expressions import sql_constant
from sqlalchemy import select, and_, not_, null, func, LABEL_STYLE_NONE

from robert_agent.persistence.predicates import runnable_task

from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def pending_tasks(conn, repo_ids=(), task_id=None):
    table1 = tables["tasks"].alias("t")
    table2 = tables["workstreams"].alias("w")
    table3 = tables["repos"].alias("r")
    table4 = tables["wakeups"].alias("wu")
    table5 = tables["work_items"].alias("wi")
    table6 = tables["github_sources"].alias("gs")
    table8 = tables["attempts"].alias("a")
    query9 = (
        select(sql_constant(1))
        .select_from(table8)
        .where((table8.c["task_id"] == table1.c["task_id"]))
        .correlate_except(table8)
        .set_label_style(LABEL_STYLE_NONE)
    )
    query7 = (
        select(
            table1.c["task_id"],
            table1.c["parent_task_id"],
            table1.c["workstream_id"],
            table1.c["metadata_json"],
            table4.c["wakeup_id"],
            table4.c["created_at"].label("requested_at"),
            table5.c["work_item_id"],
            table5.c["origin_source_id"],
            table2.c["repo_id"],
            table3.c["full_name"],
            table6.c["number"],
            table6.c["source_type"],
        )
        .select_from(
            table1.join(
                table2,
                and_(
                    (table2.c["active_task_id"] == table1.c["task_id"]),
                    (table2.c["workstream_id"] == table1.c["workstream_id"]),
                ),
                isouter=False,
            )
            .join(table3, (table3.c["repo_id"] == table2.c["repo_id"]), isouter=False)
            .join(table4, (table4.c["task_id"] == table1.c["task_id"]), isouter=False)
            .join(
                table5,
                (table5.c["work_item_id"] == table4.c["work_item_id"]),
                isouter=False,
            )
            .join(
                table6,
                (table6.c["source_id"] == table2.c["primary_source_id"]),
                isouter=False,
            )
        )
        .where(
            and_(
                table5.c["origin_type"] == sql_constant("github"),
                table5.c["canceled_at"].is_(null()),
                table5.c["completed_at"].is_(null()),
                table1.c["lifecycle"] == sql_constant(TaskLifecycle.DETECTED.value),
                table1.c["task_kind"] == sql_constant(TaskKind.HUMAN_REQUEST.value),
                table4.c["reason"] == sql_constant("manual_operator_request"),
                table4.c["status"] == sql_constant(WakeupStatus.PENDING.value),
                not_(
                    func.json_extract(
                        table1.c["metadata_json"],
                        sql_constant("$.trigger_work_item_event_id"),
                    ).is_(null())
                ),
                not_(query9.exists()),
                sql_constant(1),
            )
        )
        .order_by(table1.c["created_at"].asc(), table1.c["task_id"].asc())
        .correlate_except(table1, table2, table3, table4, table5, table6)
        .set_label_style(LABEL_STYLE_NONE)
    )
    query7 = query7.where(runnable_task(table1))
    if repo_ids:
        query7 = query7.where(table2.c.repo_id.in_(repo_ids))
    if task_id:
        query7 = query7.where(table1.c.task_id == task_id)
    return statements.read(conn, query7, {})
