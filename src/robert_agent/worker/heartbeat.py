"""Worker heartbeat command-line boundary."""

import argparse
from robert_agent.execution import command as implementation
from robert_agent.foundation.commands import emit


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--db")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=5400)
    parser.add_argument("--heartbeat-interval-seconds", type=float, default=60)
    parser.add_argument("--output-dir")
    parser.add_argument(
        "--tail-bytes", type=int, default=implementation.DEFAULT_TAIL_BYTES
    )
    parser.add_argument(
        "--tail-lines", type=int, default=implementation.DEFAULT_TAIL_LINES
    )
    parser.add_argument(
        "--cwd", help="Working directory for the child (default: current directory)"
    )
    parser.add_argument(
        "--output-mode",
        choices=("summary", "tee"),
        default="summary",
        help="summary emits JSON tails; tee forwards raw streams and stores metadata only in the command journal",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if args.output_mode == "tee" and not args.db:
        parser.error("--output-mode tee requires --db")
    if args.db:
        result = implementation.run_command_with_heartbeat(
            db_path=args.db,
            task_id=args.task_id,
            attempt_id=args.attempt_id,
            phase=args.phase,
            command=command,
            timeout_seconds=args.timeout_seconds,
            heartbeat_interval_seconds=args.heartbeat_interval_seconds,
            output_dir=args.output_dir,
            tail_bytes=args.tail_bytes,
            tail_lines=args.tail_lines,
            cwd=args.cwd,
            output_mode=args.output_mode,
        )
        if args.output_mode == "tee":
            if result["returncode"] is None:
                return 124
            return (
                result["returncode"]
                if result["returncode"] >= 0
                else 128 - result["returncode"]
            )
    else:
        result = {
            "ok": True,
            "status": "built",
            "command": implementation.build_command_record(
                args.task_id, args.attempt_id, args.phase, command
            ),
        }
    return emit(result)


if __name__ == "__main__":
    raise SystemExit(main())
