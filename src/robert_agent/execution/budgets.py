"""Pure wall-clock envelopes for a bounded pre-publication review pipeline."""

from collections.abc import Mapping

from robert_agent.foundation.contracts.execution import ReviewReportMode


MAX_PIPELINE_QUEUE_SECONDS = 3600
MAX_HANDOFF_REPAIR_SECONDS = 120
MAX_HANDOFF_REPAIR_ATTEMPTS = 3


def _positive_seconds(value):
    if type(value) is not int or value < 1:
        raise ValueError("pipeline timeouts must be positive integer seconds")
    return value


def pipeline_timeout(plan: Mapping) -> int:
    """Bound the stages selected by the durable pre-publication review plan.

    Each stage uses its recorded worker timeout. Reviews overlap; the first
    reviewer owns synthesis for historical report plans. A single-round plan
    reserves implementation and one address pass, with no final model verification.
    Historical plans retain two address and two verification passes. Each write
    pass reserves at most three artifact-only repairs, capped at 120 seconds.
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
    policy = plan.get("review_policy")
    if policy not in (None, "single_review_single_repair"):
        raise ValueError("unsupported review policy")
    write_passes = 2 if policy else 3
    mode = ReviewReportMode(plan.get("review_report_mode", ReviewReportMode.MODEL_SYNTHESIS))
    synthesis_passes = int(mode == ReviewReportMode.MODEL_SYNTHESIS)
    reviewer_passes = synthesis_passes + (0 if policy else 2)
    handoff_repairs = write_passes * MAX_HANDOFF_REPAIR_ATTEMPTS * min(implement, MAX_HANDOFF_REPAIR_SECONDS)
    return (
        write_passes * implement
        + max(reviews)
        + reviewer_passes * reviews[0]
        + min(limit, MAX_PIPELINE_QUEUE_SECONDS)
        + handoff_repairs
    )
