"""Transactional human review of runtime knowledge candidates."""

from robert_agent.persistence import database as db_database
from contextlib import closing
from robert_agent.context import knowledge


def propose(db_path, **fields):
    with closing(db_database.connect(db_path)) as conn, conn:
        return knowledge.propose_candidates(conn, **fields)


def approve(db_path, **fields):
    with closing(db_database.connect(db_path)) as conn, conn:
        return knowledge.approve_candidate(conn, **fields)


def reject(db_path, **fields):
    with closing(db_database.connect(db_path)) as conn, conn:
        return knowledge.reject_candidate(conn, **fields)
