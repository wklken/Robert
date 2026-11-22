"""Select confirmed delivery milestones for the opt-in OpenClaw digest."""

import hashlib
import json
import re
from urllib.parse import urlsplit, urlunsplit

from robert_agent.foundation.contracts.delivery import AuditStatus, GitHubActionType, OutputType, PublishStatus
from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.persistence import records
from robert_agent.persistence.queries.readviews import task_history, work_items
from robert_agent.persistence.stores import notifications
from robert_agent.security.text import safe_public_link, safe_text


REPORTS = frozenset(
    {OutputType.COMMENT_ANALYSIS, OutputType.MULTI_AGENT_REVIEW_REPORT, OutputType.MULTI_AGENT_VOTE_REPORT}
)
PR_EVENTS = {
    GitHubActionType.OPEN_PR: "pr_opened",
    GitHubActionType.PUSH_EXISTING_PR: "pr_updated",
    GitHubActionType.SUBMIT_PR_REVIEW: "pr_reviewed",
}
PR_REVIEW_OUTPUTS = frozenset(
    {OutputType.REVIEW_COMMENT, OutputType.PR_REVIEW_COMMENT, OutputType.MULTI_AGENT_REVIEW_REPORT}
)


def receipt_settings(config):
    messaging = (config or {}).get("messaging", {})
    receipts = messaging.get("receipts", {})
    if not messaging.get("enabled") or messaging.get("provider") != "openclaw" or not receipts.get("enabled"):
        return None
    activations = [utc_timestamp(value) for value in (messaging.get("send_after"), receipts.get("send_after"))]
    if not all(activations):
        return None
    destination_fields = ("provider", "channel", "account", "target", "conversation", "thread")
    destination = hashlib.sha256(
        json.dumps([messaging.get(field, "") for field in destination_fields]).encode()
    ).hexdigest()
    return tuple(receipts["events"]), max(activations), destination


def _pr_url(value, repository):
    url = safe_public_link(value)
    if not url:
        return None
    parsed = urlsplit(url)
    if not re.fullmatch(re.escape("/" + repository) + r"/pull/[1-9][0-9]*/?", parsed.path, re.I):
        return None
    return urlunsplit(parsed._replace(path=parsed.path.rstrip("/"), fragment=""))


def _record(conn, *, signal, event, identity, occurred, repo_id, repository, title, task_id, result_id, url):
    events, after, destination = signal
    when = utc_timestamp(occurred)
    if event not in events or when is None or when <= after:
        return
    key = hashlib.sha256(json.dumps([repo_id, event, identity]).encode()).hexdigest()
    metadata = {
        "destination": destination,
        "repo_id": repo_id,
        "repository": safe_text(repository, 300),
        "title": safe_text(title, 400),
        "result_id": result_id,
        "url": url,
    }
    notifications.record_receipt_event(
        conn, f"receipt-{key}", task_id, event, when.isoformat(), json.dumps(metadata, ensure_ascii=False)
    )


def record_completed(conn, config, repo_id, repository, workstream_id, work_item_id, payload):
    signal = receipt_settings(config)
    if signal is None or repository not in config["messaging"]["repos"]:
        return
    actions = [
        row
        for row in task_history.timeline_actions_for_results(conn, [payload["result_id"]])
        if row["publish_status"] == PublishStatus.PUBLISHED and row["audit_status"] == AuditStatus.ACCEPTED
    ]
    if not actions:
        return
    source = records.first(work_items.workstream_sources(conn, [workstream_id]))
    title = source["source_title"] if source else ""
    context = {
        "signal": signal,
        "repo_id": repo_id,
        "repository": repository,
        "title": title,
        "task_id": payload["task_id"],
        "result_id": payload["result_id"],
    }
    for action in actions:
        event = PR_EVENTS.get(action["action_type"])
        target = action["target_url"]
        if (
            action["action_type"] == GitHubActionType.COMMENT
            and payload["output_type"] in PR_REVIEW_OUTPUTS
            and source
            and source["source_type"] == "pull_request"
        ):
            event = "pr_reviewed"
            target = source["github_url"]
        if event and (url := _pr_url(target, repository)):
            _record(conn, event=event, identity=url.lower(), url=url, occurred=action["published_at"], **context)
    if (
        payload["output_type"] in REPORTS
        and (source is None or source["source_type"] != "pull_request")
        and all(action["action_type"] == GitHubActionType.COMMENT for action in actions)
    ):
        url = next((url for action in actions if (url := safe_public_link(action["target_url"]))), None)
        confirmed = [utc_timestamp(action["published_at"]) for action in actions]
        if all(confirmed):
            _record(
                conn,
                event="report_delivered",
                identity=work_item_id or workstream_id,
                url=url,
                occurred=max(confirmed).isoformat(),
                **context,
            )


def record_merge(conn, config, repo_id, repository, remote):
    signal = receipt_settings(config)
    if signal is None or repository not in config["messaging"]["repos"]:
        return
    url = _pr_url(remote.get("html_url"), repository)
    if url:
        _record(
            conn,
            signal=signal,
            event="pr_merged",
            identity=url.lower(),
            occurred=remote.get("merged_at"),
            repo_id=repo_id,
            repository=repository,
            title=remote.get("title"),
            task_id=None,
            result_id=None,
            url=url,
        )
