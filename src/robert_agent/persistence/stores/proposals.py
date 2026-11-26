"""Writes for proposal observations and versions under caller transactions."""

from sqlalchemy import update
from sqlalchemy.dialects.sqlite import insert

from robert_agent.persistence import statements
from robert_agent.persistence.catalog import tables


def create(conn, values):
    return statements.write(conn, insert(tables["proposals"]).values(values))


def revise(conn, proposal_id, expected_version, **values):
    proposal = tables["proposals"]
    return statements.write(
        conn,
        update(proposal)
        .where(proposal.c.proposal_id == proposal_id, proposal.c.version == expected_version)
        .values(**values),
    )


def record_revision(conn, values):
    return statements.write(conn, insert(tables["proposal_revisions"]).values(values))


def record_decision(conn, values):
    return statements.write(conn, insert(tables["proposal_decisions"]).values(values))


def complete_scan(conn, slot, now):
    scan = tables["proposal_scans"]
    return statements.write(conn, insert(scan).values(slot=slot, completed_at=now).on_conflict_do_nothing())


def record_scan(conn, slot, now, result_json):
    scan = tables["proposal_scans"]
    return statements.write(
        conn,
        insert(scan)
        .values(slot=slot, completed_at=now, result_json=result_json)
        .on_conflict_do_update(index_elements=[scan.c.slot], set_={"completed_at": now, "result_json": result_json}),
    )
