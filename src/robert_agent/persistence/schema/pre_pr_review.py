"""Add the persisted stages for unpublished PR candidates."""

from sqlalchemy.dialects.sqlite import dialect
from sqlalchemy.schema import CreateIndex, CreateTable

from robert_agent.models.rounds import PrePrReviewRun, PrePrReviewSession, PrePrReviewSnapshot
from robert_agent.persistence.dialects.sqlite import execute_control


def initialize(conn):
    sqlite = dialect()
    for model in (PrePrReviewSession, PrePrReviewSnapshot, PrePrReviewRun):
        table = model.__table__
        execute_control(conn, str(CreateTable(table, if_not_exists=True).compile(dialect=sqlite)))
        for index in table.indexes:
            execute_control(conn, str(CreateIndex(index, if_not_exists=True).compile(dialect=sqlite)))
