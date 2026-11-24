"""Public CLI parsing and presentation for read-only task inspection."""

import argparse
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from robert_agent.cli.output import emit_result
from robert_agent.foundation.contracts.work import TaskLifecycle
from robert_agent.persistence import database, errors as db_errors, transactions
from robert_agent.readmodels import status, tasks
from robert_agent.security.text import safe_text


def add_parsers(subparsers, add_config, add_output):
    task_parser = subparsers.add_parser("task", help="Inspect task lifecycles, elapsed times and recorded evidence.")
    commands = task_parser.add_subparsers(dest="task_command", required=True)
    examples = {
        "list": "robert task list --state running --latest\n  robert task list --query 073 --output json",
        "show": "robert task show TASK_ID --output json",
        "stages": "robert task stages TASK_ID\n  robert task stages TASK_ID --history --output json",
        "snapshots": "robert task snapshots TASK_ID --output json",
        "logs": "robert task logs TASK_ID --lines 40 --max-bytes 8192",
    }
    descriptions = {
        "list": "List tasks with latest attempt elapsed times; filters apply before pagination.",
        "show": "Show a task with current timing, phase, wait reason and failure/publication evidence.",
        "stages": "Inspect model runs owned by the latest attempt, or preserved history.",
        "snapshots": "List recorded snapshot identities and hashes, including historical snapshots.",
        "logs": "Read a bounded, redacted tail of registered stderr for the latest attempt.",
    }
    for command, example in examples.items():
        parser = commands.add_parser(
            command,
            help=descriptions[command],
            description=descriptions[command],
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog="Examples:\n  " + example,
        )
        add_config(parser)
        add_output(parser)
        parser.add_argument("--db", help="Read an existing database without loading configuration.")
        if command != "list":
            parser.add_argument("task_id", help="Exact recorded task ID; resolve public refs with task list --query.")
        if command in {"list", "stages", "snapshots"}:
            parser.add_argument("--limit", type=int, default=50, help="Page size (1..500).")
            parser.add_argument("--offset", type=int, default=0, help="Non-negative offset; live pages may change.")
        if command == "list":
            parser.add_argument(
                "--state",
                action="append",
                choices=[state.value for state in TaskLifecycle],
                default=[],
                help="Task lifecycle; repeat to select several states.",
            )
            parser.add_argument("--repo", help="Exact owner/repository.")
            parser.add_argument(
                "--query", help="Literal substring in title, public ref, source, task or workstream ID."
            )
            parser.add_argument("--since", help="Created since ISO timestamp with timezone (inclusive).")
            parser.add_argument(
                "--latest", action="store_true", help="Latest task per workstream, selected before other filters."
            )
        elif command == "stages":
            parser.add_argument(
                "--history", action="store_true", help="Include runs from preserved/rebound rounds and old attempts."
            )
        elif command == "logs":
            parser.add_argument("--lines", type=int, default=40, help="Tail line limit (1..500).")
            parser.add_argument("--max-bytes", type=int, default=65536, help="Read limit (1..65536 bytes).")


def _envelope(command, data):
    return {
        "ok": True,
        "status": "ready",
        "command": command,
        "format": "json",
        "checked_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "data": data,
    }


def _safe_fields(value):
    if isinstance(value, dict):
        return {key: _safe_fields(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_fields(item) for item in value]
    return safe_text(value, 65536) if isinstance(value, str) else value


def _duration(value):
    if value is None:
        return "unknown"
    seconds = int(value)
    return (
        f"{seconds // 3600}h{seconds % 3600 // 60:02d}m" if seconds >= 3600 else f"{seconds // 60}m{seconds % 60:02d}s"
    )


def _table(headers, rows):
    rows = [[str(value) if value is not None else "-" for value in row] for row in rows]
    widths = (
        [max(len(header), *(len(row[i]) for row in rows)) for i, header in enumerate(headers)]
        if rows
        else list(map(len, headers))
    )
    for row in [headers, *rows]:
        print("  ".join(str(value).ljust(widths[i]) for i, value in enumerate(row)).rstrip())


def _task_table(page, pagination_command="robert task list"):
    rows = []
    for item in page["items"]:
        attempt = item.get("current_attempt") or {}
        stages = ",".join(sorted({run["stage"] for run in item.get("active_stages", [])}))
        phase = item.get("phase") or {}
        rows.append(
            (
                item.get("public_ref"),
                item["task_id"],
                item["lifecycle"],
                _duration(attempt.get("elapsed_seconds")),
                stages or phase.get("phase") or "unknown",
                _duration(attempt.get("heartbeat_age_seconds")),
                item["waiting_reason"],
                item["title"],
            )
        )
    _table(["REF", "TASK", "STATE", "ELAPSED", "STAGE", "HEARTBEAT AGE", "WAIT", "TITLE"], rows)
    if page.get("has_more"):
        print(f"More tasks: {pagination_command} --offset {page['offset'] + page['limit']}")


def _emit(result, output):
    if output == "json" or not result.get("ok"):
        emit_result(result, output=output)
        return 0 if result.get("ok") else 3
    command, data = result["command"], result["data"]
    if command == "task list":
        _task_table(data)
    elif command == "task":
        inspection = data["inspection"]
        _task_table({"items": [inspection]})
        attempt = inspection.get("current_attempt") or {}
        print(f"attempt: {attempt.get('attempt_id') or 'none'}; started: {attempt.get('started_at') or 'unknown'}")
        print(f"failure: {attempt.get('failure') or 'none recorded'}")
        for action in inspection["publication"]:
            print(f"publication {action['action_id']}: {action['status']} {action['safe_error']}")
        for key in ("review", "vote", "pre_pr_review"):
            if data.get(key):
                print(f"{key}: {data[key]['status']} {data[key].get('error') or ''}")
    elif command == "task stages":
        _table(
            ["RUN", "STAGE", "WORKER", "STATUS", "CURRENT", "ELAPSED", "MODEL TIME", "PROCESS", "LOG AGE", "ERROR"],
            [
                (
                    row["run_id"],
                    row["stage"],
                    row["worker_id"],
                    row["status"],
                    row["current_attempt"],
                    _duration(row["elapsed_seconds"]),
                    _duration(row["model_activity"]["activity_seconds"]),
                    row["process"]["status"],
                    _duration(row.get("logs", {}).get("stdout.log", {}).get("age_seconds")),
                    row["error"],
                )
                for row in data["items"]
            ],
        )
    elif command == "task snapshots":
        _table(
            ["SNAPSHOT", "ROUND", "CURRENT", "HEAD", "BASE", "DIFF HASH"],
            [
                (
                    row["snapshot_id"],
                    row["round_id"],
                    row["current"],
                    row["head_sha"],
                    row["base_sha"],
                    row.get("diff_sha256"),
                )
                for row in data["items"]
            ],
        )
        for row in data["round_manifests"]:
            print(
                f"{row['kind']} {row['round_id']}: head={row.get('head_sha')} base={row.get('base_sha') or row.get('base_ref')}"
            )
    elif command == "task logs":
        for row in data["items"]:
            print(f"{row['artifact_id']}: missing={row['missing']} truncated={row['truncated']}\n{row['content']}")
        if not data["items"]:
            print("No registered stderr for the latest attempt.")
    else:
        emit_result(
            {key: value for key, value in result.items() if key != "data"}
            | {
                "summary": data["summary"],
                "lifecycle_counts": data["lifecycle_counts"],
                "model_pool": data["model_pool"],
                "daemon": data["daemon"],
            }
        )
        if data.get("running_tasks"):
            _task_table(data["running_tasks"], pagination_command="robert task list --state running")
    if data.get("note"):
        print(data["note"])
    if command in {"task stages", "task snapshots"} and data.get("has_more"):
        print(f"More records: use --offset {data['offset'] + data['limit']}")
    return 0


def run(args, db_path):
    try:
        command = args.task_command
        if command == "list":
            data = tasks.list_tasks(
                db_path,
                states=args.state,
                repo=args.repo,
                query=args.query,
                since=args.since,
                latest=args.latest,
                limit=args.limit,
                offset=args.offset,
            )
        elif command == "show":
            if Path(db_path).expanduser().is_file():
                with closing(
                    database.connect(Path(db_path).expanduser().resolve().as_uri() + "?mode=ro", uri=True)
                ) as conn:
                    transactions.begin(conn)
                    data = status.build_task(conn, args.task_id)
            else:
                raise FileNotFoundError(f"database not found: {db_path}")
        elif command == "stages":
            data = tasks.task_stages(db_path, args.task_id, history=args.history, limit=args.limit, offset=args.offset)
        elif command == "snapshots":
            data = tasks.task_snapshots(db_path, args.task_id, limit=args.limit, offset=args.offset)
        else:
            data = tasks.task_logs(db_path, args.task_id, lines=args.lines, max_bytes=args.max_bytes)
        result = (
            _envelope("task" if command == "show" else "task " + command, _safe_fields(data))
            if data is not None
            else {
                "ok": False,
                "status": "not_found",
                "safe_error": "task not found: " + safe_text(args.task_id),
            }
        )
    except (db_errors.Error, OSError, ValueError) as exc:
        result = {"ok": False, "status": "failed", "safe_error": safe_text(str(exc))}
    return _emit(result, args.output)


def run_status(args, db_path):
    try:
        if not 1 <= args.limit <= 500:
            raise ValueError("limit must be 1..500")
        if not Path(db_path).expanduser().is_file():
            raise FileNotFoundError(f"database not found: {db_path}")
        data = status.build_status(db_path, history_limit=args.limit, include_timings=not args.quick)
        data.update(tasks.control_summary(db_path))
        if args.tasks:
            data["running_tasks"] = tasks.list_tasks(db_path, states=("running",), limit=args.limit)
        result = _envelope("status", _safe_fields(data))
    except (db_errors.Error, OSError, ValueError) as exc:
        result = {"ok": False, "status": "failed", "safe_error": safe_text(str(exc))}
    return _emit(result, args.output)
