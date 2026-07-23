from pathlib import Path
import shutil
import sqlite3

import yaml

from robert_agent.resource_files import resource


def plan_legacy_migration(source_dir, target_dir):
    source = Path(source_dir).expanduser()
    target = Path(target_dir).expanduser()
    config = source / "config.yml"
    database = source / "dd.sqlite3"
    missing = [str(path) for path in [config, database] if not path.exists()]
    return {
        "ok": not missing and not target.exists(),
        "source": str(source),
        "target": str(target),
        "missing": missing,
        "target_exists": target.exists(),
    }


def _convert_workers(raw_workers):
    if not isinstance(raw_workers, list):
        return raw_workers
    workers = {}
    for raw_worker in raw_workers:
        if not isinstance(raw_worker, dict):
            continue
        name = str(raw_worker.get("name", "")).strip()
        if not name:
            continue
        workers[name] = {
            "adapter": raw_worker.get("agent", ""),
            "command": raw_worker.get("command", ""),
            "model": raw_worker.get("default_model", ""),
            "effort": raw_worker.get("default_effort", ""),
        }
    return workers


def _convert_repos(raw_repos):
    repos = []
    for raw_repo in raw_repos or []:
        if not isinstance(raw_repo, dict):
            continue
        repo = {
            "full_name": raw_repo.get("full_name", ""),
            "checkout": raw_repo.get("repo_root", ""),
            "worktrees": raw_repo.get("worktree_root", ""),
            "default_branch": raw_repo.get(
                "default_base_branch",
                "main",
            ),
            "trusted_actors": list(raw_repo.get("trusted_actors") or []),
        }
        if raw_repo.get("dd_account"):
            repo["github_account"] = raw_repo["dd_account"]
        if raw_repo.get("max_concurrency") is not None:
            repo["max_concurrency"] = raw_repo["max_concurrency"]
        repos.append(repo)
    return repos


def _convert_config(source_path, target_path):
    legacy = yaml.safe_load(source_path.read_text(encoding="utf-8"))
    if not isinstance(legacy, dict):
        raise ValueError("legacy config root must be a mapping")
    github_account = legacy.pop("dd_account", "")
    legacy["version"] = 1
    legacy["data_dir"] = str(target_path.parent)
    legacy["database"] = "robert.sqlite3"
    legacy["github"] = {
        "account": github_account,
        "poll_seconds": 300,
    }
    legacy["workers"] = _convert_workers(legacy.get("workers"))
    legacy["repos"] = _convert_repos(legacy.get("repos"))
    legacy.setdefault("skills", {"search_paths": []})
    route_worker_models = legacy.get("route_worker_models") or {}
    legacy["routes"] = {
        route_id: {"worker": route_config["worker"]}
        for route_id, route_config in route_worker_models.items()
        if isinstance(route_config, dict) and route_config.get("worker")
    }
    target_path.write_text(
        yaml.safe_dump(legacy, sort_keys=False),
        encoding="utf-8",
    )


def _migrate_database(path):
    migration_sql = resource(
        "db",
        "migrations",
        "001_legacy_dd_to_robert.sql",
    ).read_text(encoding="utf-8")
    with sqlite3.connect(path) as conn, conn:
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(repos)")
        }
        if "dd_account" in columns and "github_account" not in columns:
            conn.execute(
                "ALTER TABLE repos "
                "RENAME COLUMN dd_account TO github_account"
            )
        conn.executescript(migration_sql)


def migrate_legacy(source_dir, target_dir, dry_run=False):
    plan = plan_legacy_migration(source_dir, target_dir)
    if not plan["ok"]:
        return {
            **plan,
            "status": "refused",
            "safe_error": "legacy migration preflight failed",
        }
    if dry_run:
        return {**plan, "ok": True, "status": "planned"}

    source = Path(source_dir).expanduser()
    target = Path(target_dir).expanduser()
    backup = target.parent / f"{target.name}.legacy-backup"
    if backup.exists():
        return {
            **plan,
            "ok": False,
            "status": "refused",
            "backup": str(backup),
            "safe_error": f"legacy backup already exists: {backup}",
        }
    shutil.copytree(source, backup)
    try:
        target.mkdir(parents=True)
        _convert_config(source / "config.yml", target / "config.yml")
        shutil.copy2(source / "dd.sqlite3", target / "robert.sqlite3")
        _migrate_database(target / "robert.sqlite3")
    except Exception as exc:
        shutil.rmtree(target, ignore_errors=True)
        return {
            "ok": False,
            "status": "failed",
            "source": str(source),
            "target": str(target),
            "backup": str(backup),
            "safe_error": str(exc),
        }
    return {
        "ok": True,
        "status": "migrated",
        "source": str(source),
        "target": str(target),
        "backup": str(backup),
    }
