"""Worker snapshot command-line boundary."""
import argparse
from robert_agent.execution import progress as implementation
from robert_agent.foundation.commands import emit


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--db")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--status", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--next-step", default="")
    args = parser.parse_args(argv)
    if args.db:
        result = implementation.record_snapshot(
            db_path=args.db,
            task_id=args.task_id,
            attempt_id=args.attempt_id,
            phase=args.phase,
            status=args.status,
            summary=args.summary,
            next_step=args.next_step,
        )
    else:
        result = {"ok": True, "status": "built", "snapshot": implementation.build_snapshot(
            task_id=args.task_id,
            attempt_id=args.attempt_id,
            phase=args.phase,
            status=args.status,
            summary=args.summary,
            next_step=args.next_step,
        )}
    return emit(result)


if __name__ == "__main__":
    raise SystemExit(main())
