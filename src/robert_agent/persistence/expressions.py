"""Trusted schema constants must remain literals for SQLite expression indexes."""

from sqlalchemy import literal


def sql_constant(value):
    """Render fixed query syntax safely; request values use ordinary bound parameters."""
    return literal(value, literal_execute=True)
