"""Validate v2 configuration and prepare runtime settings."""

import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit
from robert_agent.foundation.timestamps import utc_timestamp

import yaml

from robert_agent import worker_adapters
from robert_agent.configuration import supervision as supervision_policy
from robert_agent.configuration import tapd as tapd_config
from robert_agent.configuration.loading import load_config
from robert_agent.configuration.routes import load_route_policies, resolve_route_config
from robert_agent.configuration.schema import apply_schema
from robert_agent.resource_files import resource


def _workers(config):
    result = []
    credentials = {
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
    }
    for name, raw in config["workers"].items():
        if name != name.strip():
            raise ValueError(f"workers.{name} name must not contain surrounding whitespace")
        if credentials.intersection(name.strip() for name in raw["environment_allowlist"]):
            raise ValueError(f"workers.{name}.environment_allowlist must not include GitHub credentials")
        normalized = worker_adapters.normalize_worker_agent_config(
            {
                "worker_agent": raw["adapter"],
                "worker_command": raw["command"],
                "timeout_seconds": raw["timeout_seconds"],
                "environment_allowlist": raw["environment_allowlist"],
            }
        )
        result.append(
            {
                "name": name,
                **normalized,
                "default_model": raw["model"].strip(),
                "default_effort": raw["effort"].strip(),
                "fallback_model": raw["fallback_model"].strip() if "fallback_model" in raw else None,
            }
        )
    return result


def _normalize(config, path, skip_external):
    schema = json.loads(resource("schemas/config.schema.json").read_text(encoding="utf-8"))
    config = apply_schema(config, schema, schema, "config")
    if any(not path.strip() for path in config["skills"]["search_paths"]):
        raise ValueError("skills.search_paths must contain non-empty paths")
    data_dir = Path(config["data_dir"]).expanduser()
    database = config["database"].strip()
    if Path(database).name != database or database in {".", ".."}:
        raise ValueError(f"database must be a filename, not a path: {database}")
    if data_dir.exists() and not data_dir.is_dir():
        raise ValueError(f"data_dir is not a directory: {data_dir}")
    config["github"]["account"] = config["github"]["account"].strip()
    workers = _workers(config)
    by_name = {w["name"]: w for w in workers}
    if config["default_worker"] not in by_name:
        raise ValueError("default_worker must reference a configured worker")
    default_worker = by_name[config["default_worker"]]
    scheduler = config["scheduler"]
    supervision = config["supervision"]
    daemon = config["daemon"]
    polling = config["github"]["polling"]
    result = {
        "ok": True,
        "status": "valid",
        "version": 2,
        "config_path": str(path),
        "data_dir": str(data_dir),
        "db_path": str(data_dir / database),
        "github": config["github"],
        "github_account": config["github"]["account"],
        "workers": workers,
        "default_worker": default_worker,
        "worker_agent": default_worker["agent"],
        "worker_command": default_worker["command"],
        "worker_agent_config": default_worker,
        "python_bin": config.get("python_bin", sys.executable or "python3").strip(),
        "max_concurrency": scheduler["max_concurrency"],
        "model_max_concurrency": scheduler["model_max_concurrency"],
        "max_dispatches": scheduler["max_dispatches"],
        "max_publish_actions": scheduler["max_publish_actions"],
        "lease_ttl_seconds": scheduler["lease_ttl_seconds"],
        "heartbeat_stale_seconds": supervision["heartbeat_stale_seconds"],
        "hard_timeout_seconds": supervision["hard_timeout_seconds"],
        "worker_startup_grace_seconds": supervision["startup_grace_seconds"],
        "routes": config["routes"],
        "repos": [],
        "skills": {"search_paths": [str(Path(p).expanduser()) for p in config["skills"]["search_paths"]]},
        "maintenance": config["maintenance"],
        "messaging": config["messaging"],
        "supervision": supervision_policy.normalize(
            {key: value for key, value in supervision.items() if key in supervision_policy.DEFAULTS}
        ),
        # Runtime control-loop names; only the schema above defines accepted YAML.
        "daemon": {
            "enabled": daemon["enabled"],
            "run_on_start": daemon["scan_on_start"],
            "local_poll_seconds": scheduler["local_poll_seconds"],
            "github_poll_seconds": polling["fallback_seconds"],
            "github_poll_when_full_seconds": polling["synchronous"]["full_capacity_seconds"],
            "rate_limit_cache_seconds": polling["synchronous"]["rate_limit_cache_seconds"],
            **config["github"]["rate_limit"],
            "live_run_timeout_seconds": daemon["control_timeouts"]["github_scan_seconds"],
            "local_drain_timeout_seconds": daemon["control_timeouts"]["local_cycle_seconds"],
        },
        "skip_external": bool(skip_external),
    }
    known_routes = {p["id"]: p for p in load_route_policies()}
    seen = set()
    for index, raw in enumerate(config["repos"]):
        name = raw["full_name"].strip()
        if name in seen:
            raise ValueError(f"duplicate repo full_name: {name}")
        seen.add(name)
        if name == "OWNER/REPO" or "your-github-login" in raw["trusted_actors"]:
            raise ValueError(f"repos[{index}] contains placeholder values")
        checkout = Path(raw["checkout"]).expanduser()
        if not checkout.exists() or not (checkout / ".git").exists():
            raise ValueError(f"repos[{index}].checkout is not a git checkout: {checkout}")
        limit = raw.get("max_concurrency", scheduler["max_concurrency"])
        if limit > scheduler["max_concurrency"]:
            raise ValueError(f"repos[{index}].max_concurrency exceeds global max_concurrency")
        ci = raw["pr_automation"]["ci"]
        if ci["enabled"] and not (ci["check_allowlist"] or ci["workflow_path_allowlist"]):
            raise ValueError(
                f"repos[{index}].pr_automation.ci.check_allowlist or workflow_path_allowlist must not be empty when enabled"
            )
        for field in ("check_allowlist", "workflow_path_allowlist"):
            ci[field] = [entry.strip() for entry in ci[field]]
            if len(ci[field]) != len(set(ci[field])):
                raise ValueError(f"repos[{index}].pr_automation.ci.{field} entries must be unique")
        result["repos"].append(
            {
                **raw,
                "full_name": name,
                "repo_root": str(checkout),
                "worktree_root": str(Path(raw["worktrees"]).expanduser()),
                "default_base_branch": raw["default_branch"],
                "max_concurrency": limit,
                "github_account": raw.get("github_account", config["github"]["account"]).strip(),
            }
        )
    for label, routes in [
        ("routes", config["routes"]),
        *[(f"repos[{i}].routes", r.get("routes", {})) for i, r in enumerate(result["repos"])],
    ]:
        unknown = set(routes) - set(known_routes)
        if unknown:
            raise ValueError(f"{label} contains unknown route: {sorted(unknown)[0]}")
        # Validate raw references even when repository overrides shadow them.
        # Native first-worker rules apply to the actual merged repository route.
        for route_id, override in routes.items():
            names = [override["worker"]] if "worker" in override else []
            for field, allowed_route in (
                ("multi_agent_review", "review-pr"),
                ("multi_agent_vote", "vote"),
            ):
                if field in override:
                    if route_id != allowed_route:
                        raise ValueError(f"{field} is only supported by {allowed_route}")
                    names.extend(override[field].get("workers", []))
            for name in names:
                if name not in by_name:
                    raise ValueError(f"{label}.{route_id} references unknown worker: {name}")
    for repo in result["repos"]:
        for policy in known_routes.values():
            resolve_route_config(result, repo, policy)
    messaging = result["messaging"]
    if "send_after" in messaging:
        start = utc_timestamp(messaging["send_after"])
        if start is None:
            raise ValueError("messaging.send_after must be an ISO timestamp with an explicit timezone")
        messaging["send_after"] = start.isoformat()
    if messaging.get("dispatch_enabled") and not messaging["enabled"]:
        raise ValueError("messaging.dispatch_enabled requires messaging.enabled")
    if messaging.get("contextual_replies") and not messaging["enabled"]:
        raise ValueError("messaging.contextual_replies requires messaging.enabled")
    if receipts := messaging.get("receipts"):
        if receipts["enabled"] and not messaging["enabled"]:
            raise ValueError("messaging.receipts.enabled requires messaging.enabled")
        if receipts["enabled"] or "send_after" in receipts:
            start = utc_timestamp(receipts.get("send_after"))
            if start is None:
                raise ValueError("messaging.receipts.send_after must be an ISO timestamp with an explicit timezone")
            receipts["send_after"] = start.isoformat()
    if messaging["enabled"]:
        for field in ("channel", "account", "target", "conversation", "allowed_senders", "repos", "send_after"):
            if not messaging.get(field):
                raise ValueError(f"messaging.{field} is required when enabled")
        for field in ("channel", "account", "target", "conversation"):
            if messaging[field] != messaging[field].strip():
                raise ValueError(f"messaging.{field} must not contain surrounding whitespace")
        if any(value != value.strip() for value in messaging["allowed_senders"]):
            raise ValueError("messaging.allowed_senders must use exact sender IDs")
        if set(messaging["repos"]) - {repo["full_name"] for repo in result["repos"]}:
            raise ValueError("messaging.repos must reference configured repositories")
    result["tapd"] = tapd_config.normalize(config["tapd"], repositories={repo["full_name"] for repo in result["repos"]})
    result["proposer"] = _proposer(config["proposer"], result)
    return result


def _proposal_schedule(setting, config):
    schedule = setting["schedule"]
    if not schedule["enabled"]:
        return
    if not setting["enabled"]:
        raise ValueError("proposer.schedule requires proposer.enabled")
    analysis_time, digest_time = schedule["analysis_time"], schedule["digest_time"]
    if not all(re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value) for value in (analysis_time, digest_time)):
        raise ValueError("proposer schedule times must be HH:MM in Asia/Shanghai")
    if analysis_time >= digest_time:
        raise ValueError("proposer digest must be scheduled after analysis")
    worker = next((worker for worker in config["workers"] if worker["name"] == schedule.get("worker")), None)
    if not worker or not worker["default_model"]:
        raise ValueError("proposer.schedule.worker requires a configured worker with a model")
    setting["model"] = worker["default_model"]
    url = schedule.get("web_url", "")
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or any(ord(char) <= 32 or char in "<>\\\"'" for char in url)
        or not parsed.netloc.isascii()
    ):
        raise ValueError("proposer.schedule.web_url must be a credential-free Web origin")
    _ = parsed.port
    schedule["web_url"] = url.rstrip("/")
    if not config["messaging"]["enabled"]:
        raise ValueError("proposer.schedule requires operator messaging")
    if setting["tapd_projects"]:
        if len(setting["tapd_projects"]) != 1:
            raise ValueError("proposer.schedule expects one TAPD gateway project")
        for kind in ("story", "bug"):
            waiting = setting["tapd_todo_states"][kind]
            started = setting["tapd_started_states"].get(kind)
            if not waiting or not started or started in waiting:
                raise ValueError("proposer TAPD requires distinct to-do and started states for story and bug")


def _proposer(setting, config):
    known = {repo["full_name"] for repo in config["repos"]}
    if setting["github_all_registered"]:
        setting["github_repositories"] = sorted(known)
    github = set(setting["github_repositories"])
    _proposal_schedule(setting, config)
    tapd = set(setting["tapd_projects"])
    known_tapd = {f"{project['host']}/{project['workspace_id']}" for project in config["tapd"]["projects"]}
    if github - known or tapd - known_tapd:
        raise ValueError("proposer sources must reference configured GitHub repositories and TAPD projects")
    if setting["enabled"]:
        if not (github or tapd) or not setting.get("gateway_metadata") or not setting.get("model"):
            raise ValueError("proposer requires configured sources, gateway_metadata and model")
        if tapd and not config["tapd"]["enabled"]:
            raise ValueError("proposer.tapd_projects requires TAPD integration")
        setting["gateway_metadata"] = str(Path(setting["gateway_metadata"]).expanduser())
    for field in ("github_assignees", "tapd_assignees"):
        if any(value != value.strip() for value in setting[field]):
            raise ValueError(f"proposer.{field} must use exact account names")
    setting["github_assignees"] = [login.casefold() for login in setting["github_assignees"]]
    return setting


def validate_config(config_path, skip_external=False):
    try:
        return _normalize(load_config(config_path), config_path, skip_external)
    except (KeyError, OSError, TypeError, ValueError, yaml.YAMLError) as exc:
        return {
            "ok": False,
            "status": "failed_config",
            "safe_error": f"invalid config: {exc}",
        }
