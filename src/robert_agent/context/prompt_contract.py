"""Values shared by the prompt producers and the central renderer."""

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ContextItem:
    id: str
    purpose: str
    source_ref: str
    requirement: str = field(default="optional", kw_only=True)
    delivery: str = field(default="inline", kw_only=True)
    content: object = field(default=None, kw_only=True)
    locator: str = field(default="", kw_only=True)
    revision: str = field(default="", kw_only=True)
    completeness: str = field(default="complete", kw_only=True)
    condition: str = field(default="", kw_only=True)
    reason: str = field(default="", kw_only=True)


@dataclass(frozen=True, slots=True)
class OutputContract:
    transport: str
    schema: str
    destination: str | tuple[str, ...]
    consumer: str
    example: object = field(default=None, kw_only=True)
    command: tuple[str, ...] = field(default=(), kw_only=True)


@dataclass(frozen=True, slots=True)
class PromptInput:
    stage: str
    identity: dict[str, object]
    output: OutputContract
    mode: str = field(default="fresh", kw_only=True)
    objective: str = field(default="", kw_only=True)
    authority: dict[str, object] = field(default_factory=dict, kw_only=True)
    context_items: tuple[ContextItem, ...] = field(default=(), kw_only=True)
    verification: dict[str, object] = field(default_factory=dict, kw_only=True)
    values: dict[str, str] = field(default_factory=dict, kw_only=True)
    guidance: tuple[str, ...] = field(default=(), kw_only=True)


@dataclass(frozen=True, slots=True)
class RenderedPrompt:
    text: str
    metadata: dict[str, object]
