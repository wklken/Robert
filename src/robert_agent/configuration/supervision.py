"""Validate supervision settings without importing execution services."""

from pathlib import Path
import re


DEFAULTS = {
    "mode": "observe",
    "poll_seconds": 60,
    "batch_size": 100,
    "stall_seconds": 300,
    "diagnosis": None,
    "recovery": None,
    "repair": None,
    "build_manifest": None,
}

RECOVERY_STAGES = {
    "spool_replay": "ingestion",
    "audit_retry": "audit",
    "handoff_resume": "handoff",
    "publication_retry": "publication",
    "publication_reconcile": "publication_reconcile",
}

_DIAGNOSIS_DEFAULTS = {
    "environment_allowlist": [],
    "timeout_seconds": 120,
    "cooldown_seconds": 600,
    "max_calls_per_hour": 6,
}
_DENIED_ENV_PREFIXES = (
    "GH_",
    "GITHUB_",
    "GIT_",
    "OPENCLAW_",
    "CODEX_",
    "CLAUDE_",
    "ROBERT_",
    "SSH_",
    "AWS_",
    "XDG_",
    "PYTHON",
    "LD_",
    "DYLD_",
)
_DENIED_ENV_NAMES = {"HOME", "PATH", "TMPDIR", "BASH_ENV", "ENV", "NODE_OPTIONS"}

REPAIR_ACTIONS = ("push_existing_pr", "open_pr")
_REPAIR_DEFAULTS: dict[str, object] = {
    "mode": "backlog",
    "worker": None,
    "allowed_actions": [],
    "launcher": [],
    "verification_commands": [],
    "max_repairs_per_day": 3,
}


def normalize_diagnosis(raw):
    """Validate an opt-in administrator-configured no-tool command profile."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) - (
        {"command"} | _DIAGNOSIS_DEFAULTS.keys()
    ):
        raise ValueError(
            "supervision.diagnosis must contain only the supported profile fields"
        )
    argv = raw.get("command")
    if (
        not isinstance(argv, list)
        or not 1 <= len(argv) <= 32
        or any(
            not isinstance(arg, str)
            or not arg.strip()
            or "\x00" in arg
            or len(arg) > 8192
            for arg in argv
        )
        or not Path(argv[0]).is_absolute()
    ):
        raise ValueError(
            "supervision.diagnosis.command requires an argv array with an absolute executable"
        )
    profile = {**_DIAGNOSIS_DEFAULTS, **raw}
    allowlist = profile["environment_allowlist"]
    if (
        not isinstance(allowlist, list)
        or len(allowlist) > 32
        or any(
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,63}", name)
            or name in _DENIED_ENV_NAMES
            or name.startswith(_DENIED_ENV_PREFIXES)
            for name in allowlist
        )
    ):
        raise ValueError(
            "supervision.diagnosis.environment_allowlist contains a prohibited or invalid variable name"
        )
    for field, maximum in (
        ("timeout_seconds", 300),
        ("cooldown_seconds", 86400),
        ("max_calls_per_hour", 60),
    ):
        value = profile[field]
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(
                f"supervision.diagnosis.{field} must be an integer between 1 and {maximum}"
            )
    return {
        **profile,
        "command": list(argv),
        "environment_allowlist": list(dict.fromkeys(allowlist)),
    }


def is_repair_argv(value):
    return (
        isinstance(value, list)
        and 1 <= len(value) <= 32
        and all(
            isinstance(arg, str)
            and arg.strip()
            and "\x00" not in arg
            and len(arg) <= 4096
            for arg in value
        )
        and Path(value[0]).is_absolute()
    )


def normalize_repair(raw):
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) - (
        {"repository"} | _REPAIR_DEFAULTS.keys()
    ):
        raise ValueError(
            "supervision.repair must contain only supported maintenance fields"
        )
    policy = {**_REPAIR_DEFAULTS, **raw}
    repository = policy.get("repository")
    if not isinstance(repository, str) or not re.fullmatch(
        r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", repository
    ):
        raise ValueError(
            "supervision.repair.repository must be a configured owner/repository"
        )
    if policy["mode"] not in ("backlog", "auto"):
        raise ValueError("supervision.repair.mode must be backlog or auto")
    worker = policy["worker"]
    if worker is not None and (
        not isinstance(worker, str) or not worker.strip() or len(worker) > 120
    ):
        raise ValueError(
            "supervision.repair.worker must name one configured command worker"
        )
    actions = policy["allowed_actions"]
    if not isinstance(actions, list) or any(
        action not in REPAIR_ACTIONS for action in actions
    ):
        raise ValueError(
            "supervision.repair.allowed_actions only supports push_existing_pr and open_pr"
        )
    launcher = policy["launcher"]
    if launcher != [] and not is_repair_argv(launcher):
        raise ValueError(
            "supervision.repair.launcher must be an argv array with an absolute executable"
        )
    commands = policy["verification_commands"]
    if (
        not isinstance(commands, list)
        or len(commands) > 8
        or any(not is_repair_argv(argv) for argv in commands)
    ):
        raise ValueError(
            "supervision.repair.verification_commands must contain bounded absolute argv arrays"
        )
    limit = policy["max_repairs_per_day"]
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError(
            "supervision.repair.max_repairs_per_day must be an integer between 1 and 20"
        )
    return {
        **policy,
        "repository": repository.casefold(),
        "allowed_actions": [action for action in REPAIR_ACTIONS if action in actions],
    }


def normalize(raw):
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("supervision must be a mapping")
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        raise ValueError(
            "unsupported supervision fields: " + ", ".join(sorted(unknown))
        )
    result = {**DEFAULTS, **raw}
    if result["mode"] not in ("off", "observe", "recover_known"):
        raise ValueError("supervision.mode must be off, observe or recover_known")
    for key, maximum in (
        ("poll_seconds", 3600),
        ("batch_size", 500),
        ("stall_seconds", 86400),
    ):
        if type(result[key]) is not int or not 1 <= result[key] <= maximum:
            raise ValueError(
                f"supervision.{key} must be an integer between 1 and {maximum}"
            )
    result["diagnosis"] = normalize_diagnosis(result["diagnosis"])
    recovery = result["recovery"]
    if recovery is not None:
        if not isinstance(recovery, dict) or set(recovery) != {
            "allowed_actions",
            "repositories",
        }:
            raise ValueError(
                "supervision.recovery requires allowed_actions and repositories"
            )
        for field in ("allowed_actions", "repositories"):
            values = recovery[field]
            if (
                not isinstance(values, list)
                or not values
                or len(values) > 100
                or any(not isinstance(v, str) or not v for v in values)
            ):
                raise ValueError(
                    "supervision.recovery lists must contain bounded nonempty strings"
                )
        if set(recovery["allowed_actions"]) - set(RECOVERY_STAGES):
            raise ValueError("supervision.recovery contains an unsupported action")
        result["recovery"] = {
            key: sorted(set(value)) for key, value in recovery.items()
        }
    result["repair"] = normalize_repair(result["repair"])
    if result["build_manifest"] is not None and (
        not isinstance(result["build_manifest"], str)
        or not result["build_manifest"].strip()
    ):
        raise ValueError("supervision.build_manifest must be a nonempty path")
    return result
