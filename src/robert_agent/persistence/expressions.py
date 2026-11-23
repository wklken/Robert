"""Trusted schema constants must remain literals for SQLite expression indexes."""

from sqlalchemy import case, func, literal


def sql_constant(value):
    """Render fixed query syntax safely; request values use ordinary bound parameters."""
    return literal(value, literal_execute=True)


def valid_json_or_empty(expression):
    """Keep read projections available when historical JSON text is malformed."""
    return case((func.json_valid(expression), expression), else_=sql_constant("{}"))
