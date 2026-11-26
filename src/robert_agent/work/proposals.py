"""Pure proposal candidate facts and assessment validation."""

import hashlib
import json

from robert_agent.adapters.github.discovery import source_key as github_key
from robert_agent.foundation import tapd_identity
from robert_agent.security.text import safe_text
from robert_agent.work import tapd_policy


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def github_source(repo, issue, allowed, *, in_progress_labels=()):
    repository = repo["full_name"]
    owners = tuple(assignee["login"].casefold() for assignee in issue["assignees"])
    labels = issue.get("labels", [])
    if in_progress_labels and (
        not isinstance(labels, list)
        or any(not isinstance(label, dict) or not isinstance(label.get("name"), str) for label in labels)
    ):
        raise ValueError("GitHub returned incomplete Issue labels")
    started = bool(in_progress_labels) and bool(
        {label["name"].casefold() for label in labels} & {label.casefold() for label in in_progress_labels}
    )
    if issue["state"] != "open" or started or allowed is not None and any(owner not in allowed for owner in owners):
        return None
    key = github_key(repository, "issue", issue["number"])
    return {
        "provider": "github",
        "source_type": "issue",
        "identity": f"github:{repository.casefold()}:{issue['id']}",
        "source_key": key,
        "repository": repository,
        "number": issue["number"],
        "url": issue["html_url"],
        "title": safe_text(issue["title"], 500),
        "description": safe_text(issue.get("body") or "", 6000),
        "description_truncated": len(issue.get("body") or "") > 6000,
        "content_digest": digest([issue["title"], issue.get("body") or ""]),
        "author": issue["user"]["login"],
        "created_at": issue["created_at"],
        "owners": sorted(owners),
    }


def tapd_source(project, item, terminal, allowed, *, allow_unbound=False, todo_states=()):
    routing, repository, label = tapd_policy.select_repository(item.labels, project["label_rules"])
    if (
        (not repository and not allow_unbound)
        or item.owners is None
        or any(owner not in allowed for owner in item.owners)
        or item.archived is True
        or todo_states
        and item.status not in todo_states
        or tapd_policy.is_ended(item.status, terminal)
    ):
        return None
    host, workspace = project["host"], project["workspace_id"]
    key = (
        tapd_identity.source_key(host, workspace, item.kind, item.external_id, repository)
        if repository
        else f"tapd:{host}/{workspace}/{item.kind}/{item.external_id}"
    )
    source = {
        "provider": "tapd",
        "source_type": tapd_identity.SOURCE_TYPES[item.kind],
        "identity": f"tapd:{host}/{workspace}/{item.kind}/{item.external_id}",
        "source_key": key,
        "repository": repository,
        "repository_label": label,
        "project_key": tapd_identity.project_key(host, workspace),
        "kind": item.kind,
        "external_id": item.external_id,
        "url": tapd_identity.ticket_url(host, workspace, item.kind, item.external_id),
        "title": safe_text(item.title, 500),
        "description": safe_text(item.description, 6000),
        "description_truncated": len(item.description) > 6000,
        "content_digest": digest([item.title, item.description]),
        "created_at": item.created,
        "owners": sorted(item.owners),
    }
    if allow_unbound and not repository:
        source["repository_candidates"] = [dict(rule) for rule in project["label_rules"]]
        if routing == "conflict":
            source["routing_conflict"] = True
    return source


def validate_assessment(output, commit, excerpts):
    if output == {"decision": "skip"}:
        return None
    fields = ("summary", "scope", "exclusions", "verification", "risks")
    ratings = {"priority", "recommendation", "recommendation_reason"}
    if (
        not isinstance(output, dict)
        or set(output) - ratings != {"decision", "kind", "evidence", *fields}
        or output["decision"] != "propose"
        or output["kind"] not in ("implementation", "investigation")
        or any(not isinstance(output[key], str) or not output[key].strip() or len(output[key]) > 1500 for key in fields)
    ):
        raise ValueError("invalid proposal assessment")
    expected = {item["id"]: item for item in excerpts}
    evidence = []
    references = set()
    if not isinstance(output["evidence"], list) or not 1 <= len(output["evidence"]) <= 8:
        raise ValueError("proposal must cite code evidence")
    for item in output["evidence"]:
        if not isinstance(item, dict) or set(item) != {"id", "line", "reason"} or not isinstance(item["id"], str):
            raise ValueError("invalid proposal evidence")
        excerpt = expected.get(item["id"])
        line, reason = item["line"], item["reason"]
        if (
            not excerpt
            or type(line) is not int
            or not excerpt["start"] <= line <= excerpt["end"]
            or not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > 500
            or (item["id"], line) in references
        ):
            raise ValueError("proposal cited unavailable code evidence")
        references.add((item["id"], line))
        evidence.append({"path": excerpt["path"], "line": line, "reason": safe_text(reason, 500)})
    rank = {}
    if ratings.intersection(output):
        if (
            str(output.get("priority")) not in {"high", "medium", "low"}
            or str(output.get("recommendation")) not in {"high", "medium", "low"}
            or not isinstance(output.get("recommendation_reason"), str)
            or not output["recommendation_reason"].strip()
            or len(output["recommendation_reason"]) > 500
        ):
            raise ValueError("invalid proposal ratings")
        rank = {
            "priority": output["priority"],
            "recommendation": output["recommendation"],
            "recommendation_reason": safe_text(output["recommendation_reason"], 500),
        }
    return {
        **rank,
        "kind": output["kind"],
        "commit": commit,
        "evidence": evidence,
        **{field: safe_text(output[field], 1500) for field in fields},
    }
