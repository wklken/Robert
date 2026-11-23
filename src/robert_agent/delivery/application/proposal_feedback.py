"""Publish fixed proposal receipts with read-back through the normal delivery lane."""

from contextlib import closing
import json
import re
import subprocess

from robert_agent.adapters.github import issues
from robert_agent.adapters.tapd.client import TapdError
from robert_agent.delivery.application import effects, tapd_comments
from robert_agent.delivery.comment_receipts import comment_records, match_comment
from robert_agent.delivery.lock import task_publication_lock
from robert_agent.execution import retry
from robert_agent.foundation import commands, values
from robert_agent.persistence import database, transactions
from robert_agent.persistence.queries import proposal_feedback as queries
from robert_agent.persistence.stores import proposal_feedback as store
from robert_agent.work import proposal_feedback as feedback, tapd_policy


_TEMPLATES = {
    "created": "已生成。请在 Robert Web 查看审批和进展。",
    "admitted": "已在 Robert Web 批准，工作已受理。",
    "operator_question": "等待进一步澄清，请查看单据讨论。",
    "execution_failed": "当前需要操作员检查，请查看 Robert Web。",
    "completed": "已完成，结论可在本单据中查看。",
    "pr_opened": "已提交 PR，等待评审。",
    "pr_merged": "关联的 PR 已合并。",
    "unmerged_pr_closed": "关联的 PR 已关闭。",
}
_ERRORS = (ValueError, OSError, subprocess.SubprocessError, TapdError)


def _scope(config):
    setting = config.get("proposer", {})
    return (
        [values.repository_id(repo) for repo in setting.get("github_repositories", ())],
        setting.get("tapd_projects", ()),
    )


def due(conn, config, now):
    setting = config.get("proposer", {})
    if not setting.get("enabled") or not setting.get("publish_comments"):
        return False
    return bool(queries.pending(conn, now, 1) or queries.unreported(conn, *_scope(config), limit=1))


def _collect(conn, config):
    for row in queries.unreported(conn, *_scope(config)):
        source = values.json_object(row["source_json"])
        event = values.json_object(row["event_json"])
        feedback.record_comment(
            conn,
            row["notification_id"],
            row["event_type"],
            row["proposal_id"],
            row["revision"],
            source,
            row["occurred_at"],
            pr_url=event.get("pr_url") or event.get("url"),
        )


def _safe_pr(metadata):
    url = metadata.get("pr_url")
    prefix = "https://github.com/" + metadata["repository"] + "/pull/"
    return url if isinstance(url, str) and re.fullmatch(re.escape(prefix) + r"[1-9][0-9]*", url) else None


def _comment(row, metadata):
    template = _TEMPLATES[row["notification_type"]]
    text = f"Robert 提案 {metadata['proposal_id']}（v{metadata['revision']}）{template}"
    url = _safe_pr(metadata)
    return text + "\n\n" + url if url else text


def _github_comment(config, metadata, marker, text, runner):
    endpoint = effects.issue_comment_endpoint(metadata["target_url"])
    repo = next((repo for repo in config["repos"] if repo["full_name"] == metadata["repository"]), None)
    if not endpoint or not repo or endpoint != f"repos/{repo['full_name']}/issues/{metadata['number']}/comments":
        return "skipped", {"reason_code": "source_out_of_scope"}
    body = text + "\n\n" + marker
    command = ["gh", "api", f"{endpoint}?per_page=100", "--paginate", "--slurp"]
    output = commands.run_command(command, runner=runner, capture_output=True, text=True, check=True, timeout=30)
    comments = comment_records(output.stdout)
    if comments is None:
        return "failed", {"reason_code": "comment_response_malformed"}
    receipt = match_comment(comments, body, repo["github_account"], marker)
    if receipt["status"] == "unknown":
        return "failed", {"reason_code": receipt["reason_code"]}
    if receipt["status"] == "confirmed_effect":
        return "sent", {"comment_id": str(receipt["comment"]["id"])}
    issue = issues.get(repo["full_name"], metadata["number"], runner=runner)
    if issue["html_url"] != metadata["target_url"] or issue["state"] != "open":
        return "skipped", {"reason_code": "source_closed_or_changed"}
    issues.check_identity(repo["github_account"], runner=runner)
    result = commands.run_command(
        ["gh", "api", endpoint, "--method", "POST", "--input", "-"],
        runner=runner,
        input=json.dumps({"body": body}, ensure_ascii=False),
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )
    response = json.loads(result.stdout)
    receipt = match_comment([response], body, repo["github_account"], marker) if isinstance(response, dict) else {}
    if receipt.get("status") != "confirmed_effect" or type(response.get("id")) is not int:
        raise ValueError("comment receipt unavailable")
    return "sent", {"comment_id": str(response["id"])}


def _tapd_comment(config, metadata, marker, text, runner):
    publisher = tapd_comments.publisher_for(config, runner)
    target = tapd_comments.ticket_target(metadata["target_url"], publisher.settings) if publisher else None
    if not target:
        return "skipped", {"reason_code": "source_out_of_scope"}

    def guard():
        client = publisher.client()
        item = client.get_item(*target)
        terminal = client.terminal_states(target[0], target[1], item.workitem_type_id if target[1] == "story" else None)
        if item.archived or tapd_policy.is_ended(item.status, terminal):
            return {"ok": True, "status": "skipped"}
        return None

    result = tapd_comments.publish(
        {"target_url": metadata["target_url"], "metadata_json": json.dumps({"body": text})},
        publisher,
        marker,
        scope_guard=guard,
    )
    if result["status"] == "skipped":
        return "skipped", {"reason_code": "source_ended"}
    if result["ok"]:
        return "sent", {"comment_id": result["external_id"]}
    if result.get("retryable", True):
        raise ValueError("TAPD comment write or read-back unavailable")
    return "failed", {"reason_code": "tapd_comment_unconfirmed"}


def deliver_pending(config, *, now=None, runner=None):
    setting = config.get("proposer", {})
    if not setting.get("enabled") or not setting.get("publish_comments"):
        return {}
    now = now or values.utc_now()
    with task_publication_lock(config["db_path"], "proposal-feedback") as acquired:
        return _deliver_locked(config, now, runner) if acquired else {}


def _deliver_locked(config, now, runner):
    db_path = config["db_path"]
    setting = config["proposer"]
    with closing(database.connect(db_path)) as conn, conn:
        transactions.begin_immediate(conn)
        _collect(conn, config)
        rows = queries.pending(conn, now)
    outcomes = {}
    for row in rows:
        identifier = row["notification_id"]
        metadata = values.json_object(row["metadata_json"])
        marker = "<!-- robert-proposal " + identifier + " -->"
        if not feedback.can_comment(setting, metadata):
            status, result = "skipped", {"reason_code": "source_out_of_scope"}
        else:
            try:
                send = _github_comment if metadata["provider"] == "github" else _tapd_comment
                status, result = send(config, metadata, marker, _comment(row, metadata), runner)
            except commands.ControlDeadlineExceeded:
                raise
            except _ERRORS:
                result = retry.failure_state(metadata.get("delivery") or {}, "proposal feedback unsuccessful", now)
                status = "pending" if result["retryable"] else "failed"
        with closing(database.connect(db_path)) as conn, conn:
            store.update_status(conn, identifier, status, json.dumps(result, ensure_ascii=False))
        outcomes[status] = outcomes.get(status, 0) + 1
    return outcomes
