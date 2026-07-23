#!/usr/bin/env python3
import argparse
from contextlib import closing
import sqlite3

from robert_agent.common import emit


SUMMARY_TABLES = [
    "repos",
    "github_sources",
    "github_events",
    "workstreams",
    "tasks",
    "attempts",
    "worker_results",
    "github_actions",
    "notifications",
    "agent_runs",
]


def summarize_database(db_path):
    with closing(sqlite3.connect(db_path)) as conn, conn:
        summary = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in SUMMARY_TABLES
        }
        summary["rejected_worker_results"] = conn.execute(
            """
            SELECT COUNT(*)
            FROM worker_results
            WHERE json_extract(metadata_json, '$.audit.status') IN ('failed', 'policy_violation')
            """
        ).fetchone()[0]
        summary["failed_github_actions"] = conn.execute(
            """
            SELECT COUNT(*)
            FROM github_actions
            WHERE audit_status IN ('failed', 'policy_violation')
            """
        ).fetchone()[0]
        summary["pending_publish_actions"] = conn.execute(
            """
            SELECT COUNT(*)
            FROM github_actions
            WHERE audit_status = 'accepted'
              AND publish_status = 'not_published'
            """
        ).fetchone()[0]
        summary["publish_failed_actions"] = conn.execute(
            """
            SELECT COUNT(*)
            FROM github_actions
            WHERE audit_status = 'accepted'
              AND publish_status = 'not_published'
              AND json_extract(metadata_json, '$.publish.status') = 'publish_failed'
            """
        ).fetchone()[0]
        summary["skipped_publish_actions"] = conn.execute(
            """
            SELECT COUNT(*)
            FROM github_actions
            WHERE audit_status = 'accepted'
              AND publish_status = 'skipped'
            """
        ).fetchone()[0]
        summary["pending_actor_permission_events"] = conn.execute(
            """
            SELECT COUNT(*)
            FROM github_events
            WHERE authorization_status = 'pending_actor_permission'
            """
        ).fetchone()[0]
        summary["pending_authorization_events"] = conn.execute(
            """
            SELECT COUNT(*)
            FROM github_events
            WHERE authorization_status = 'pending_authorization'
            """
        ).fetchone()[0]
        summary["skipped_closed_sources"] = conn.execute(
            """
            SELECT COUNT(*)
            FROM audit_events
            WHERE event_type = 'current_state_closed'
            """
        ).fetchone()[0]
        return summary


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    args = parser.parse_args(argv)
    return emit({"ok": True, "status": "summarized", "summary": summarize_database(args.db)})


if __name__ == "__main__":
    raise SystemExit(main())
