"""Describe the inline facts and output contracts of supervision model calls."""

from robert_agent.context.prompt_contract import ContextItem, OutputContract, PromptInput
from robert_agent.foundation.contracts.supervision import DecisionAction


_OBSERVATION_LIMIT = "bounded observation projection with source selection, redaction and size limits"


def _bundle_identity(packet, scope_field):
    fields = ("bundle_id", "input_generation", scope_field)
    if missing := set(fields) - packet.keys():
        raise ValueError(f"missing supervision bundle identity: {', '.join(sorted(missing))}")
    return {field: f"$.{field}" for field in fields}


def _inline_context(packet, fields, truncated=None):
    if missing := fields.keys() - packet.keys():
        raise ValueError(f"missing supervision bundle context: {', '.join(sorted(missing))}")
    truncated = truncated or {}
    return tuple(
        ContextItem(
            field,
            purpose,
            "stdin JSON bundle",
            requirement="required",
            locator=f"$.{field}",
            revision="$.input_generation",
            completeness="truncated" if field in truncated else "complete",
            reason=truncated.get(field, ""),
        )
        for field, purpose in fields.items()
    )


def diagnosis_input(packet, *, classes, recommendations, max_progress_chars):
    return PromptInput(
        "supervision.diagnose",
        _bundle_identity(packet, "scope"),
        OutputContract(
            "stdout-json",
            "diagnosis",
            "stdout",
            "Robert diagnosis validator",
            example={
                "bundle_id": "<copy from bundle>",
                "bundle_sha256": "<copy from bundle>",
                "diagnosis_class": "unknown",
                "evidence_ids": ["<supplied evidence ID>"],
                "recommended_action": "wait",
                "expected_progress": "<observable next signal or remaining uncertainty>",
            },
        ),
        authority={"tools": []},
        context_items=_inline_context(
            packet,
            {"evidence": "Scoped observations and task facts", "missing": "Explicitly unavailable observations"},
            {"evidence": _OBSERVATION_LIMIT},
        ),
        values={
            "diagnosis_classes": ", ".join(f"`{value}`" for value in sorted(classes)),
            "recommendations": ", ".join(f"`{value}`" for value in sorted(recommendations)),
            "max_progress_chars": str(max_progress_chars),
        },
    )


def decision_input(
    packet, *, min_recheck, max_recheck, max_text_chars, compacted_fields, candidates_omitted, candidates_sanitized
):
    truncated = {"evidence": _OBSERVATION_LIMIT}
    if candidates_sanitized:
        truncated["candidates"] = "redacted or omitted candidate facts"
    truncated.update({field: "shortened for the bundle byte budget" for field in compacted_fields})
    if candidates_omitted:
        limit = f"{candidates_omitted} candidates omitted by candidate limit"
        truncated["candidates"] = ", ".join(filter(None, (truncated.get("candidates"), limit)))
    return PromptInput(
        "supervision.decide",
        _bundle_identity(packet, "root_id"),
        OutputContract(
            "stdout-json",
            "decision",
            "stdout",
            "Robert decision validator and domain consumers",
            example={
                "bundle_id": "<copy from bundle>",
                "bundle_sha256": "<copy from bundle>",
                "action": DecisionAction.WAIT.value,
                "candidate_id": None,
                "reason": "<evidence-based reason>",
                "evidence_ids": ["<supplied evidence ID>"],
                "expected_progress": "<observable next signal>",
                "recheck_seconds": min_recheck,
                "message": "",
            },
        ),
        authority={"tools": []},
        context_items=_inline_context(
            packet,
            {
                "evidence": "Goal, current facts, decision history, receipts and operator reply",
                "candidates": "Offered actions with candidate identities and scoped facts",
                "allowed_actions": "Offered actions that require no candidate",
            },
            truncated,
        ),
        values={
            "min_recheck": str(min_recheck),
            "max_recheck": str(max_recheck),
            "max_text_chars": str(max_text_chars),
        },
    )
