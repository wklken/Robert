#!/usr/bin/env python3
import argparse
import subprocess

from robert_agent.common import emit
from robert_agent import discover
from robert_agent import validate_config


def _sample_events(events, limit=5):
    samples = []
    for event in events[:limit]:
        samples.append(
            {
                "event_fingerprint": event.get("event_fingerprint"),
                "source_key": event.get("source_key"),
                "source_type": event.get("source_type"),
                "event_type": event.get("event_type"),
                "actor_login": event.get("actor_login"),
                "mentions_dd": bool(event.get("mentions_dd")),
                "has_open_dd_pr": bool(event.get("has_open_dd_pr")),
                "intent": event.get("intent"),
            }
        )
    return samples


def live_discovery_acceptance(config_path, runner=subprocess.run, limit=30):
    config = validate_config.validate_config(config_path, skip_external=True)
    if not config["ok"]:
        return config
    repo = config["repos"][0]
    try:
        raw_events = discover.collect_live_events(repo, runner=runner, limit=limit)
    except subprocess.CalledProcessError as exc:
        command = exc.cmd if isinstance(exc.cmd, list) else [exc.cmd]
        return {
            "ok": False,
            "status": "failed_discovery",
            "safe_error": f"GitHub discovery command failed with exit {exc.returncode}",
            "command": [str(part) for part in command],
        }
    normalized = discover.normalize_events(raw_events, repo)
    return {
        "ok": True,
        "status": "completed",
        "read_only": True,
        "repo": repo["full_name"],
        "github_account": repo["github_account"],
        "raw_event_count": len(raw_events),
        "normalized_event_count": len(normalized),
        "sample_events": _sample_events(normalized),
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args(argv)
    result = live_discovery_acceptance(args.config, limit=args.limit)
    if result.get("ok"):
        return emit(result, 0)
    if result.get("status") == "failed_config":
        return emit(result, 3)
    return emit(result, 2)


if __name__ == "__main__":
    raise SystemExit(main())
