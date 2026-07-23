#!/usr/bin/env python3
import argparse
from pathlib import Path
import shutil

import yaml

from robert_agent.common import emit
from robert_agent.paths import default_data_dir


def init_config(
    config_path,
    values=None,
    non_interactive=False,
    force=False,
):
    path = Path(config_path).expanduser()
    values = dict(values or {})
    if path.exists() and not force:
        return {
            "ok": False,
            "status": "exists",
            "config_path": str(path),
            "safe_error": f"config already exists: {path}",
        }
    required = {
        "repo",
        "repo_path",
        "worker",
        "github_account",
        "trusted_actor",
    }
    missing = sorted(required - set(values))
    if missing:
        return {
            "ok": False,
            "status": "failed_input",
            "safe_error": (
                "missing "
                + ("non-interactive " if non_interactive else "")
                + "values: "
                + ", ".join(missing)
            ),
        }

    repo_path = Path(values["repo_path"]).expanduser()
    worker_name = str(values["worker"])
    adapter = (
        worker_name
        if worker_name in {"cbc", "codex", "tcodex"}
        else "codex"
    )
    config = {
        "version": 1,
        "data_dir": str(default_data_dir()),
        "database": "robert.sqlite3",
        "github": {
            "account": values["github_account"],
            "poll_seconds": 300,
        },
        "skills": {"search_paths": []},
        "workers": {
            "default": {
                "adapter": adapter,
                "command": worker_name,
                "model": "default",
                "effort": "default",
            }
        },
        "routes": {},
        "repos": [
            {
                "full_name": values["repo"],
                "checkout": str(repo_path),
                "worktrees": str(repo_path / ".worktrees"),
                "default_branch": "main",
                "trusted_actors": [values["trusted_actor"]],
            }
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    path.write_text(
        yaml.safe_dump(config, sort_keys=False),
        encoding="utf-8",
    )
    return {
        "ok": True,
        "status": "created",
        "config_path": str(path),
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        default="~/.config/robert/config.yml",
        help="Target config path to create when missing.",
    )
    parser.add_argument("--repo", required=True)
    parser.add_argument("--repo-path", required=True)
    parser.add_argument("--worker", required=True)
    parser.add_argument("--github-account", required=True)
    parser.add_argument("--trusted-actor", required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    result = init_config(
        args.config,
        {
            "repo": args.repo,
            "repo_path": args.repo_path,
            "worker": args.worker,
            "github_account": args.github_account,
            "trusted_actor": args.trusted_actor,
        },
        non_interactive=True,
        force=args.force,
    )
    return emit(result, 0 if result["ok"] else 3)


if __name__ == "__main__":
    raise SystemExit(main())
