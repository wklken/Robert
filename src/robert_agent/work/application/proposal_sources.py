"""Read the current provider facts and bounded discussion for proposal analysis."""

import re
import subprocess

from robert_agent.adapters.github import issues
from robert_agent.adapters.tapd import client as tapd_client
from robert_agent.foundation import commands, tapd_identity, values
from robert_agent.security.text import safe_text
from robert_agent.work import proposals, tapd_policy


def _discussion(raw):
    recent = raw[-6:]
    return {
        "items": [safe_text(body, 600) for body in recent],
        "truncated": len(raw) > 6 or any(len(body) > 600 for body in recent),
        "digest": proposals.digest(raw),
    }


def _tapd_discussion(rows, mention_name, *, include_automatic=False):
    bodies = []
    for row in rows:
        parsed = tapd_policy.parse_comment(row.body, mention_name)
        result = (
            include_automatic
            and re.search(r"回执 [0-9a-f]{16}$", parsed.text)
            and not re.sub(r"^[^:：]+[:：]\s*", "", parsed.text, count=1).startswith("Robert 提案 ")
        )
        if not parsed.automatic or result:
            bodies.append(row.body)
    return _discussion(bodies)


def discussion(source, clients, tapd_settings, *, github_account=None, include_automatic=False):
    if source["provider"] == "github":
        kwargs = {"exclude_author": github_account}
        if include_automatic:
            kwargs["include_author_results"] = True
        return _discussion(issues.discussion(source["repository"], source["number"], **kwargs))
    client = clients[source["project_key"]]
    rows = client.ticket_comments(
        source["project_key"].split("/", 1)[1],
        source["kind"],
        source["external_id"],
        max_pages=10,
    )
    return _tapd_discussion(rows, tapd_settings["mention_name"], include_automatic=include_automatic)


def refresh(config, repo, source, *, clients=None, selected_repository=None):
    setting = config["proposer"]
    scheduled = setting.get("schedule", {}).get("enabled", False)
    if source["provider"] == "github":
        raw = issues.get(repo["full_name"], source["number"])
        current = _github_source(config, repo, raw)
        if current and source.get("discussion_digest"):
            comments = discussion(
                current,
                {},
                config["tapd"],
                github_account=repo["github_account"],
                include_automatic=scheduled,
            )
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
    current = _tapd_source(config, project, item, terminal)
    selected, repository, label = tapd_policy.select_repository(item.labels, project["label_rules"])
    if selected_repository is None and source.get("routing_unconfirmed"):
        selected_repository = source.get("repository")
    if selected_repository:
        matching = [rule for rule in project["label_rules"] if rule["repository"] == selected_repository]
        if not current or selected == "conflict" or selected == "selected" and repository != selected_repository:
            return None, None
        if selected == "missing":
            if len(matching) != 1:
                return None, None
            label = matching[0]["label"]
            current.update(
                repository=selected_repository,
                repository_label=label,
                routing_unconfirmed=True,
                source_key=tapd_identity.source_key(
                    project["host"], project["workspace_id"], item.kind, item.external_id, selected_repository
                ),
            )
            current.pop("repository_candidates", None)
    if not current:
        return None, None
    comments = client.ticket_comments(project["workspace_id"], source["kind"], source["external_id"], max_pages=10)
    non_automatic = _tapd_discussion(comments, config["tapd"]["mention_name"], include_automatic=scheduled)
    if source.get("discussion_digest"):
        current["discussion_digest"] = non_automatic["digest"]
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


_READ_ERRORS = (ValueError, OSError, subprocess.SubprocessError, tapd_client.TapdError)


def _github_source(config, repo, issue):
    setting = config["proposer"]
    scheduled = setting.get("schedule", {}).get("enabled", False)
    allowed = None if setting.get("github_all_registered") else setting["github_assignees"]
    source = proposals.github_source(
        repo,
        issue,
        allowed,
        in_progress_labels=setting.get("github_in_progress_labels", ("in progress", "in-progress"))
        if scheduled
        else (),
    )
    if source and scheduled and issues.has_open_pull_request(repo["full_name"], issue["number"]):
        return None
    if source and scheduled:
        source.update(status=issue["state"], labels=[label["name"] for label in issue.get("labels", [])])
    return source


def _tapd_source(config, project, item, terminal):
    setting = config["proposer"]
    scheduled = setting.get("schedule", {}).get("enabled", False)
    source = proposals.tapd_source(
        project,
        item,
        terminal,
        setting["tapd_assignees"],
        allow_unbound=scheduled,
        todo_states=setting.get("tapd_todo_states", {}).get(item.kind, ()) if scheduled else (),
    )
    if source and scheduled:
        source.update(status=item.status, labels=tapd_policy.split_labels(item.labels), priority=item.priority)
    return source


def discover(config, since, until, stats, clients):
    """Adapt each configured source into zero or more stable candidate observations."""
    setting = config["proposer"]
    repos = {repo["full_name"]: repo for repo in config["repos"]}
    names = repos if setting.get("github_all_registered") else setting["github_repositories"]
    for name in sorted(names):
        try:
            for issue in issues.list_recent(name, since=since, until=until):
                source = _github_source(config, repos[name], issue)
                if source:
                    yield source
        except commands.ControlDeadlineExceeded:
            raise
        except _READ_ERRORS:
            stats["failed_scopes"].append("github:" + name)
    for project in config["tapd"]["projects"]:
        workspace = project["workspace_id"]
        key = tapd_identity.project_key(project["host"], workspace)
        if key not in setting["tapd_projects"]:
            continue
        try:
            client = clients.get(key) or tapd_client.client_for(config["tapd"], config["data_dir"])
            clients[key] = client
        except commands.ControlDeadlineExceeded:
            raise
        except _READ_ERRORS:
            stats["failed_scopes"].append("tapd:" + key)
            continue
        terminal_cache = {}
        for kind in ("story", "bug"):
            try:
                for item in client.recent_items(workspace, kind, since=since, until=until):
                    terminal_key = (kind, item.workitem_type_id)
                    if terminal_key not in terminal_cache:
                        terminal_cache[terminal_key] = client.terminal_states(
                            workspace,
                            kind,
                            item.workitem_type_id if kind == "story" else None,
                        )
                    source = _tapd_source(config, project, item, terminal_cache[terminal_key])
                    if source:
                        yield source
            except commands.ControlDeadlineExceeded:
                raise
            except _READ_ERRORS:
                stats["failed_scopes"].append(f"tapd:{key}:{kind}")
