"""Pure wall-clock envelopes for a bounded pre-publication review pipeline."""

from collections.abc import Mapping


MAX_PIPELINE_QUEUE_SECONDS = 3600
MAX_HANDOFF_REPAIR_SECONDS = 120
MAX_HANDOFF_REPAIR_ATTEMPTS = 3


def _positive_seconds(value):
    if type(value) is not int or value < 1:
        raise ValueError("pipeline timeouts must be positive integer seconds")
    return value


def pipeline_timeout(plan: Mapping) -> int:
    """Bound implement, reviews, synthesis, address, verify, one repair and verify.

    Each stage uses its recorded worker timeout. Reviews overlap; the first
    reviewer owns synthesis and both verification passes. The implementer owns
    implementation and both address passes. Each of these three write passes
    reserves at most three artifact-only repairs, each capped at 120 seconds.
    Pool waiting shares one allowance, capped at an hour and at the plan timeout.
    This helper grants no authority: callers must obtain the plan from a durable
    pre-PR session before applying it to an attempt's global runtime limit.
    """
    if not isinstance(plan, Mapping):
        raise ValueError("pipeline plan must be a mapping")
    limit = _positive_seconds(plan.get("timeout_seconds"))
    implementer = plan.get("implementer")
    workers = plan.get("workers")
    if not isinstance(implementer, Mapping) or not isinstance(workers, list) or not workers:
        raise ValueError("pipeline plan requires an implementer and reviewers")
    implement = _positive_seconds(implementer.get("timeout_seconds"))
    reviews = []
    for worker in workers:
        if not isinstance(worker, Mapping):
            raise ValueError("pipeline reviewer must be a mapping")
        reviews.append(_positive_seconds(worker.get("timeout_seconds")))
    handoff_repairs = 3 * MAX_HANDOFF_REPAIR_ATTEMPTS * min(implement, MAX_HANDOFF_REPAIR_SECONDS)
    return 3 * implement + max(reviews) + 3 * reviews[0] + min(limit, MAX_PIPELINE_QUEUE_SECONDS) + handoff_repairs
