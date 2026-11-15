"""Authenticated endpoint evidence supplied by a trusted TAPD client integration."""

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class TapdPublisherIdentity:
    """Attest the principal on the exact client performing the scoped write."""

    host: str
    workspace_id: str
    principal: str = field(repr=False)
    evidence_id: str = field(repr=False)
    observed_at: str
