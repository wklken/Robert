"""Scripted output for the installed supervisor's decision command."""

import json
from pathlib import Path
import sys

from git_transport import activity


def main():
    root = Path(sys.argv[1])
    bundle = json.load(sys.stdin)
    identity = {key: bundle[key] for key in ("bundle_id", "bundle_sha256")}
    actors = root / "actors"
    actors.mkdir(exist_ok=True)
    (actors / f"decision-{identity['bundle_id']}.json").write_text(json.dumps(bundle))
    action = json.loads((root / "decision-policy.json").read_text())["action"]
    candidate = bundle["candidates"][0] if bundle["candidates"] else None
    if (
        activity(
            "decision",
            {
                **identity,
                "action": action,
                "candidate": candidate,
                "candidate_action": candidate["action"] if candidate else None,
            },
            "ready",
        )
        != "continue"
    ):
        return 1
    if action == "rate_limit":
        print(json.dumps({**identity, "error": {"code": "rate_limited", "retry_after_seconds": 10}}))
        return 0
    candidate_id = None
    if action == "candidate":
        action = candidate["action"] if candidate else "wait"
        candidate_id = candidate["id"] if candidate else None
    if action == "invent":
        action, candidate_id = "retry_worker", "nonexistent-candidate"
    message = {"ask_user": "Should the fixture keep the API?", "notify_user": "Fixture progress is pending."}
    print(
        json.dumps(
            {
                **identity,
                "action": action,
                "candidate_id": candidate_id,
                "reason": "Follow the synthetic task objective.",
                "evidence_ids": ["tasks"],
                "expected_progress": "The task produces a result",
                "recheck_seconds": 2,
                "message": message.get(action, ""),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
