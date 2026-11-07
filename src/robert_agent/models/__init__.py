"""Declarative mappings of the installed database schema."""

from robert_agent.models.base import Base as Base

# Explicit imports register each table with the shared metadata.
import robert_agent.models.context as context
import robert_agent.models.delivery as delivery
import robert_agent.models.discovery as discovery
import robert_agent.models.execution as execution
import robert_agent.models.rounds as rounds
import robert_agent.models.runtime as runtime
import robert_agent.models.schema as schema
import robert_agent.models.supervision as supervision
import robert_agent.models.work as work

__all__ = [
    "Base",
    "context",
    "delivery",
    "discovery",
    "execution",
    "rounds",
    "runtime",
    "schema",
    "supervision",
    "work",
]
