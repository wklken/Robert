"""delivery queries responsibilities."""

from robert_agent.persistence.queries import github_actions as db_read_github_actions

from robert_agent.execution import retry as retry_policy

from robert_agent.delivery import values as publication_values


def ready_actions(conn, limit, repo_id=None, task_ids=None, action_ids=None):
    return [
        dict(row)
        for row in db_read_github_actions.find_for_ready_actions(
            conn, publication_values.now(), limit, repo_id, task_ids, action_ids
        )
    ]


def retry_budget_exhausted(prior, now):
    first = prior.get("first_failed_at") or now
    elapsed = (retry_policy.timestamp(now) - retry_policy.timestamp(first)).total_seconds()
    return (
        int(prior.get("attempt_count", 0)) >= retry_policy.MAX_ATTEMPTS or elapsed >= retry_policy.MAX_ELAPSED_SECONDS
    )
