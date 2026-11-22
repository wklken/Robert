"""Shared shape for Agent-proposed options in a human question."""

import re

PROMPT_RESOURCE = "guidance/operator-question.md"
PROMPT_MARKER = "<!-- operator-question-choices-v1 -->"
REQUIRED_FLAG = "question_choices_required"


def validation_error(choices, *, required=False):
    if not isinstance(choices, list) or len(choices) > 5:
        return "operator_question.choices must contain at most five options"
    structured = required or any(isinstance(choice, dict) and "tradeoff" in choice for choice in choices)
    if structured and len(choices) < 2:
        return "operator_question.choices requires two to five viable options"
    ids = set()
    labels = set()
    for choice in choices:
        if not isinstance(choice, dict) or set(choice) - {"id", "label", "tradeoff"}:
            return "operator_question.choices contains unsupported fields"
        identifier = choice.get("id")
        label = choice.get("label")
        tradeoff = choice.get("tradeoff")
        if not isinstance(identifier, str) or not identifier.strip():
            return "operator_question choice id is required"
        if not isinstance(label, str) or not label.strip():
            return "operator_question choice label is required"
        if structured:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,16}", identifier) or identifier in ids:
                return "operator_question choice ids must be distinct short identifiers"
            if len(label) > 80 or "\n" in label or "\r" in label or label.strip().casefold() in labels:
                return "operator_question choice labels must be distinct single lines of up to 80 characters"
            if not isinstance(tradeoff, str) or not tradeoff.strip() or len(tradeoff) > 240:
                return "operator_question choice tradeoff must explain the option in up to 240 characters"
        ids.add(identifier)
        labels.add(label.strip().casefold())
    return None


def option_lines(choices, *, recommended):
    return [
        f"{number}. {choice['label']}{recommended if number == 1 else ''}"
        + (f" — {choice['tradeoff']}" if choice.get("tradeoff") else "")
        for number, choice in enumerate(choices, 1)
    ]
