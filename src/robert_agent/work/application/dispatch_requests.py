"""Accept authenticated dispatch drafts and durably create their GitHub Issues."""

from contextlib import closing
import json
from pathlib import Path
import subprocess
from uuid import uuid4

from robert_agent.adapters.github import issues, discovery
from robert_agent.adapters.notifications import wakeup
from robert_agent.delivery.lock import task_publication_lock
from robert_agent.foundation import values
from robert_agent.foundation.commands import ControlDeadlineExceeded
from robert_agent.foundation.contracts.work import DispatchRequestStatus as Status
from robert_agent.persistence import database, transactions, records
from robert_agent.persistence.queries import dispatch_requests as queries, repos
from robert_agent.persistence.stores import dispatch_requests as stores
from robert_agent.security.redaction import redact_text
from robert_agent.work import dispatch_requests as facts
from robert_agent.work.errors import WorkItemValidationError, WorkItemNotFoundError, WorkItemConflictError
from robert_agent.work.inputs import validate_text, MAX_DESCRIPTION
from robert_agent.work.messaging_identity import authenticate, dispatch_allowed, IDENTITY_FIELDS


def _connect(config):
    return database.connect(Path(config["db_path"]).resolve().as_uri() + "?mode=rw", uri=True)


def _view(conn, row):
    issue = json.loads(row["issue_json"])
    return {
        "ok": True,
        "request_id": row["request_id"],
        "status": row["status"],
        "title": row["title"],
        "issue_url": issue.get("html_url"),
        "task_id": row["task_id"],
        "safe_error": row["error"],
        "result": json.loads(row["result_json"]),
        "task": queries.latest_task(conn, row["request_id"]) if row["workstream_id"] else None,
        **({"submit_command": f"/robert-inbox submit {row['request_id']}"} if row["status"] == Status.DRAFT else {}),
    }


def _notice(conn, row, kind, text, now):
    issue = json.loads(row["issue_json"])
    stores.notification(
        conn,
        row["request_id"],
        kind,
        now,
        json.dumps(
            {
                "request_id": row["request_id"],
                "repo_id": row["repo_id"],
                "repository_scoped": True,
                "dispatch_identity": json.loads(row["identity_json"]),
                "summary": text,
                "issue_url": issue.get("html_url"),
            },
            ensure_ascii=False,
        ),
        row["task_id"],
    )


def handle(config, request):
    authenticate(config, request)
    if not config["messaging"].get("dispatch_enabled"):
        raise WorkItemValidationError("Issue dispatch is disabled.")
    action = request.get("action")
    now = values.utc_now()
    identity = {key: request.get(key, "") for key in IDENTITY_FIELDS}
    identity["protocol"] = "robert.message.v1"
    with closing(_connect(config)) as conn, conn:
        transactions.begin_immediate(conn)
        if action == "new":
            repo = request.get("repo")
            if not dispatch_allowed(config, identity, repo):
                raise WorkItemValidationError("Repository is not enabled for dispatch.")
            record = records.first(repos.find_repo_id_by_full_name(conn, repo))
            if not record:
                raise WorkItemValidationError("Repository is not initialized; start Robert first.")
            body = validate_text(request.get("body"), "body", MAX_DESCRIPTION, required=True)
            safe = redact_text(body)
            if not safe["ok"] or safe["text"] != body:
                raise WorkItemValidationError("Request contains sensitive text; remove it before dispatch.")
            request_id = "dispatch-" + uuid4().hex
            stores.create(
                conn,
                {
                    "request_id": request_id,
                    "repo_id": record[0],
                    "identity_json": json.dumps(identity, sort_keys=True),
                    "title": body.strip().splitlines()[0][:160],
                    "body": body,
                    "status": Status.DRAFT.value,
                    "created_at": now,
                    "updated_at": now,
                },
            )
        else:
            request_id = validate_text(request.get("request_id"), "request_id", 128, required=True)
            row = queries.get(conn, request_id)
            if not row or json.loads(row["identity_json"]) != identity or not facts.authorized(config, row):
                raise WorkItemNotFoundError("Dispatch request not found.")
            if action == "submit":
                stores.transition(conn, request_id, Status.DRAFT.value, Status.ACCEPTED.value, now)
            elif action == "retry":
                if row["status"] not in {Status.FAILED, Status.ATTENTION}:
                    raise WorkItemConflictError("This request does not need dispatch recovery.")
                target = Status.ACCEPTED if row["status"] == Status.FAILED else Status.CREATING
                stores.transition(conn, request_id, row["status"], target.value, now, error="")
            elif action != "status":
                raise WorkItemValidationError("Unsupported dispatch action.")
        result = _view(conn, queries.get(conn, request_id))
    if action in {"submit", "retry"}:
        wakeup.notify(config["db_path"])
    return result


def _failed(config, row, status, message):
    now = values.utc_now()
    with closing(_connect(config)) as conn, conn:
        if stores.transition(conn, row["request_id"], row["status"], status.value, now, error=message):
            _notice(conn, row, "attention", message + f" /robert-inbox status {row['request_id']}", now)


def _advance_one(config, row, runner):
    repo = next(item for item in config["repos"] if values.repository_id(item["full_name"]) == row["repo_id"])
    request_id = row["request_id"]
    body = row["body"] + f"\n\n<!-- robert-dispatch request_id={request_id} -->"
    if row["status"] == Status.ACCEPTED:
        try:
            issues.check_identity(repo["github_account"], runner=runner)
        except (OSError, subprocess.SubprocessError, ValueError):
            _failed(
                config, row, Status.FAILED, "Cannot verify the configured GitHub identity; inspect local credentials."
            )
            return
        with closing(_connect(config)) as conn, conn:
            current = queries.get(conn, request_id)
            if not current or not facts.authorized(config, current):
                return
            if not stores.transition(conn, request_id, Status.ACCEPTED.value, Status.CREATING.value, values.utc_now()):
                return
        row = {**row, "status": Status.CREATING.value}
        try:
            issue = issues.create(repo["full_name"], title=row["title"], body=body, runner=runner)
            if issue["user"]["login"].casefold() != repo["github_account"].casefold() or issue.get("body") != body:
                raise ValueError("Issue publisher identity changed")
        except ControlDeadlineExceeded:
            raise  # The persisted creating state requires reconciliation after restart.
        except (OSError, subprocess.SubprocessError, ValueError):
            _failed(
                config,
                row,
                Status.ATTENTION,
                "Issue creation outcome is unknown; retry reconciles without creating another Issue.",
            )
            return
    else:
        try:
            matches = issues.find_created(
                repo["full_name"], request_id, repo["github_account"], row["created_at"], runner=runner
            )
        except ControlDeadlineExceeded:
            raise
        except (OSError, subprocess.SubprocessError, ValueError):
            matches = []
        matches = [issue for issue in matches if issue.get("body") == body]
        if len(matches) != 1:
            _failed(
                config,
                row,
                Status.ATTENTION,
                "Could not confirm exactly one Issue; inspect GitHub before retrying reconciliation.",
            )
            return
        issue = matches[0]
    now = values.utc_now()
    with closing(_connect(config)) as conn, conn:
        if stores.transition(
            conn,
            request_id,
            Status.CREATING.value,
            Status.ISSUE_CREATED.value,
            now,
            issue_json=json.dumps(issue, ensure_ascii=False),
            workstream_id=discovery.source_key(repo["full_name"], "issue", issue["number"]),
            error="",
        ):
            saved = queries.get(conn, request_id)
            _notice(conn, saved, "created", "Issue created; Robert will classify and execute the request.", now)
    wakeup.notify(config["db_path"])


def advance(config, *, runner=None):
    with closing(_connect(config)) as conn:
        pending = facts.pending(conn, config, remote=True)
    # One bounded remote write/reconciliation per publisher opportunity.
    for row in pending:
        with task_publication_lock(config["db_path"], "issue-" + row["request_id"]) as acquired:
            if not acquired:
                continue
            with closing(_connect(config)) as conn:
                current = queries.get(conn, row["request_id"])
            if (
                current
                and current["status"] in {Status.ACCEPTED, Status.CREATING}
                and facts.authorized(config, current)
            ):
                _advance_one(config, current, runner)
                return 1
    return 0
