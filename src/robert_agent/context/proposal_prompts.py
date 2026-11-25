"""Input contract for a read-only proposal assessment."""

from robert_agent.context.prompt_contract import ContextItem, OutputContract, PromptInput
from robert_agent.context.prompts import render_prompt


def assessment(source, commit):
    return render_prompt(
        PromptInput(
            "proposer.evaluate",
            {"repository": source["repository"], "commit": commit},
            OutputContract(
                "stdout-json",
                "proposal_assessment",
                "stdout",
                "Robert proposal evidence validator",
                example={
                    "decision": "propose",
                    "kind": "implementation",
                    "summary": "<proposed outcome>",
                    "priority": "medium",
                    "recommendation": "high",
                    "recommendation_reason": "<why it belongs here today>",
                    "scope": "<bounded implementation or investigation>",
                    "exclusions": "<out of scope>",
                    "verification": "<checks and expected results>",
                    "risks": "<remaining risk>",
                    "evidence": [{"id": "code-1", "line": 1, "reason": "<fact this line supports>"}],
                },
            ),
            authority={"tools": []},
            context_items=(
                ContextItem(
                    "ticket",
                    "Original title and description",
                    source["provider"],
                    requirement="required",
                    locator="$.ticket",
                ),
                ContextItem("discussion", "Recent relevant discussion", source["provider"], locator="$.discussion"),
                ContextItem(
                    "code",
                    "Code excerpts pinned by Robert",
                    "repository commit",
                    requirement="required",
                    revision=commit,
                    locator="$.code",
                ),
            ),
        )
    )


def routing(source):
    return render_prompt(
        PromptInput(
            "proposer.route",
            {"provider": source["provider"], "repository_labels": source["repository_candidates"]},
            OutputContract(
                "stdout-json",
                "proposal_route",
                "stdout",
                "Robert proposal routing validator",
                example={"label": None, "reason": "<reason for selecting a listed label or abstaining>"},
            ),
            authority={"tools": []},
            context_items=(
                ContextItem("ticket", "Title and description", source["provider"], locator="$.ticket"),
                ContextItem("discussion", "Recent discussion", source["provider"], locator="$.discussion"),
            ),
        )
    )
