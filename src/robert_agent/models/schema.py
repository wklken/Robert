"""Schema migration markers."""

from sqlalchemy import Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from robert_agent.models.base import Base


class SchemaMigration(Base):
    __tablename__ = "schema_migrations"

    version: Mapped[int] = mapped_column(Integer, primary_key=True, nullable=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    checksum: Mapped[str] = mapped_column(Text)
    applied_at: Mapped[str] = mapped_column(Text)
