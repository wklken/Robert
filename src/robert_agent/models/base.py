"""Shared metadata for the existing database schema."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Map the existing schema without changing storage encodings.

    Keep SQLite rowid tables' declared primary key nullability. Callers
    still supply the existing identifiers for text primary keys.
    """
