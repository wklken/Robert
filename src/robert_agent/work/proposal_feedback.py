"""Build operator messages and source-comment requests from proposal facts."""

from robert_agent.foundation.values import repository_id
from robert_agent.persistence.stores import proposal_feedback as store
from robert_agent.work.proposals import encoded


def can_comment(setting, source):
    if not setting.get("enabled") or not setting.get("publish_comments"):
        return False
    if source["provider"] == "github":
        return source["repository"] in setting["github_repositories"]
    return source["project_key"] in setting["tapd_projects"]


def record_created(conn, setting, proposal_id, source, now):
    path = f"/proposals?id={proposal_id}"
    store.record(
        conn,
        f"proposal-operator:{proposal_id}",
        "proposal_created",
        now,
        encoded(
            {
                "repo_id": repository_id(source["repository"]),
                "repository_scoped": True,
                "summary": f"Robert 有一条新提案 {proposal_id}。查看 {path}",
                "issue_url": source["url"],
            }
        ),
        operator=True,
    )
    if can_comment(setting, source):
        record_comment(conn, f"proposal-comment:{proposal_id}:created", "created", proposal_id, 1, source, now)


def record_comment(conn, identifier, kind, proposal_id, revision, source, now, pr_url=None):
    return store.record(
        conn,
        identifier,
        kind,
        now,
        encoded(
            {
                "proposal_id": proposal_id,
                "revision": revision,
                "provider": source["provider"],
                "repository": source["repository"],
                "target_url": source["url"],
                "number": source.get("number"),
                "status_before": source.get("status"),
                "owners_before": source.get("owners"),
                "repository_label": source.get("repository_label"),
                "routing_unconfirmed": source.get("routing_unconfirmed", False),
                "project_key": source.get("project_key"),
                "pr_url": pr_url,
            }
        ),
    )
