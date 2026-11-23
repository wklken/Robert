"""Resolve compact step output without changing the run-detail response."""

from robert_agent.execution.journal import SUMMARY_REFERENCE


def step_output(step_key, output, summary):
    if step_key == "summarize" and output == SUMMARY_REFERENCE:
        return summary
    if (
        step_key == "repo_pipelines"
        and isinstance(output, dict)
        and output.get("repo_summaries_ref") == "agent_runs.summary_json.repo_summaries"
    ):
        resolved = {key: value for key, value in output.items() if key != "repo_summaries_ref"}
        resolved["repo_summaries"] = summary.get("repo_summaries", [])
        return resolved
    return output


def diagnostic_detail(summary):
    """Explain omitted detail without exposing it as missing execution evidence."""
    if not isinstance(summary, dict):
        return {}
    detail = summary.get("diagnostics")
    if not isinstance(detail, dict):
        return {}
    if detail.get("detail_status") == "compacted_no_actions":
        return {**detail, "summary": "本轮没有执行或发布动作，步骤明细已压缩；运行摘要和业务事件仍保留。"}
    return detail
