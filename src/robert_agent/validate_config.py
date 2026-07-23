#!/usr/bin/env python3
import argparse
from pathlib import Path

import yaml

from robert_agent import worker_adapters
from robert_agent.common import emit
from robert_agent.route_config import (
    load_route_policies,
    resolve_route_config,
)


def load_config(path):
    config_path = Path(path).expanduser()
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("config root must be a mapping")
    return data


def _normalize_workers(config):
    raw_workers = config.get("workers")
    legacy_fields = {"worker_agent", "worker_command"} & set(config)
    if raw_workers not in (None, "") and legacy_fields:
        raise ValueError(
            "workers cannot be combined with legacy fields: "
            f"{', '.join(sorted(legacy_fields))}"
        )

    if raw_workers in (None, ""):
        worker_config = worker_adapters.normalize_worker_agent_config(config)
        return [
            {
                "name": "default",
                **worker_config,
                "default_model": "gpt-5.4",
                "default_effort": "high",
            }
        ]
    if isinstance(raw_workers, dict):
        normalized = []
        for name, raw_worker in raw_workers.items():
            if not isinstance(raw_worker, dict):
                raise ValueError(f"workers.{name} must be a mapping")
            unsupported_fields = sorted(
                set(raw_worker)
                - {
                    "adapter",
                    "command",
                    "model",
                    "effort",
                    "prompt_transport",
                    "timeout_seconds",
                    "environment_allowlist",
                }
            )
            if unsupported_fields:
                raise ValueError(
                    f"workers.{name} contains unsupported fields: "
                    f"{', '.join(unsupported_fields)}"
                )
            worker_name = str(name).strip()
            if not worker_name:
                raise ValueError("worker name must not be empty")
            command = raw_worker.get("command", "")
            default_model = str(raw_worker.get("model", "")).strip()
            default_effort = str(raw_worker.get("effort", "")).strip()
            if not str(command).strip():
                raise ValueError(f"workers.{name}.command must not be empty")
            if not default_model:
                raise ValueError(f"workers.{name}.model must not be empty")
            if not default_effort:
                raise ValueError(f"workers.{name}.effort must not be empty")
            worker = worker_adapters.normalize_worker_agent_config(
                {
                    "worker_agent": raw_worker.get("adapter", ""),
                    "worker_command": command,
                    "prompt_transport": raw_worker.get(
                        "prompt_transport",
                        "stdin",
                    ),
                    "timeout_seconds": raw_worker.get(
                        "timeout_seconds",
                        5400,
                    ),
                    "environment_allowlist": raw_worker.get(
                        "environment_allowlist",
                        [],
                    ),
                }
            )
            normalized.append(
                {
                    "name": worker_name,
                    **worker,
                    "default_model": default_model,
                    "default_effort": default_effort,
                }
            )
        if not normalized:
            raise ValueError("workers must contain at least one named worker")
        return normalized
    if not isinstance(raw_workers, list) or not raw_workers:
        raise ValueError("workers must contain at least one worker")

    normalized = []
    seen_names = set()
    for index, raw_worker in enumerate(raw_workers):
        if not isinstance(raw_worker, dict):
            raise ValueError(f"workers[{index}] must be a mapping")
        unsupported_fields = sorted(
            set(raw_worker)
            - {
                "name",
                "agent",
                "command",
                "default_model",
                "default_effort",
                "prompt_transport",
                "timeout_seconds",
                "environment_allowlist",
            }
        )
        if unsupported_fields:
            raise ValueError(
                f"workers[{index}] contains unsupported fields: "
                f"{', '.join(unsupported_fields)}"
            )
        name = str(raw_worker.get("name", "")).strip()
        if not name:
            raise ValueError(f"workers[{index}].name must not be empty")
        if name in seen_names:
            raise ValueError(f"duplicate worker name: {name}")
        seen_names.add(name)

        default_model = str(raw_worker.get("default_model", "")).strip()
        default_effort = str(raw_worker.get("default_effort", "")).strip()
        command = raw_worker.get("command", "")
        if not str(command).strip():
            raise ValueError(f"workers[{index}].command must not be empty")
        if not default_model:
            raise ValueError(f"workers[{index}].default_model must not be empty")
        if not default_effort:
            raise ValueError(f"workers[{index}].default_effort must not be empty")

        adapter_config = {
            "worker_agent": raw_worker.get("agent", ""),
            "worker_command": command,
            "prompt_transport": raw_worker.get(
                "prompt_transport",
                "stdin",
            ),
            "timeout_seconds": raw_worker.get(
                "timeout_seconds",
                5400,
            ),
            "environment_allowlist": raw_worker.get(
                "environment_allowlist",
                [],
            ),
        }
        worker_config = worker_adapters.normalize_worker_agent_config(adapter_config)
        normalized.append(
            {
                "name": name,
                **worker_config,
                "default_model": default_model,
                "default_effort": default_effort,
            }
        )
    return normalized


def _normalize_route_worker_models(config, workers):
    raw = config.get("route_worker_models", {})
    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict):
        raise ValueError("route_worker_models must be a mapping of route id to model configuration")
    workers_by_name = {worker["name"]: worker for worker in workers}
    default_worker = workers[0]
    normalized = {}
    for route_id, route_config in raw.items():
        route_id = str(route_id).strip()
        if not route_id:
            raise ValueError("route_worker_models route id must not be empty")
        if not isinstance(route_config, dict):
            raise ValueError(
                f"route_worker_models.{route_id} must be a worker configuration"
            )
        unsupported_fields = sorted(set(route_config) - {"worker", "model", "effort"})
        if unsupported_fields:
            raise ValueError(
                f"route_worker_models.{route_id} contains unsupported fields: "
                f"{', '.join(unsupported_fields)}"
            )
        worker_name = str(route_config.get("worker", default_worker["name"])).strip()
        if not worker_name:
            raise ValueError(f"route_worker_models.{route_id}.worker must not be empty")
        worker = workers_by_name.get(worker_name)
        if worker is None:
            raise ValueError(
                f"route_worker_models.{route_id} references unknown worker: {worker_name}"
            )
        model = str(route_config.get("model", worker["default_model"])).strip()
        effort = str(route_config.get("effort", worker["default_effort"])).strip()
        if not model:
            raise ValueError(f"route_worker_models.{route_id}.model must not be empty")
        if not effort:
            raise ValueError(f"route_worker_models.{route_id}.effort must not be empty")
        normalized[route_id] = {
            "worker": worker_name,
            "model": model,
            "effort": effort,
        }
    return normalized


DAEMON_DEFAULTS = {
    "enabled": True,
    "local_poll_seconds": 5,
    "github_poll_seconds": 300,
    "github_poll_when_full_seconds": 600,
    "rate_limit_cache_seconds": 300,
    "min_search_remaining": 10,
    "min_core_remaining": 500,
    "live_run_timeout_seconds": 300,
    "local_drain_timeout_seconds": 180,
    "event_retention_days": 7,
    "run_on_start": False,
}


def _normalize_daemon_config(config):
    daemon = {
        "enabled": bool(config.get("daemon_enabled", DAEMON_DEFAULTS["enabled"])),
        "local_poll_seconds": int(config.get("daemon_local_poll_seconds", DAEMON_DEFAULTS["local_poll_seconds"])),
        "github_poll_seconds": int(config.get("daemon_github_poll_seconds", DAEMON_DEFAULTS["github_poll_seconds"])),
        "github_poll_when_full_seconds": int(config.get("daemon_github_poll_when_full_seconds", DAEMON_DEFAULTS["github_poll_when_full_seconds"])),
        "rate_limit_cache_seconds": int(config.get("daemon_rate_limit_cache_seconds", DAEMON_DEFAULTS["rate_limit_cache_seconds"])),
        "min_search_remaining": int(config.get("daemon_min_search_remaining", DAEMON_DEFAULTS["min_search_remaining"])),
        "min_core_remaining": int(config.get("daemon_min_core_remaining", DAEMON_DEFAULTS["min_core_remaining"])),
        "live_run_timeout_seconds": int(config.get("daemon_live_run_timeout_seconds", DAEMON_DEFAULTS["live_run_timeout_seconds"])),
        "local_drain_timeout_seconds": int(config.get("daemon_local_drain_timeout_seconds", DAEMON_DEFAULTS["local_drain_timeout_seconds"])),
        "event_retention_days": int(config.get("daemon_event_retention_days", DAEMON_DEFAULTS["event_retention_days"])),
        "run_on_start": bool(config.get("daemon_run_on_start", DAEMON_DEFAULTS["run_on_start"])),
    }
    for key, value in daemon.items():
        if isinstance(value, bool):
            continue
        if value < 1:
            raise ValueError(f"daemon_{key} must be at least 1")
    return daemon


def _placeholder_fields(repo):
    placeholders = {
        "full_name": {"OWNER/REPO"},
        "github_account": {"robot-user"},
        "repo_root": {"/absolute/path/to/local/repo"},
        "worktree_root": {"/absolute/path/to/local/repo/.worktrees"},
    }
    fields = [
        key
        for key, values in placeholders.items()
        if str(repo.get(key, "")) in values
    ]
    trusted_actors = repo.get("trusted_actors") or []
    if "your-github-login" in trusted_actors:
        fields.append("trusted_actors")
    return fields


def validate_config(config_path, skip_external=False):
    try:
        config = load_config(config_path)
        public_config = "version" in config
        if public_config and config.get("version") != 1:
            raise ValueError("version must be 1")
        github_config = config.get("github")
        if public_config:
            if not isinstance(github_config, dict):
                raise ValueError("github must be a mapping")
            unsupported_github = sorted(
                set(github_config) - {"account", "poll_seconds"}
            )
            if unsupported_github:
                raise ValueError(
                    "unsupported github fields: "
                    + ", ".join(
                        f"github.{name}"
                        for name in unsupported_github
                    )
                )
            github_account = str(github_config.get("account", "")).strip()
            github_poll_seconds = int(github_config.get("poll_seconds", 300))
            if github_poll_seconds < 1:
                raise ValueError("github.poll_seconds must be at least 1")
            if "github_account" in config:
                raise ValueError(
                    "unsupported public config field: github_account"
                )
        else:
            github_account = str(config.get("github_account", "")).strip()
            github_poll_seconds = 300
        data_dir = Path(str(config["data_dir"])).expanduser()
        database = (
            ""
            if config["database"] is None
            else str(config["database"]).strip()
        )
        repos = config["repos"]
        stale_after_minutes = int(config.get("stale_after_minutes", 20))
        hard_timeout_minutes = int(config.get("hard_timeout_minutes", 90))
        worker_startup_grace_seconds = int(config.get("worker_startup_grace_seconds", 300))
        lease_ttl_minutes = int(config.get("lease_ttl_minutes", 9))
        max_concurrency = int(config.get("max_concurrency", 3))
        python_bin_value = config.get("python_bin", "python3")
        python_bin = (
            ""
            if python_bin_value is None
            else str(python_bin_value).strip()
        )
        workers = _normalize_workers(config)
        default_worker = workers[0]
        route_worker_models = _normalize_route_worker_models(config, workers)
        daemon_config = _normalize_daemon_config(config)
        skills_config = config.get("skills", {"search_paths": []})
        if not isinstance(skills_config, dict):
            raise ValueError("skills must be a mapping")
        skill_search_paths = skills_config.get("search_paths", [])
        if not isinstance(skill_search_paths, list):
            raise ValueError("skills.search_paths must be a list")
        routes_config = config.get("routes", {})
        if not isinstance(routes_config, dict):
            raise ValueError("routes must be a mapping")
    except (KeyError, OSError, TypeError, ValueError, yaml.YAMLError) as exc:
        error = str(exc)
        safe_error = (
            error
            if error.startswith(
                (
                    "unsupported worker_agent:",
                    "worker_agent must ",
                    "worker_command must ",
                )
            )
            else f"invalid config: {error}"
        )
        return {
            "ok": False,
            "status": "failed_config",
            "safe_error": safe_error,
        }

    worker_agent_config = {
        "agent": default_worker["agent"],
        "command": default_worker["command"],
        "command_argv": default_worker["command_argv"],
    }
    worker_agent = default_worker["agent"]
    worker_command = default_worker["command"]

    if not python_bin:
        return {
            "ok": False,
            "status": "failed_config",
            "safe_error": "python_bin must not be empty",
        }
    if not database:
        return {
            "ok": False,
            "status": "failed_config",
            "safe_error": "database must not be empty",
        }
    if Path(database).name != database:
        return {
            "ok": False,
            "status": "failed_config",
            "safe_error": f"database must be a filename, not a path: {database}",
        }
    if data_dir.exists() and not data_dir.is_dir():
        return {
            "ok": False,
            "status": "failed_config",
            "safe_error": f"data_dir is not a directory: {data_dir}",
        }
    positive_integer_fields = {
        "max_concurrency": max_concurrency,
        "stale_after_minutes": stale_after_minutes,
        "hard_timeout_minutes": hard_timeout_minutes,
        "worker_startup_grace_seconds": worker_startup_grace_seconds,
        "lease_ttl_minutes": lease_ttl_minutes,
    }
    for field, value in positive_integer_fields.items():
        if value < 1:
            return {
                "ok": False,
                "status": "failed_config",
                "safe_error": f"{field} must be at least 1",
            }

    if not isinstance(repos, list) or not repos:
        return {
            "ok": False,
            "status": "failed_config",
            "safe_error": "repos must contain at least one repository",
        }

    seen_repo_names = set()
    normalized_repos = []
    for index, repo in enumerate(repos):
        if not isinstance(repo, dict):
            return {
                "ok": False,
                "status": "failed_config",
                "safe_error": f"repos[{index}] must be a mapping",
            }
        repo_full_name = str(repo.get("full_name", "")).strip()
        if repo_full_name in seen_repo_names:
            return {
                "ok": False,
                "status": "failed_config",
                "safe_error": f"duplicate repo full_name: {repo_full_name}",
            }
        if repo_full_name:
            seen_repo_names.add(repo_full_name)

        effective_github_account = str(repo.get("github_account") or github_account).strip()
        if not effective_github_account:
            return {
                "ok": False,
                "status": "failed_config",
                "safe_error": f"repos[{index}].github_account must not be empty",
            }
        if public_config:
            unsupported_repo_fields = sorted(
                set(repo)
                & {
                    "repo_root",
                    "worktree_root",
                    "default_base_branch",
                }
            )
            if unsupported_repo_fields:
                raise ValueError(
                    f"repos[{index}] contains unsupported public fields: "
                    f"{', '.join(unsupported_repo_fields)}"
                )
            repo = {
                **repo,
                "full_name": repo_full_name,
                "github_account": effective_github_account,
                "repo_root": repo.get("checkout"),
                "worktree_root": repo.get("worktrees"),
                "default_base_branch": repo.get("default_branch"),
            }
        else:
            repo = {
                **repo,
                "full_name": repo_full_name,
                "github_account": effective_github_account,
                "repo_root": repo.get("repo_root", repo.get("checkout")),
                "worktree_root": repo.get(
                    "worktree_root",
                    repo.get("worktrees"),
                ),
                "default_base_branch": repo.get(
                    "default_base_branch",
                    repo.get("default_branch"),
                ),
            }
        trusted_actors = repo.get("trusted_actors")
        if not trusted_actors:
            return {
                "ok": False,
                "status": "failed_config",
                "safe_error": f"repos[{index}].trusted_actors must not be empty",
            }
        required_keys = [
            "full_name",
            "github_account",
            "default_base_branch",
            "repo_root",
            "worktree_root",
        ]
        missing = [key for key in required_keys if not repo.get(key)]
        if missing:
            return {
                "ok": False,
                "status": "failed_config",
                "safe_error": f"repos[{index}] missing required keys: {', '.join(missing)}",
            }

        placeholder_fields = _placeholder_fields(repo)
        if placeholder_fields:
            return {
                "ok": False,
                "status": "failed_config",
                "safe_error": (
                    f"repos[{index}] contains placeholder values: "
                    f"{', '.join(placeholder_fields)}"
                ),
            }

        repo_root = Path(str(repo["repo_root"])).expanduser()
        if not repo_root.exists() or not (repo_root / ".git").exists():
            return {
                "ok": False,
                "status": "failed_config",
                "safe_error": f"repos[{index}].repo_root is not a git checkout: {repo_root}",
            }

        worktree_root = Path(str(repo["worktree_root"])).expanduser()
        repo_max_concurrency = repo.get("max_concurrency", max_concurrency)
        if (
            isinstance(repo_max_concurrency, bool)
            or not isinstance(repo_max_concurrency, int)
            or repo_max_concurrency < 1
            or repo_max_concurrency > max_concurrency
        ):
            return {
                "ok": False,
                "status": "failed_config",
                "safe_error": (
                    f"repos[{index}].max_concurrency must be an integer between "
                    f"1 and global max_concurrency ({max_concurrency})"
                ),
            }
        normalized_repos.append(
            {
                **repo,
                "trusted_actors": list(trusted_actors),
                "repo_root": str(repo_root),
                "worktree_root": str(worktree_root),
                "max_concurrency": repo_max_concurrency,
            }
        )

    route_validation_config = {
        "workers": workers,
        "routes": routes_config,
    }
    try:
        for repo in normalized_repos:
            for policy in load_route_policies():
                route_config = resolve_route_config(
                    route_validation_config,
                    repo,
                    policy,
                )
                if route_config["worker"] not in {
                    worker["name"]
                    for worker in workers
                }:
                    raise ValueError(
                        f"route {route_config['id']} references unknown "
                        f"worker: {route_config['worker']}"
                    )
    except ValueError as exc:
        return {
            "ok": False,
            "status": "failed_config",
            "safe_error": f"invalid config: {exc}",
        }

    return {
        "ok": True,
        "status": "valid",
        "config_path": str(Path(config_path)),
        "data_dir": str(data_dir),
        "db_path": str(data_dir / database),
        "version": config.get("version"),
        "github": {
            "account": github_account,
            "poll_seconds": github_poll_seconds,
        },
        "github_account": github_account,
        "repos": normalized_repos,
        "stale_after_minutes": stale_after_minutes,
        "hard_timeout_minutes": hard_timeout_minutes,
        "worker_startup_grace_seconds": worker_startup_grace_seconds,
        "lease_ttl_minutes": lease_ttl_minutes,
        "max_concurrency": max_concurrency,
        "python_bin": python_bin,
        "worker_agent": worker_agent,
        "worker_command": worker_command,
        "worker_agent_config": worker_agent_config,
        "workers": workers,
        "default_worker": default_worker,
        "route_worker_models": route_worker_models,
        "routes": routes_config,
        "skills": {
            "search_paths": [
                str(Path(path).expanduser())
                for path in skill_search_paths
            ],
        },
        "daemon": daemon_config,
        "skip_external": bool(skip_external),
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--skip-external", action="store_true")
    args = parser.parse_args(argv)
    result = validate_config(args.config, skip_external=args.skip_external)
    return emit(result, 0 if result["ok"] else 3)


if __name__ == "__main__":
    raise SystemExit(main())
