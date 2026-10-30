"""Construct the controller's internal audit input without submitting a worker result."""
from robert_agent.worker import result


def internal_result(*args, **kwargs):
    envelope = result.build_result(*args, **kwargs)
    return {key: value for key, value in envelope.items() if key != 'submission'} | envelope['submission']
