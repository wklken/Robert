"""Worker result command-line boundary."""

import argparse
import json
import os
from pathlib import Path
import sys
from robert_agent.delivery.application import submission as implementation
from robert_agent.foundation.commands import emit


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Submit a worker result. Read the JSON status to determine whether it was accepted."
    )
    parser.add_argument("--db", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--output-type", required=True)
    args = parser.parse_args(argv)
    try:
        submission = json.load(sys.stdin)
    except ValueError:
        return emit(
            {
                "ok": False,
                "status": "failed_validation",
                "reason_code": "invalid_json",
                "field_path": "result",
                "safe_error": "Submission must be valid JSON.",
            }
        )
    if output_dir := os.environ.get("ROBERT_PRE_PR_CANDIDATE_DIR"):
        target = Path(output_dir) / "candidate.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(submission), encoding="utf-8")
        temporary.replace(target)
        return emit({"ok": True, "status": "candidate_saved"})
    return emit(
        implementation.record_result(
            args.db,
            {
                "task_id": args.task_id,
                "attempt_id": args.attempt_id,
                "output_type": args.output_type,
                "submission": submission,
            },
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
