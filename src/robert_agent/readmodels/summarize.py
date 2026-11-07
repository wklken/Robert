#!/usr/bin/env python3
import argparse
from contextlib import closing

from robert_agent.foundation.commands import emit
from robert_agent.persistence import database as db_database
from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import audit_events as db_read_audit_events
from robert_agent.persistence.queries import github_actions as db_read_github_actions
from robert_agent.persistence.queries import github_events as db_read_github_events
from robert_agent.persistence.queries import worker_results as db_read_worker_results
from robert_agent.persistence.queries.readviews import counts


def summarize_database(db_path):
    with closing(db_database.connect(db_path)) as conn, conn:
        summary = counts.database_totals(conn)
        summary["rejected_worker_results"] = db_records.first(db_read_worker_results.find_for_summarize_database(conn))[
            0
        ]
        summary["failed_github_actions"] = db_records.first(db_read_github_actions.find_for_summarize_database(conn))[0]
        summary["pending_publish_actions"] = db_records.first(
            db_read_github_actions.find_for_control_plane_state_gate(conn)
        )[0]
        summary["publish_failed_actions"] = db_records.first(
            db_read_github_actions.find_for_summarize_database_2(conn)
        )[0]
        summary["skipped_publish_actions"] = db_records.first(
            db_read_github_actions.find_for_summarize_database_3(conn)
        )[0]
        summary["pending_actor_permission_events"] = db_records.first(
            db_read_github_events.find_for_summarize_database(conn)
        )[0]
        summary["pending_authorization_events"] = db_records.first(
            db_read_github_events.find_for_summarize_database_2(conn)
        )[0]
        summary["skipped_closed_sources"] = db_records.first(db_read_audit_events.find_for_summarize_database(conn))[0]
        return summary


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    args = parser.parse_args(argv)
    return emit({"ok": True, "status": "summarized", "summary": summarize_database(args.db)})


if __name__ == "__main__":
    raise SystemExit(main())
