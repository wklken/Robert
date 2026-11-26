"""delivery queries responsibilities."""

from robert_agent.persistence.queries import github_actions as db_read_github_actions

from robert_agent.delivery import retry as publication_retry

from robert_agent.delivery import values as publication_values


def ready_actions(conn, limit, repo_id=None, task_ids=None, action_ids=None):
    return [
        dict(row)
        for row in db_read_github_actions.find_for_ready_actions(
            conn, publication_values.now(), limit, repo_id, task_ids, action_ids
        )
    ]


def retry_budget_exhausted(prior, now):
    return publication_retry.retry_budget_exhausted(prior, now)
