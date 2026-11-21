"""Expose controller-confirmed human ownership separately from untrusted source text."""

from robert_agent.context.prompt_contract import ContextItem


def context_item(owner):
    return ContextItem(
        "decision-owner",
        "Controller-confirmed decision owner for questions and PR followups",
        "Robert's accepted original request and verified workstream lineage",
        requirement="required",
        content={"decision_owner": owner, "decision_mention": "@" + (owner["login"] if owner else "wklken")},
    )
