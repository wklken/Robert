"""Shared queries operations for execution_pending_facts."""

from robert_agent.foundation.contracts.execution import BindingStatus
from robert_agent.foundation.contracts.work import RemediationStatus

from functools import lru_cache

from robert_agent.persistence.expressions import sql_constant

from robert_agent.persistence.predicates import runnable_task

from sqlalchemy import (
    select,
    bindparam,
    and_,
    or_,
    null,
    func,
    LABEL_STYLE_NONE,
)


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def find_for_pending_facts(conn, repo_id, now):
    query1 = (
        select(tables["execution_pending_facts"])
        .select_from(tables["execution_pending_facts"])
        .where(
            and_(
                tables["execution_pending_facts"].c["repo_id"] == bindparam("p0"),
                tables["execution_pending_facts"].c["consumed_at"].is_(null()),
                func.julianday(tables["execution_pending_facts"].c["not_before_at"]) <= func.julianday(bindparam("p1")),
            )
        )
        .order_by(
            tables["execution_pending_facts"].c["not_before_at"].asc(),
            tables["execution_pending_facts"].c["source_signal_id"].asc(),
        )
        .correlate_except(tables["execution_pending_facts"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": repo_id, "p1": now})


def find_record_by_source_signal_id(conn, signal_id):
    query1 = (
        select(tables["execution_pending_facts"])
        .select_from(tables["execution_pending_facts"])
        .where((tables["execution_pending_facts"].c["source_signal_id"] == bindparam("p0")))
        .correlate_except(tables["execution_pending_facts"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": signal_id})


@lru_cache(maxsize=2)
def _has_local_work_filtered_statement(scoped):
    table1 = tables["execution_pending_facts"].alias("f")
    table2 = tables["execution_subjects"].alias("c")
    table3 = tables["tasks"].alias("t")
    table7 = tables["pr_remediation_episodes"].alias("e")
    query8 = (
        select(sql_constant(1))
        .select_from(table7)
        .where(
            and_(
                (table7.c["episode_id"] == table1.c["subject_id"]),
                (
                    or_(
                        table7.c["status"].in_(
                            [
                                sql_constant(RemediationStatus.RESOLVED.value),
                                sql_constant(RemediationStatus.EXHAUSTED.value),
                                sql_constant(RemediationStatus.SUPERSEDED.value),
                                sql_constant(RemediationStatus.CANCELED.value),
                            ]
                        ),
                        (
                            table1.c["dedupe_key"]
                            != table1.c["fact_id"].concat(sql_constant(":")).concat(table7.c["attempt_count"])
                        ),
                    )
                ),
            )
        )
        .correlate_except(table7)
        .set_label_style(LABEL_STYLE_NONE)
    )
    query4 = (
        select(sql_constant(1))
        .select_from(
            table1.join(
                table2,
                and_(
                    (table2.c["subject_type"] == table1.c["subject_type"]),
                    (table2.c["subject_id"] == table1.c["subject_id"]),
                ),
                isouter=False,
            ).join(table3, (table3.c["task_id"] == table1.c["task_id"]), isouter=False)
        )
        .where(
            and_(
                table1.c["consumed_at"].is_(null()),
                table2.c["binding_status"] == sql_constant(BindingStatus.ACTIVE.value),
                runnable_task(table3),
                func.julianday(table1.c["not_before_at"]) <= func.julianday(bindparam("p0")),
                or_(
                    or_(
                        table1.c["subject_type"] == sql_constant("task"),
                        table1.c["topic"] != sql_constant("remediation_decision"),
                    ),
                    query8.exists(),
                ),
            )
        )
        .limit(sql_constant(1))
        .correlate_except(table1, table2, table3)
        .set_label_style(LABEL_STYLE_NONE)
    )
    if scoped:
        query4 = query4.where(table1.c.repo_id.in_(bindparam("items", expanding=True)))
    return query4


def has_local_work_filtered(conn, items, value_1):
    return statements.read(conn, _has_local_work_filtered_statement(bool(items)), {"p0": value_1, "items": items})
