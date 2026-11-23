"""Mapped application tables and SQLite-owned catalog/search projections."""

from types import MappingProxyType

from sqlalchemy import Table, column, table
from sqlalchemy.sql.selectable import TableClause

from robert_agent.models import Base


tables: MappingProxyType[str, Table | TableClause] = MappingProxyType(
    {
        **Base.metadata.tables,
        "sqlite_master": table(
            "sqlite_master",
            *map(
                column,
                (
                    "type",
                    "name",
                    "tbl_name",
                    "rootpage",
                    "sql",
                ),
            ),
        ),
        "project_memory_fts": table(
            "project_memory_fts",
            *map(
                column,
                (
                    "memory_id",
                    "repo_id",
                    "title",
                    "short_summary",
                    "long_summary",
                    "paths",
                    "symbols",
                    "keywords",
                ),
            ),
        ),
    }
)
