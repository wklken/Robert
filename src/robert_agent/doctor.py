from pathlib import Path
import shutil
import subprocess

from robert_agent import validate_config
from robert_agent.route_config import (
    load_route_policies,
    resolve_route_config,
)
from robert_agent.skills import discover_skill_names, route_skill_status


def doctor(config_path, skip_external=False):
    validated = validate_config.validate_config(
        config_path,
        skip_external=skip_external,
    )
    checks = {
        "config": {
            "status": "passed" if validated.get("ok") else "failed",
        }
    }
    if not validated.get("ok"):
        return {
            "ok": False,
            "status": "failed",
            "checks": checks,
            "safe_error": validated.get("safe_error"),
        }

    worker = validated["default_worker"]
    worker_command = worker["command_argv"][0]
    worker_ok = shutil.which(worker_command) is not None
    checks["worker"] = {
        "status": "passed" if worker_ok else "failed",
        "command": worker_command,
    }

    gh_path = shutil.which("gh")
    gh_ok = skip_external or bool(gh_path)
    if gh_path and not skip_external:
        gh_ok = subprocess.run(
            ["gh", "auth", "status"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        ).returncode == 0
    checks["github_cli"] = {
        "status": "passed" if gh_ok else "failed",
    }

    repos_ok = all(
        Path(repo["repo_root"]).exists()
        for repo in validated["repos"]
    )
    checks["repositories"] = {
        "status": "passed" if repos_ok else "failed",
    }

    installed = discover_skill_names(
        validated["skills"]["search_paths"]
    )
    route_checks = []
    for repo in validated["repos"]:
        for policy in load_route_policies():
            route = resolve_route_config(validated, repo, policy)
            skill_status = route_skill_status(
                required=route["required_skills"],
                recommended=route["recommended_skills"],
                installed=installed,
            )
            route_checks.append(
                {
                    "repo": repo["full_name"],
                    "route": route["id"],
                    **skill_status,
                }
            )
    checks["skills"] = {
        "status": (
            "passed"
            if all(item["runnable"] for item in route_checks)
            else "failed"
        ),
        "routes": route_checks,
    }

    ok = all(check["status"] == "passed" for check in checks.values())
    return {
        "ok": ok,
        "status": "ready" if ok else "failed",
        "checks": checks,
    }
