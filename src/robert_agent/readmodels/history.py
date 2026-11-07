"""Resolve compact step output without changing the run-detail response."""

from robert_agent.execution.journal import SUMMARY_REFERENCE


def step_output(step_key, output, summary):
    if step_key == "summarize" and output == SUMMARY_REFERENCE:
        return summary
    return output
