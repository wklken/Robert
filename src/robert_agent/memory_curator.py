#!/usr/bin/env python3
import argparse
from contextlib import closing
from pathlib import Path
import sqlite3
from datetime import datetime, timezone

from robert_agent.common import emit
from robert_agent import runtime_knowledge, storage


def _now():
    return datetime.now(timezone.utc).isoformat()


def _repo_id(conn, repo_id=None):
    if repo_id:
        return repo_id
    row = conn.execute("SELECT repo_id FROM repos ORDER BY repo_id LIMIT 1").fetchone()
    if not row:
        raise ValueError("no repo rows found")
    return row[0]


def run_command(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)

    propose = subparsers.add_parser("propose")
    propose.add_argument("--repo-id")

    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--repo-id")
    list_parser.add_argument("--status")

    show = subparsers.add_parser("show")
    show.add_argument("--candidate-id", required=True)

    approve = subparsers.add_parser("approve")
    approve.add_argument("--candidate-id", required=True)
    approve.add_argument("--scope-type", required=True)
    approve.add_argument("--scope-value", default="")
    approve.add_argument("--approved-by", required=True)

    reject = subparsers.add_parser("reject")
    reject.add_argument("--candidate-id", required=True)
    reject.add_argument("--reviewer", required=True)
    reject.add_argument("--review-note", default="")

    args = parser.parse_args(argv)
    db_path = Path(args.db).expanduser()
    storage.init_database(db_path)
    now = _now()
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        if args.command == "propose":
            return runtime_knowledge.propose_candidates(
                conn,
                _repo_id(conn, args.repo_id),
                now,
            )
        if args.command == "list":
            candidates = runtime_knowledge.list_candidates(
                conn,
                repo_id=args.repo_id,
                status=args.status,
            )
            return {"ok": True, "status": "listed", "candidates": candidates}
        if args.command == "show":
            candidate = runtime_knowledge.show_candidate(conn, args.candidate_id)
            if not candidate:
                return {"ok": False, "status": "not_found", "safe_error": f"candidate not found: {args.candidate_id}"}
            return {"ok": True, "status": "shown", "candidate": candidate}
        if args.command == "approve":
            return runtime_knowledge.approve_candidate(
                conn,
                candidate_id=args.candidate_id,
                scope_type=args.scope_type,
                scope_value=args.scope_value,
                approved_by=args.approved_by,
                run_now=now,
            )
        if args.command == "reject":
            return runtime_knowledge.reject_candidate(
                conn,
                candidate_id=args.candidate_id,
                reviewer=args.reviewer,
                review_note=args.review_note,
                run_now=now,
            )
    return {"ok": False, "status": "failed_command", "safe_error": f"unknown command: {args.command}"}


def main(argv=None):
    try:
        payload = run_command(argv)
    except ValueError as exc:
        payload = {"ok": False, "status": "failed_validation", "safe_error": str(exc)}
    return emit(payload)


if __name__ == "__main__":
    raise SystemExit(main())
