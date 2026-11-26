"""Current authority of a stored, explicitly approved proposal."""

from robert_agent.foundation import tapd_identity, values
from robert_agent.persistence.queries import proposals, github_sources, workstreams
from robert_agent.persistence import records
from robert_agent.work import tapd_policy


def pending(conn, config, repo_ids=None):
    setting = (config or {}).get("proposer", {})
    if not setting.get("enabled"):
        return []
    return proposals.current(conn, status="approved", repo_ids=repo_ids)


def request_body(row):
    source = values.json_object(row["source_json"])
    report = values.json_object(row["report_json"])
    return "\n\n".join(
        (
            f"Web approved proposal {row['proposal_id']} revision {row['revision']}.",
            f"Requested outcome\n{report.get('summary', '')}",
            f"Authorized scope\n{report.get('scope', '')}",
            f"Excluded scope\n{report.get('exclusions', '')}",
            f"Acceptance\n{report.get('verification', '')}",
            f"Risks\n{report.get('risks', '')}",
            f"Original ticket\n{source.get('description', '')}",
        )
    )


def approved_source(row, decision):
    source = values.json_object(row["source_json"])
    options = values.json_object(decision.get("options_json"))
    label = options.get("repository_label")
    if not label:
        return source
    rule = next((rule for rule in source.get("repository_candidates", []) if rule["label"] == label), None)
    if not rule or source.get("provider") != "tapd":
        return {}
    host, workspace = source["project_key"].split("/", 1)
    repository = rule["repository"]
    return {
        **source,
        "repository": repository,
        "repository_label": label,
        "routing_unconfirmed": True,
        "source_key": tapd_identity.source_key(host, workspace, source["kind"], source["external_id"], repository),
    }


def reauthorize(conn, config, event, *, already_running=False):
    rejected = {
        **event,
        "authorization_status": "ignored_untrusted_trigger",
        "creates_task": False,
        "drives_execution": False,
    }
    if conn is None or not (config or {}).get("proposer", {}).get("enabled"):
        return rejected
    proposal_id, version = event.get("proposal_id"), event.get("proposal_decision_version")
    if not isinstance(proposal_id, str) or not proposal_id or type(version) is not int:
        return rejected
    rows = proposals.current(conn, proposal_id=proposal_id)
    if not rows:
        return rejected
    row = rows[0]
    setting = config["proposer"]
    decision = proposals.get_decision(conn, row["proposal_id"], version=version)
    source = approved_source(row, decision) if decision else {}
    expected_fingerprint = f"proposal-approved:{row['proposal_id']}:{version}"
    if (
        not decision
        or decision["action"] not in {"approve", "investigate"}
        or event.get("proposal_action", "approve") != decision["action"]
        or decision["revision"] != row["revision"]
        or row["status"] not in {"approved", "investigating", "admitted"}
        or row["version"] != (version if row["status"] == "approved" else version + 1)
        or (row["status"] == "investigating" and decision["action"] != "investigate")
        or (row["status"] == "admitted" and decision["action"] != "approve")
        or event.get("proposal_revision") != row["revision"]
        or event.get("event_type") != "proposal_approved"
        or event.get("actor_kind") != "operator"
        or event.get("actor_login") != decision["actor"]
        or event.get("source_key") != source.get("source_key")
        or event.get("repo") != source.get("repository")
        or event.get("provider") != source.get("provider")
        or event.get("url") != source.get("url")
        or event.get("source_type") != source.get("source_type")
        or event.get("event_fingerprint") != expected_fingerprint
        or event.get("body") != request_body(row)
        or event.get("proposal_discussion", []) != source.get("discussion", [])
    ):
        return rejected
    if row["status"] in {"admitted", "investigating"} and event.get("workstream_id") != row["workstream_id"]:
        return rejected
    if row["status"] == "approved" and event.get("workstream_id") != source["source_key"]:
        matches = workstreams.find_open_derived_pull_requests(conn, row["repo_id"], source["source_key"])
        if source.get("provider") != "tapd" or len(matches) != 1 or matches[0][0] != event.get("workstream_id"):
            return rejected
    repo = next(
        (repo for repo in config.get("repos", []) if values.repository_id(repo["full_name"]) == row["repo_id"]), None
    )
    if not repo or repo["full_name"] != source.get("repository"):
        return rejected
    if source.get("provider") == "github":
        if source["repository"] not in setting["github_repositories"] or event.get("number") != source.get("number"):
            return rejected
    elif source.get("provider") == "tapd":
        project = tapd_policy.project_for(config.get("tapd"), source.get("project_key"))
        if (
            not config.get("tapd", {}).get("enabled")
            or not project
            or source.get("project_key") not in setting["tapd_projects"]
            or not any(
                rule["repository"] == source["repository"] and rule["label"] == source.get("repository_label")
                for rule in project["label_rules"]
            )
            or event.get("project_key") != source.get("project_key")
            or event.get("external_id") != source.get("external_id")
        ):
            return rejected
    else:
        return rejected
    state = records.first(github_sources.find_state_by_source_id(conn, values.github_source_id(source["source_key"])))
    if state and state[0] == "closed" and not (already_running and event.get("existing_pr_number")):
        return rejected
    return {**event, "authorization_status": "authorized_trigger", "creates_task": True, "drives_execution": True}
