"""Read the current provider facts and bounded discussion for proposal analysis."""

from robert_agent.adapters.github import issues
from robert_agent.adapters.tapd import client as tapd_client
from robert_agent.foundation import values
from robert_agent.security.text import safe_text
from robert_agent.work import proposals, tapd_policy


def _discussion(raw):
    recent = raw[-6:]
    return {
        "items": [safe_text(body, 600) for body in recent],
        "truncated": len(raw) > 6 or any(len(body) > 600 for body in recent),
        "digest": proposals.digest(raw),
    }


def _tapd_discussion(rows, mention_name):
    return _discussion([row.body for row in rows if not tapd_policy.parse_comment(row.body, mention_name).automatic])


def discussion(source, clients, tapd_settings, *, github_account=None):
    if source["provider"] == "github":
        return _discussion(issues.discussion(source["repository"], source["number"], exclude_author=github_account))
    client = clients[source["project_key"]]
    rows = client.ticket_comments(
        source["project_key"].split("/", 1)[1],
        source["kind"],
        source["external_id"],
        max_pages=10,
    )
    return _tapd_discussion(rows, tapd_settings["mention_name"])


def refresh(config, repo, source, *, clients=None):
    setting = config["proposer"]
    if source["provider"] == "github":
        raw = issues.get(repo["full_name"], source["number"])
        current = proposals.github_source(repo, raw, setting["github_assignees"])
        if current and source.get("discussion_digest"):
            comments = discussion(current, {}, config["tapd"], github_account=repo["github_account"])
            current["discussion_digest"] = comments["digest"]
        return current, None
    project = tapd_policy.project_for(config["tapd"], source["project_key"])
    if not project:
        raise ValueError("TAPD project is no longer configured")
    client = (clients or {}).get(source["project_key"]) or tapd_client.client_for(config["tapd"], config["data_dir"])
    item = client.get_item(project["workspace_id"], source["kind"], source["external_id"])
    terminal = client.terminal_states(
        project["workspace_id"], source["kind"], item.workitem_type_id if source["kind"] == "story" else None
    )
    current = proposals.tapd_source(project, item, terminal, setting["tapd_assignees"])
    if not current:
        return None, None
    comments = client.ticket_comments(project["workspace_id"], source["kind"], source["external_id"], max_pages=10)
    non_automatic = _tapd_discussion(comments, config["tapd"]["mention_name"])
    if source.get("discussion_digest"):
        current["discussion_digest"] = non_automatic["digest"]
    selected, _repository, label = tapd_policy.select_repository(item.labels, project["label_rules"])
    if selected != "selected":
        return None, None
    snapshot = {
        "kind": source["kind"],
        "external_id": source["external_id"],
        "title": item.title,
        "status": item.status,
        "labels": tapd_policy.split_labels(item.labels),
        "ended": False,
        "repository": current["repository"],
        "repository_label": label,
        "description": safe_text(item.description, 6000),
        "description_truncated": len(item.description) > 6000,
        "comments": [
            {
                "comment_id": row.comment_id,
                "author": row.author,
                "created": row.created,
                "text": safe_text(row.body, 600),
                "truncated": len(row.body) > 600,
                "robert_reply": tapd_policy.parse_comment(row.body, config["tapd"]["mention_name"]).automatic,
            }
            for row in comments[-6:]
        ],
        "omitted_comments": max(0, len(comments) - 6),
        "observed_at": values.utc_now(),
    }
    return current, snapshot
