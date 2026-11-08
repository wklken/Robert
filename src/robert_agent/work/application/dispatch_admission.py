"""Bind a confirmed Issue to one authenticated request and prepare classification."""

import json
from pathlib import Path

from robert_agent.foundation import values
from robert_agent.persistence import records
from robert_agent.persistence.queries import github_sources
from robert_agent.foundation.contracts.work import DispatchRequestStatus as Status
from robert_agent.adapters.github import discovery
from robert_agent.persistence.stores import dispatch_requests as stores
from robert_agent.work import dispatch_requests as facts, streams
from robert_agent.work.application import preparation, source_events
from robert_agent.workflows.routing import effective_route
from robert_agent.execution.application import dispatch


def prepare(conn, *, config, repo, repo_id, now, dispatch_queue, dispatch_budget, prompt_paths):
    admitted = set()
    for row in facts.pending(conn, config, repo_ids=[repo_id]):
        if min(dispatch_budget["remaining"], dispatch_budget.get("repo_remaining", {}).get(repo_id, 1)) <= 0:
            break
        issue = json.loads(row["issue_json"])
        source_key = discovery.source_key(repo["full_name"], "issue", issue["number"])
        source = records.first(github_sources.find_state_by_source_id(conn, values.github_source_id(source_key)))
        if issue.get("state", "open") != "open" or (source and source[0] == "closed"):
            stores.transition(
                conn,
                row["request_id"],
                Status.ISSUE_CREATED.value,
                Status.ATTENTION.value,
                now,
                error="The created Issue is closed; no execution was started.",
            )
            stores.notification(
                conn,
                row["request_id"],
                "source_closed",
                now,
                json.dumps(
                    {
                        "request_id": row["request_id"],
                        "repo_id": row["repo_id"],
                        "repository_scoped": True,
                        "dispatch_identity": json.loads(row["identity_json"]),
                        "issue_url": issue["html_url"],
                        "summary": "The Issue is closed; dispatch requires local inspection before execution.",
                    }
                ),
            )
            continue
        event = {
            "source_key": source_key,
            "workstream_id": source_key,
            "source_type": "issue",
            "number": issue["number"],
            "repo": repo["full_name"],
            "repo_full_name": repo["full_name"],
            "url": issue["html_url"],
            "event_fingerprint": f"operator-dispatch:{row['request_id']}",
            "event_type": "operator_dispatch",
            "actor_kind": "operator",
            "actor_login": None,
            "source_author_login": issue["user"]["login"],
            "dispatch_request_id": row["request_id"],
            "body": row["body"],
            "source_body": issue.get("body", row["body"]),
            "title": row["title"],
            "state": "open",
            "event_at": row["created_at"],
            "intent": "unclear",
            "body_observed": True,
            "body_authorization_complete": True,
        }
        event = facts.reauthorize(conn, config, event)
        if not event["drives_execution"]:
            continue
        source_events.insert_source_and_event(conn, repo_id, event, now)
        route = effective_route(config, repo, event, "classification-result")
        info = preparation.create_task_attempt_and_prompt(
            conn,
            Path(config["data_dir"]),
            config["db_path"],
            repo,
            repo_id,
            event,
            route,
            streams.plan_event(event),
            now,
            related_events=[event],
        )
        if not info:
            continue
        stores.transition(
            conn,
            row["request_id"],
            Status.ISSUE_CREATED.value,
            Status.ADMITTED.value,
            now,
            workstream_id=source_key,
            task_id=info["task_id"],
        )
        dispatch.queue_dispatch(dispatch_queue, info, None, dispatch_budget, repo_id=repo_id, repo=repo)
        prompt_paths.append(str(info["prompt_path"]))
        admitted.add(source_key)
    return admitted
