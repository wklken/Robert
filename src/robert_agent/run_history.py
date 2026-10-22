"""Resolve compact step output without changing the run-detail response."""


SUMMARY_REFERENCE = {'summary_ref': 'agent_runs.summary_json'}


def step_output(step_key, output, summary):
    if step_key == 'summarize' and output == SUMMARY_REFERENCE:
        return summary
    return output
