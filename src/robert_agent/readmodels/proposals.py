"""Proposal previews, findings, and operator decisions."""

from contextlib import closing
from pathlib import Path

from robert_agent.foundation.values import json_object
from robert_agent.persistence import database
from robert_agent.persistence.queries import proposal_feedback, proposals, sqlite_master


VISIBLE_STATES = frozenset({"pending", "held", "discarded", "approved", "admitted", "blocked"})


def _view(row):
    source = json_object(row["source_json"])
    return {
        **{
            key: row[key]
            for key in (
                "proposal_id",
                "repo_id",
                "version",
                "revision",
                "status",
                "created_at",
                "updated_at",
                "work_item_id",
                "task_id",
                "workstream_id",
                "blocked_reason",
            )
        },
        "title": source.get("title", ""),
        "source_url": source.get("url", ""),
        "provider": source.get("provider", ""),
        "repository": source.get("repository", ""),
        "report": json_object(row["report_json"]),
    }


def read(db_path, *, status=None, proposal_id=None, repo_ids=None):
    path = Path(db_path)
    if not path.is_file():
        return []
    with closing(database.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        if not sqlite_master.table_exists(conn, "proposals"):
            return []
        rows = proposals.current(conn, proposal_id=proposal_id, status=status, repo_ids=repo_ids)
        views = [_view(row) for row in rows]
        if proposal_id is not None and views:
            views[0]["decisions"] = [
                {key: decision[key] for key in ("revision", "action", "actor", "reason", "created_at")}
                for decision in proposals.decision_history(conn, proposal_id)
            ]
            views[0]["feedback"] = [
                {
                    "event": receipt["notification_type"],
                    "status": receipt["status"],
                    "comment_id": (json_object(receipt["metadata_json"]).get("delivery") or {}).get("comment_id"),
                }
                for receipt in proposal_feedback.status(conn, proposal_id)
            ]
            if rows[0]["task_id"]:
                progress = proposals.work_progress(conn, rows[0]["task_id"])
                if progress:
                    pr = proposal_feedback.pull_request(conn, rows[0]["task_id"])
                    lifecycle = progress["lifecycle"]
                    state = (
                        {
                            "open": "in_review",
                            "merged": "pr_merged",
                            "closed": "pr_closed",
                        }.get(pr["state"], lifecycle)
                        if pr and lifecycle == "completed"
                        else lifecycle
                    )
                    views[0]["work"] = {
                        "state": state,
                        "terminal": state in ("completed", "failed", "canceled", "pr_merged", "pr_closed"),
                        "pr": pr,
                    }
    return views
