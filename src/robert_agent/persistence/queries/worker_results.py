"""Shared queries operations for worker_results."""

from robert_agent.foundation.contracts.delivery import OutputType
from robert_agent.foundation.contracts.execution import AttemptStatus
from robert_agent.foundation.contracts.work import TaskLifecycle, WorkstreamLifecycle

from functools import lru_cache

from functools import cache

from robert_agent.persistence.expressions import sql_constant

from robert_agent.persistence.predicates import occupied_attempt, runnable_task

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
            tables["worker_results"].c["result_id"],
            tables["worker_results"].c["output_type"],
            tables["worker_results"].c["metadata_json"],
        )
        .select_from(tables["worker_results"])
        .order_by(tables["worker_results"].c["created_at"].desc())
        .limit(sql_constant(1))
        .correlate_except(tables["worker_results"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {})


def find_for_wait_for_result(conn):
    result, attempt = tables["worker_results"], tables["attempts"]
    stmt = (
        select(result.c.result_id, result.c.output_type, attempt.c.metadata_json)
        .join(attempt, attempt.c.attempt_id == result.c.attempt_id)
        .order_by(result.c.created_at.desc())
        .limit(1)
    )
    return statements.read(conn, stmt)


def find_metadata_json_by_result_id(conn, result_id):
    query1 = (
        select(tables["worker_results"].c["metadata_json"])
        .select_from(tables["worker_results"])
        .where((tables["worker_results"].c["result_id"] == bindparam("p0")))
        .correlate_except(tables["worker_results"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": result_id})


def find_for_audit_completed_results(conn, repo_id):
    table1 = tables["worker_results"].alias("wr")
    table2 = tables["tasks"].alias("t")
    table3 = tables["attempts"].alias("a")
    table4 = tables["route_decisions"].alias("rd")
    query6 = (
        select(tables["workstreams"].c["workstream_id"])
        .select_from(tables["workstreams"])
        .where((tables["workstreams"].c["repo_id"] == bindparam("p0")))
        .correlate_except(tables["workstreams"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    query5 = (
        select(
            table1.c["result_id"],
            table1.c["task_id"],
            table1.c["attempt_id"],
            table1.c["output_type"],
            table1.c["consumed_event_fingerprints_json"],
            table1.c["verification_json"],
            table1.c["handoff"],
            table1.c["metadata_json"],
            table2.c["workstream_id"],
            table4.c["allowed_github_actions_json"],
            table4.c["expected_output"],
            table4.c["required_skills_json"],
            table4.c["recommended_skills_json"],
            table4.c["route_id"],
            table2.c["task_kind"],
            table2.c["metadata_json"],
        )
        .select_from(
            table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=False)
            .join(
                table3,
                (table3.c["attempt_id"] == table1.c["attempt_id"]),
                isouter=False,
            )
            .join(table4, (table4.c["task_id"] == table1.c["task_id"]), isouter=False)
        )
        .where(
            and_(
                table2.c["workstream_id"].in_(query6),
                table2.c["lifecycle"].in_(
                    [sql_constant(TaskLifecycle.QUEUED.value), sql_constant(TaskLifecycle.RUNNING.value)]
                ),
                table3.c["status"] == sql_constant(AttemptStatus.COMPLETED.value),
                or_(
                    func.json_type(table3.c["metadata_json"], sql_constant("$.dispatch.pid")).is_not(
                        sql_constant("integer")
                    ),
                    not_(
                        func.json_extract(
                            table3.c["metadata_json"],
                            sql_constant("$.process_stopped_at"),
                        ).is_(null())
                    ),
                ),
            )
        )
        .order_by(table1.c["created_at"].asc(), table1.c["result_id"].asc())
        .correlate_except(table1, table2, table3, table4)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query5, {"p0": repo_id})


def find_for_trusted_action_scope(conn, result_id):
    table1 = tables["worker_results"].alias("wr")
    table2 = tables["tasks"].alias("t")
    table3 = tables["attempts"].alias("a")
    query4 = (
        select(
            table1.c["metadata_json"],
            table2.c["metadata_json"],
            table3.c["metadata_json"],
        )
        .select_from(
            table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=True).join(
                table3,
                and_(
                    (table3.c["attempt_id"] == table1.c["attempt_id"]),
                    (table3.c["task_id"] == table1.c["task_id"]),
                ),
                isouter=True,
            )
        )
        .where((table1.c["result_id"] == bindparam("p0")))
        .correlate_except(table1, table2, table3)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query4, {"p0": result_id})


def find_for_lookup(conn, attempt_id, task_id):
    query1 = (
        select(
            tables["worker_results"].c["result_id"],
            tables["worker_results"].c["metadata_json"],
        )
        .select_from(tables["worker_results"])
        .where(
            and_(
                (tables["worker_results"].c["attempt_id"] == bindparam("p0")),
                (tables["worker_results"].c["task_id"] == bindparam("p1")),
            )
        )
        .correlate_except(tables["worker_results"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": attempt_id, "p1": task_id})


def find_result_id_and_metadata_json_by_attempt_id(conn, attempt_id):
    query1 = (
        select(
            tables["worker_results"].c["result_id"],
            tables["worker_results"].c["metadata_json"],
        )
        .select_from(tables["worker_results"])
        .where((tables["worker_results"].c["attempt_id"] == bindparam("p0")))
        .correlate_except(tables["worker_results"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": attempt_id})


def find_result_id_by_attempt_id(conn, attempt_id):
    query1 = (
        select(tables["worker_results"].c["result_id"])
        .select_from(tables["worker_results"])
        .where((tables["worker_results"].c["attempt_id"] == bindparam("p0")))
        .correlate_except(tables["worker_results"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": attempt_id})


def find_task_id_by_result_id(conn, fact_id):
    query1 = (
        select(tables["worker_results"].c["task_id"])
        .select_from(tables["worker_results"])
        .where((tables["worker_results"].c["result_id"] == bindparam("p0")))
        .correlate_except(tables["worker_results"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": fact_id})


def find_for_build_dashboard_data(conn, history_limit):
    query1 = (
        select(
            tables["worker_results"].c["result_id"],
            tables["worker_results"].c["task_id"],
            tables["worker_results"].c["attempt_id"],
            tables["worker_results"].c["output_type"],
            tables["worker_results"].c["consumed_event_fingerprints_json"],
            tables["worker_results"].c["verification_json"],
            tables["worker_results"].c["handoff"],
            tables["worker_results"].c["created_at"],
            tables["worker_results"].c["metadata_json"],
        )
        .select_from(tables["worker_results"])
        .order_by(
            tables["worker_results"].c["created_at"].desc(),
            tables["worker_results"].c["result_id"].desc(),
        )
        .limit(bindparam("p0"))
        .correlate_except(tables["worker_results"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": history_limit})


def find_for_results_for_task(conn, task_id, limit):
    query1 = (
        select(
            tables["worker_results"].c["result_id"],
            tables["worker_results"].c["attempt_id"],
            tables["worker_results"].c["output_type"],
            tables["worker_results"].c["verification_json"],
            tables["worker_results"].c["handoff"],
            tables["worker_results"].c["created_at"],
            tables["worker_results"].c["metadata_json"],
        )
        .select_from(tables["worker_results"])
        .where((tables["worker_results"].c["task_id"] == bindparam("p0")))
        .order_by(
            tables["worker_results"].c["created_at"].desc(),
            tables["worker_results"].c["result_id"].desc(),
        )
        .limit(bindparam("p1"))
        .correlate_except(tables["worker_results"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id, "p1": limit})


def find_result_id_and_output_type_and_created_at_and_handoff_by_attempt_id(conn, attempt_id):
    query1 = (
        select(
            tables["worker_results"].c["result_id"],
            tables["worker_results"].c["output_type"],
            tables["worker_results"].c["created_at"],
            tables["worker_results"].c["handoff"],
        )
        .select_from(tables["worker_results"])
        .where((tables["worker_results"].c["attempt_id"] == bindparam("p0")))
        .correlate_except(tables["worker_results"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": attempt_id})


def find_for_summarize_database(conn):
    query1 = (
        select(func.count(literal_column("*")))
        .select_from(tables["worker_results"])
        .where(
            func.json_extract(
                tables["worker_results"].c["metadata_json"],
                sql_constant("$.audit.status"),
            ).in_([sql_constant("failed"), sql_constant("policy_violation")])
        )
        .correlate_except(tables["worker_results"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {})


def find_for_summarize_acceptance_metrics(conn):
    query1 = (
        select(tables["worker_results"].c["metadata_json"])
        .select_from(tables["worker_results"])
        .correlate_except(tables["worker_results"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {})


def find_for_result_counts(conn):
    query1 = (
        select(func.count(literal_column("*")))
        .select_from(tables["worker_results"])
        .where(
            (
                func.json_extract(
                    tables["worker_results"].c["metadata_json"],
                    sql_constant("$.audit.status"),
                )
                == sql_constant("accepted")
            )
        )
        .correlate_except(tables["worker_results"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {})


def find_for_verified(conn, task_id, target_id, value_3, value_4):
    query1 = (
        select(
            func.json_extract(
                tables["worker_results"].c["metadata_json"],
                sql_constant("$.audit.audited_at"),
            )
        )
        .select_from(tables["worker_results"])
        .where(
            and_(
                tables["worker_results"].c["task_id"] == bindparam("p0"),
                tables["worker_results"].c["result_id"] == bindparam("p1"),
                tables["worker_results"].c["attempt_id"] == bindparam("p2"),
                func.json_extract(
                    tables["worker_results"].c["metadata_json"],
                    sql_constant("$.audit.status"),
                )
                == sql_constant("accepted"),
                func.json_extract(
                    tables["worker_results"].c["metadata_json"],
                    sql_constant("$.submission_fingerprint"),
                ).is_(bindparam("p3")),
            )
        )
        .correlate_except(tables["worker_results"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id, "p1": target_id, "p2": value_3, "p3": value_4})


def find_for_unapplied_audit(conn, scope_id, target_id, value_3, value_4):
    query1 = (
        select(sql_constant(1))
        .select_from(tables["worker_results"])
        .where(
            and_(
                tables["worker_results"].c["task_id"] == bindparam("p0"),
                tables["worker_results"].c["result_id"] == bindparam("p1"),
                tables["worker_results"].c["attempt_id"] == bindparam("p2"),
                func.json_extract(tables["worker_results"].c["metadata_json"], sql_constant("$.audit")).is_(null()),
                func.json_extract(
                    tables["worker_results"].c["metadata_json"],
                    sql_constant("$.submission_fingerprint"),
                ).is_(bindparam("p3")),
            )
        )
        .correlate_except(tables["worker_results"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": scope_id, "p1": target_id, "p2": value_3, "p3": value_4})


def find_for_target(conn, task_id, target_id):
    query1 = (
        select(sql_constant(1))
        .select_from(tables["worker_results"])
        .where(
            and_(
                (tables["worker_results"].c["task_id"] == bindparam("p0")),
                (tables["worker_results"].c["attempt_id"] == bindparam("p1")),
            )
        )
        .correlate_except(tables["worker_results"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id, "p1": target_id})


def find_for_execute_command(conn, audit_result_id, task_id):
    query1 = (
        select(
            tables["worker_results"].c["attempt_id"],
            tables["worker_results"].c["metadata_json"],
        )
        .select_from(tables["worker_results"])
        .where(
            and_(
                (tables["worker_results"].c["result_id"] == bindparam("p0")),
                (tables["worker_results"].c["task_id"] == bindparam("p1")),
            )
        )
        .correlate_except(tables["worker_results"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": audit_result_id, "p1": task_id})


def find_for_finalize_published_results(conn, repo_id):
    table1 = tables["worker_results"].alias("wr")
    table2 = tables["tasks"].alias("t")
    table3 = tables["attempts"].alias("a")
    query5 = (
        select(tables["workstreams"].c["workstream_id"])
        .select_from(tables["workstreams"])
        .where((tables["workstreams"].c["repo_id"] == bindparam("p0")))
        .correlate_except(tables["workstreams"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    query4 = (
        select(
            table1.c["result_id"],
            table1.c["task_id"],
            table1.c["attempt_id"],
            table1.c["output_type"],
            table1.c["consumed_event_fingerprints_json"],
            table1.c["verification_json"],
            table1.c["handoff"],
            table1.c["metadata_json"],
            table2.c["workstream_id"],
        )
        .select_from(
            table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=False).join(
                table3,
                (table3.c["attempt_id"] == table1.c["attempt_id"]),
                isouter=False,
            )
        )
        .where(
            and_(
                table2.c["workstream_id"].in_(query5),
                table2.c["lifecycle"].in_(
                    [sql_constant(TaskLifecycle.QUEUED.value), sql_constant(TaskLifecycle.RUNNING.value)]
                ),
                table3.c["status"] == sql_constant(AttemptStatus.COMPLETED.value),
                or_(
                    func.json_type(table3.c["metadata_json"], sql_constant("$.dispatch.pid")).is_not(
                        sql_constant("integer")
                    ),
                    not_(
                        func.json_extract(
                            table3.c["metadata_json"],
                            sql_constant("$.process_stopped_at"),
                        ).is_(null())
                    ),
                ),
            )
        )
        .order_by(table1.c["created_at"].asc(), table1.c["result_id"].asc())
        .correlate_except(table1, table2, table3)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query4, {"p0": repo_id})


def find_for_resume_waiting_vote(conn, value_1):
    query1 = (
        select(tables["worker_results"].c["created_at"])
        .select_from(tables["worker_results"])
        .where(
            and_(
                (tables["worker_results"].c["task_id"] == bindparam("p0")),
                (tables["worker_results"].c["output_type"] == sql_constant(OutputType.WAITING_FOR_USER.value)),
            )
        )
        .order_by(tables["worker_results"].c["created_at"].desc())
        .limit(sql_constant(1))
        .correlate_except(tables["worker_results"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": value_1})


def find_for_finalize_superseded_result(conn, result_id):
    table1 = tables["worker_results"].alias("wr")
    table2 = tables["tasks"].alias("t")
    query3 = (
        select(table1.c["task_id"], table2.c["workstream_id"], table2.c["task_kind"])
        .select_from(table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=False))
        .where((table1.c["result_id"] == bindparam("p0")))
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": result_id})


def find_for_target_2(conn, task_id, attempt_id):
    query1 = (
        select(
            tables["worker_results"].c["result_id"],
            tables["worker_results"].c["metadata_json"],
        )
        .select_from(tables["worker_results"])
        .where(
            and_(
                (tables["worker_results"].c["task_id"] == bindparam("p0")),
                (tables["worker_results"].c["attempt_id"] == bindparam("p1")),
            )
        )
        .correlate_except(tables["worker_results"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id, "p1": attempt_id})


def find_for_verified_2(conn, task_id, target_id, value_3):
    table1 = tables["worker_results"].alias("wr")
    table3 = tables["wakeups"].alias("wu")
    query4 = (
        select(sql_constant(1))
        .select_from(table3)
        .where(
            and_(
                table3.c["result_id"] == table1.c["result_id"],
                table3.c["attempt_id"] == table1.c["attempt_id"],
                table3.c["reason"] == sql_constant("worker_result_ready"),
            )
        )
        .correlate_except(table3)
        .set_label_style(LABEL_STYLE_NONE)
    )
    query2 = (
        select(table1.c["result_id"], table1.c["created_at"])
        .select_from(table1)
        .where(
            and_(
                table1.c["task_id"] == bindparam("p0"),
                table1.c["attempt_id"] == bindparam("p1"),
                func.json_extract(table1.c["metadata_json"], sql_constant("$.submission_fingerprint"))
                == bindparam("p2"),
                or_(
                    func.json_extract(table1.c["metadata_json"], sql_constant("$.audit.status"))
                    == sql_constant("accepted"),
                    query4.exists(),
                ),
            )
        )
        .correlate_except(table1)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query2, {"p0": task_id, "p1": target_id, "p2": value_3})


@lru_cache(maxsize=2)
def _has_local_work_filtered_statement(scoped):
    table1 = tables["worker_results"].alias("wr")
    table2 = tables["attempts"].alias("a")
    table3 = tables["tasks"].alias("t")
    table4 = tables["workstreams"].alias("w")
    query5 = (
        select(
            table1.c["result_id"],
            table1.c["task_id"],
            func.json_extract(
                table1.c["metadata_json"],
                sql_constant("$.audit_observation.attempt_count"),
            ),
        )
        .select_from(
            table1.join(
                table2,
                (table2.c["attempt_id"] == table1.c["attempt_id"]),
                isouter=False,
            )
            .join(table3, (table3.c["task_id"] == table1.c["task_id"]), isouter=False)
            .join(
                table4,
                (table4.c["workstream_id"] == table3.c["workstream_id"]),
                isouter=False,
            )
        )
        .where(
            and_(
                table3.c["lifecycle"].in_(
                    [sql_constant(TaskLifecycle.QUEUED.value), sql_constant(TaskLifecycle.RUNNING.value)]
                ),
                table2.c["status"] == sql_constant(AttemptStatus.COMPLETED.value),
                not_(func.coalesce(occupied_attempt(table2), sql_constant(0))),
                func.json_extract(table1.c["metadata_json"], sql_constant("$.audit")).is_(null()),
                runnable_task(table3),
                or_(
                    func.json_extract(
                        table1.c["metadata_json"],
                        sql_constant("$.audit_observation.next_retry_at"),
                    ).is_(null()),
                    func.julianday(
                        func.json_extract(
                            table1.c["metadata_json"],
                            sql_constant("$.audit_observation.next_retry_at"),
                        )
                    )
                    <= func.julianday(bindparam("p0")),
                ),
            )
        )
        .correlate_except(table1, table2, table3, table4)
        .set_label_style(LABEL_STYLE_NONE)
    )
    if scoped:
        query5 = query5.where(table4.c.repo_id.in_(bindparam("items", expanding=True)))
    return query5


def has_local_work_filtered(conn, items, value_1):
    return statements.read(conn, _has_local_work_filtered_statement(bool(items)), {"p0": value_1, "items": items})


@lru_cache(maxsize=2)
def _published_result_needs_finalization_filtered_statement(scoped):
    table1 = tables["worker_results"].alias("wr")
    table2 = tables["tasks"].alias("t")
    table3 = tables["attempts"].alias("a")
    table4 = tables["workstreams"].alias("w")
    query5 = (
        select(table1.c["result_id"])
        .select_from(
            table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=False)
            .join(
                table3,
                (table3.c["attempt_id"] == table1.c["attempt_id"]),
                isouter=False,
            )
            .join(
                table4,
                (table4.c["workstream_id"] == table2.c["workstream_id"]),
                isouter=False,
            )
        )
        .where(
            and_(
                table2.c["lifecycle"].in_(
                    [sql_constant(TaskLifecycle.QUEUED.value), sql_constant(TaskLifecycle.RUNNING.value)]
                ),
                table3.c["status"] == sql_constant(AttemptStatus.COMPLETED.value),
                or_(
                    func.json_type(table3.c["metadata_json"], sql_constant("$.dispatch.pid")).is_not(
                        sql_constant("integer")
                    ),
                    not_(
                        func.json_extract(
                            table3.c["metadata_json"],
                            sql_constant("$.process_stopped_at"),
                        ).is_(null())
                    ),
                ),
                func.json_extract(table1.c["metadata_json"], sql_constant("$.audit.status"))
                == sql_constant("accepted"),
                runnable_task(table2),
            )
        )
        .correlate_except(table1, table2, table3, table4)
        .set_label_style(LABEL_STYLE_NONE)
    )
    if scoped:
        query5 = query5.where(table4.c.repo_id.in_(bindparam("items", expanding=True)))
    return query5


def published_result_needs_finalization_filtered(conn, items):
    return statements.read(conn, _published_result_needs_finalization_filtered_statement(bool(items)), {"items": items})


@cache
def _orphaned_classification_rows_filtered_statement():
    table1 = tables["worker_results"].alias("wr")
    table2 = tables["tasks"].alias("t")
    table3 = tables["workstreams"].alias("w")
    table5 = tables["tasks"].alias("child")
    query6 = (
        select(sql_constant(1))
        .select_from(table5)
        .where((table5.c["parent_task_id"] == table2.c["task_id"]))
        .correlate_except(table5)
        .set_label_style(LABEL_STYLE_NONE)
    )
    query4 = (
        select(
            table1.c["result_id"],
            table1.c["task_id"],
            table1.c["attempt_id"],
            table1.c["output_type"],
            table1.c["consumed_event_fingerprints_json"],
            table1.c["verification_json"],
            table1.c["handoff"],
            table1.c["metadata_json"],
            table2.c["workstream_id"],
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
                table3.c["repo_id"].in_(bindparam("items", expanding=True)),
                table3.c["lifecycle"] == sql_constant(WorkstreamLifecycle.ACTIVE.value),
                table3.c["active_task_id"] == table2.c["task_id"],
                table2.c["lifecycle"] == sql_constant(TaskLifecycle.COMPLETED.value),
                table1.c["output_type"] == sql_constant(OutputType.CLASSIFICATION_RESULT.value),
                func.coalesce(
                    func.json_extract(
                        table2.c["metadata_json"],
                        sql_constant("$.classification_handoff.retryable"),
                    ),
                    sql_constant(1),
                )
                != sql_constant(0),
                or_(
                    func.json_extract(
                        table2.c["metadata_json"],
                        sql_constant("$.classification_handoff.next_retry_at"),
                    ).is_(null()),
                    func.julianday(
                        func.json_extract(
                            table2.c["metadata_json"],
                            sql_constant("$.classification_handoff.next_retry_at"),
                        )
                    )
                    <= func.julianday(bindparam("p0")),
                ),
                not_(query6.exists()),
            )
        )
        .order_by(table1.c["created_at"].asc(), table1.c["result_id"].asc())
        .correlate_except(table1, table2, table3)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return query4


def orphaned_classification_rows_filtered(conn, items, value_1):
    return statements.read(conn, _orphaned_classification_rows_filtered_statement(), {"p0": value_1, "items": items})
