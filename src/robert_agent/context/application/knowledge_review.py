"""Transactional human review of runtime knowledge candidates."""
from contextlib import closing
import sqlite3
from robert_agent.context import knowledge


def propose(db_path, **fields):
    with closing(sqlite3.connect(db_path)) as conn, conn:
        return knowledge.propose_candidates(conn, **fields)


def approve(db_path, **fields):
    with closing(sqlite3.connect(db_path)) as conn, conn:
        return knowledge.approve_candidate(conn, **fields)


def reject(db_path, **fields):
    with closing(sqlite3.connect(db_path)) as conn, conn:
        return knowledge.reject_candidate(conn, **fields)
