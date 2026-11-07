"""Shared queries operations for github_control_children."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import select, bindparam, and_, or_, func, LABEL_STYLE_NONE


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def list_children(conn):
    query1 = (
        select(
            tables["github_control_children"].c["child_id"],
            tables["github_control_children"].c["owner_id"],
            tables["github_control_children"].c["lane"],
            tables["github_control_children"].c["identity_json"],
            tables["github_control_children"].c["status"],
        )
        .select_from(tables["github_control_children"])
        .order_by(
            tables["github_control_children"].c["owner_id"].asc(),
            tables["github_control_children"].c["lane"].asc(),
            tables["github_control_children"].c["child_id"].asc(),
        )
        .correlate_except(tables["github_control_children"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {})


def find_for_child(conn, identifier):
    query1 = (
        select(
            tables["github_control_children"].c["child_id"],
            tables["github_control_children"].c["owner_id"],
            tables["github_control_children"].c["lane"],
            tables["github_control_children"].c["identity_json"],
            tables["github_control_children"].c["status"],
        )
        .select_from(tables["github_control_children"])
        .where((tables["github_control_children"].c["child_id"] == bindparam("p0")))
        .correlate_except(tables["github_control_children"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": identifier})


def find_status_and_identity_json_by_child_id(conn, identifier):
    query1 = (
        select(
            tables["github_control_children"].c["status"],
            tables["github_control_children"].c["identity_json"],
        )
        .select_from(tables["github_control_children"])
        .where((tables["github_control_children"].c["child_id"] == bindparam("p0")))
        .correlate_except(tables["github_control_children"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": identifier})


def find_for_previous_owner_children(conn, current_owner, scope_key):
    table1 = tables["github_control_children"].alias("c")
    table2 = tables["leases"].alias("l")
    query3 = (
        select(
            table1.c["child_id"],
            table1.c["identity_json"],
            table1.c["status"],
            table1.c["owner_id"],
            table1.c["lane"],
        )
        .distinct()
        .select_from(table1.join(table2, (table2.c["owner_id"] == table1.c["owner_id"]), isouter=False))
        .where(
            and_(
                table1.c["status"].in_([sql_constant("starting"), sql_constant("running")]),
                table1.c["owner_id"] != bindparam("p0"),
                table2.c["resource_type"] == sql_constant("daemon"),
                table2.c["resource_key"] == bindparam("p1"),
                or_(
                    table2.c["status"] != sql_constant("active"),
                    func.julianday(table2.c["expires_at"]) <= func.julianday(sql_constant("now")),
                ),
            )
        )
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": current_owner, "p1": scope_key})
