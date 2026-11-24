"""Read-only prerequisites for a new merge commit; callers own recovery and effects."""

import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import time

from robert_agent.foundation.commands import run_command


_MERGE_HOOKS = ("pre-merge-commit", "prepare-commit-msg", "commit-msg", "post-merge")
_SIMPLE_PREFIX = """if [ "$SKIP_SIMPLE_GIT_HOOKS" = "1" ]; then
    echo "[INFO] SKIP_SIMPLE_GIT_HOOKS is set to 1, skipping hook."
    exit 0
fi
if [ -f "$SIMPLE_GIT_HOOKS_RC" ]; then
    . "$SIMPLE_GIT_HOOKS_RC"
fi"""
_HUSKY_WRAPPER = '''#!/usr/bin/env sh
. "$(dirname "$0")/h"'''
_HUSKY_DRIVER = """#!/usr/bin/env sh
[ "$HUSKY" = "2" ] && set -x
n=$(basename "$0")
s=$(dirname "$(dirname "$0")")/$n

[ ! -f "$s" ] && exit 0

if [ -f "$HOME/.huskyrc" ]; then
\techo "husky - '~/.huskyrc' is DEPRECATED, please move your code to ~/.config/husky/init.sh"
fi
i="${XDG_CONFIG_HOME:-$HOME/.config}/husky/init.sh"
[ -f "$i" ] && . "$i"

[ "${HUSKY-}" = "0" ] && exit 0

export PATH="node_modules/.bin:$PATH"
sh -e "$s" "$@"
c=$?

[ $c != 0 ] && echo "husky - $n script failed (code $c)"
[ $c = 127 ] && echo "husky - command not found in PATH=$PATH"
exit $c"""


class MergePreflightError(ValueError):
    """A merge prerequisite needs operator attention before spending a refresh."""


def _read(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
                raise ValueError
            value = stream.read(65537)
            if len(value) > 65536:
                raise ValueError
            return value.decode("utf-8")
    except (OSError, ValueError, UnicodeError):
        raise MergePreflightError(
            "merge hook dependency file is missing, unreadable or too large; restore its installation"
        ) from None


def _manifest(path):
    try:
        result = json.loads(_read(path))
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, TypeError):
        raise MergePreflightError(
            "merge hook package manifest is unavailable or malformed; restore the local install"
        ) from None


def _executable(name, cwd, env, *, kind="runtime"):
    search = os.pathsep.join(
        str(cwd / p) if not Path(p).is_absolute() else p for p in env.get("PATH", os.defpath).split(os.pathsep)
    )
    found = shutil.which(name, path=search) if "/" not in name else str(cwd / name)
    if not found or not Path(found).is_file() or not os.access(found, os.X_OK):
        label = name if re.fullmatch(r"[A-Za-z0-9_.-]+", name) else "declared"
        raise MergePreflightError(f"merge hook {kind} {label} is unavailable; install it in the merge process PATH")
    return Path(found)


def _interpreter(text, cwd, env):
    if not text.startswith("#!"):
        raise MergePreflightError("merge hook interpreter is undeclared; provide a supported explicit shebang")
    try:
        parts = shlex.split(text.splitlines()[0][2:])
    except ValueError:
        parts = []
    if parts and parts[0] == "/usr/bin/env":
        parts = parts[1:]
        if parts[:1] == ["-S"]:
            parts = parts[1:]
    if len(parts) != 1 or parts[0].startswith("-") or "=" in parts[0]:
        raise MergePreflightError("merge hook interpreter arguments are unsupported; verify its environment explicitly")
    _executable(parts[0], cwd, env, kind="interpreter")
    return Path(parts[0]).name


def _normalized(text):
    return "\n".join(line.strip() for line in text.strip().splitlines() if line.strip())


def _version_satisfies(version, constraint):
    """Support declared numeric comparators; refuse unrecognized engine expressions."""
    for alternative in constraint.split("||"):
        terms = alternative.strip().split()
        if not terms:
            raise ValueError
        matches = []
        for term in terms:
            match = re.fullmatch(r"(>=|<=|>|<|=|\^|~)?v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", term)
            if not match:
                raise ValueError
            op, major, minor, patch = match.groups()
            required = (int(major), int(minor or 0), int(patch or 0))
            if op == "^":
                index = next((i for i, part in enumerate(required) if part), 2)
                upper = required[:index] + (required[index] + 1,) + (0,) * (2 - index)
                matches.append(required <= version < upper)
            elif op == "~":
                upper = (required[0], required[1] + 1, 0) if minor else (required[0] + 1, 0, 0)
                matches.append(required <= version < upper)
            elif op is None:
                width = 3 if patch else 2 if minor else 1
                matches.append(version[:width] == required[:width])
            else:
                matches.append(
                    {
                        "=": version == required,
                        ">=": version >= required,
                        "<=": version <= required,
                        ">": version > required,
                        "<": version < required,
                    }[op]
                )
        if all(matches):
            return True
    return False


def _node_packages(package_path, *, boundary, seen=None, deadline):
    """Follow installed runtime dependencies without imports or package-manager calls."""
    if time.monotonic() >= deadline:
        raise MergePreflightError("merge environment preflight timed out; restore prerequisites and retry")
    seen = set() if seen is None else seen
    actual = package_path.resolve()
    if actual in seen:
        return []
    if len(seen) >= 256:
        raise MergePreflightError("merge hook package dependency graph is too large; verify the install explicitly")
    seen.add(actual)
    manifest = _manifest(package_path / "package.json")
    dependencies = manifest.get("dependencies", {})
    if not isinstance(dependencies, dict):
        raise MergePreflightError("merge hook package dependencies are malformed; restore the local install")
    constraints = [manifest.get("engines", {}).get("node")]
    for name in dependencies:
        if not re.fullmatch(r"(?:@[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+", name) or name in (".", ".."):
            raise MergePreflightError("merge hook package dependency is unsupported; restore the local install")
        child = None
        for parent in (actual, *actual.parents):
            if parent.name != "node_modules" and (parent / "node_modules" / name / "package.json").is_file():
                child = parent / "node_modules" / name
                break
            if parent == boundary:
                break
        if child is None:
            raise MergePreflightError(
                "merge hook commitlint runtime dependency is missing; install declared local dependencies"
            )
        constraints.extend(_node_packages(child, boundary=boundary, seen=seen, deadline=deadline))
    return constraints


def _commitlint(cwd, env, command, *, deadline):
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = []
    if len(parts) != 4 or parts[:3] != ["npx", "commitlint", "--edit"] or parts[3] not in ("$1", "${1}"):
        raise MergePreflightError(
            "merge commit-msg hook dependencies are unknown; verify or simplify its commands explicitly"
        )
    if env.get("NODE_OPTIONS"):
        raise MergePreflightError(
            "merge hook NODE_OPTIONS changes runtime initialization; verify or remove it in the merge environment"
        )
    if env.get("NODE_PATH"):
        raise MergePreflightError(
            "merge hook NODE_PATH changes package resolution; verify the local dependency environment"
        )
    node = _executable("node", cwd, env)
    npx = _executable("npx", cwd, env)
    if _interpreter(_read(npx), cwd, env) != "node":
        raise MergePreflightError("merge hook npx interpreter is unsupported; restore the local npm installation")
    manifest = _manifest(cwd / "package.json")
    dependencies = {**manifest.get("dependencies", {}), **manifest.get("devDependencies", {})}
    if "@commitlint/cli" not in dependencies:
        raise MergePreflightError("merge hook commitlint is undeclared; declare and install its local package")
    local = cwd / "node_modules/.bin/commitlint"
    if not local.is_file() or not os.access(local, os.X_OK):
        raise MergePreflightError(
            "merge hook commitlint is missing; install declared local dependencies before retrying (no npx download)"
        )
    cli = _manifest(cwd / "node_modules/@commitlint/cli/package.json")
    bins = cli.get("bin", {})
    entry = bins if isinstance(bins, str) else bins.get("commitlint") if isinstance(bins, dict) else None
    if not entry or local.resolve() != (cwd / "node_modules/@commitlint/cli" / entry).resolve():
        raise MergePreflightError(
            "merge hook commitlint executable differs from its local package; restore the install"
        )
    if _interpreter(_read(local), cwd, env) != "node":
        raise MergePreflightError("merge hook commitlint interpreter is unsupported; restore the local Node entrypoint")
    constraints = [manifest.get("engines", {}).get("node")]
    constraints.extend(
        _node_packages(cwd / "node_modules/@commitlint/cli", boundary=Path(cwd.anchor), deadline=deadline)
    )
    configs = [
        cwd / name
        for name in (
            ".commitlintrc.cjs",
            ".commitlintrc.js",
            "commitlint.config.cjs",
            "commitlint.config.js",
            ".commitlintrc.json",
            ".commitlintrc",
        )
        if (cwd / name).is_file()
    ]
    if len(configs) != 1:
        raise MergePreflightError(
            "merge hook commitlint configuration is unknown; verify a single supported local config"
        )
    config = _read(configs[0]).strip()
    if config.startswith("module.exports"):
        # The target repo uses a literal config plus this pure ignore predicate.
        config = re.sub(r"^module\.exports\s*=\s*", "", config).rstrip(";").strip()
        config = re.sub(r"ignores\s*:\s*\[commit\s*=>\s*commit\.includes\('init'\)\]\s*,", "", config)
        config = re.sub(r"'([^'\\]*)'", lambda match: json.dumps(match[1]), config)
        config = re.sub(r"([{,]\s*)([A-Za-z][A-Za-z0-9_-]*)(\s*:)", r'\1"\2"\3', config)
        config = re.sub(r",\s*([}\]])", r"\1", config)
    try:
        value = json.loads(config)
        if not isinstance(value, dict) or set(value) - {"extends", "rules", "defaultIgnores"}:
            raise ValueError
        extends = value["extends"]
        if extends != ["@commitlint/config-conventional"]:
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise MergePreflightError(
            "merge hook commitlint configuration dependencies are dynamic or unknown; verify a literal local config"
        ) from None
    for package in extends:
        if package not in dependencies:
            raise MergePreflightError(
                "merge hook commitlint preset is undeclared; declare and install its local package"
            )
        constraints.extend(_node_packages(cwd / "node_modules" / package, boundary=Path(cwd.anchor), deadline=deadline))
    return node, constraints


def check_merge_environment(worktree_path, *, env=None, timeout_seconds=10):
    """Check only a *new* merge, before checkpoint/budget writes. Skip exact recovery.

    The supplied environment replaces os.environ, matching subprocess semantics.
    Only Git observations and the declared Node runtime's --version are executed.
    Shell hooks, npx, package installation and JavaScript configs are never run.
    Unknown scripts require explicit operator verification, without hook bypass.
    """
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise MergePreflightError("merge preflight requires a finite positive timeout")
    deadline = time.monotonic() + min(timeout_seconds, 10)
    environment = dict(os.environ if env is None else env)
    try:
        cwd = Path(worktree_path).resolve()
    except (OSError, RuntimeError):
        raise MergePreflightError("merge worktree path is unavailable; restore the registered checkout") from None

    def observe(args, *, at=cwd):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MergePreflightError("merge environment preflight timed out; restore prerequisites and retry")
        try:
            return run_command(
                args, cwd=at, env=environment, text=True, capture_output=True, check=True, timeout=remaining
            ).stdout.strip()
        except subprocess.TimeoutExpired:
            raise MergePreflightError(
                "merge environment preflight timed out; restore prerequisites and retry"
            ) from None
        except (subprocess.CalledProcessError, OSError):
            raise MergePreflightError(
                "merge environment observation failed; verify Git/runtime configuration locally"
            ) from None

    git = _executable("git", cwd, environment)
    for role in ("AUTHOR", "COMMITTER"):
        try:
            identity = observe([str(git), "var", f"GIT_{role}_IDENT"])
            if not re.fullmatch(r"[^<>\n]+ <[^<>\n]+> \d+ [+-]\d{4}", identity):
                raise MergePreflightError("invalid identity")
        except MergePreflightError as exc:
            if "timed out" in str(exc):
                raise
            raise MergePreflightError(
                f"merge Git {role.lower()} identity is unavailable; configure user.name/user.email "
                "in the target repository or supply valid GIT identity variables"
            ) from None
    hooks = Path(observe([str(git), "rev-parse", "--path-format=absolute", "--git-path", "hooks"]))
    for name in _MERGE_HOOKS:
        if time.monotonic() >= deadline:
            raise MergePreflightError("merge environment preflight timed out; restore prerequisites and retry")
        hook = hooks / name
        if not hook.exists() or not os.access(hook, os.X_OK):
            continue
        text = _read(hook)
        interpreter = _interpreter(text, cwd, environment)
        if any(environment.get(variable) for variable in ("BASH_ENV", "ENV")):
            raise MergePreflightError(
                "merge hook shell initialization is dynamic; verify its runtime environment explicitly"
            )
        hook_env = dict(environment)
        if _normalized(text) == _normalized(_HUSKY_WRAPPER):
            if _normalized(_read(hooks / "h")) != _normalized(_HUSKY_DRIVER):
                raise MergePreflightError(
                    f"merge {name} hook dependencies use an unknown Husky driver; verify the installation"
                )
            if environment.get("HUSKY") == "0":
                raise MergePreflightError("merge hook HUSKY disables hooks; restore the required hook environment")
            home = Path(environment.get("HOME", "~"))
            config_home = Path(environment.get("XDG_CONFIG_HOME") or home / ".config")
            if (config_home / "husky/init.sh").exists():
                raise MergePreflightError(
                    "merge hook Husky initialization is dynamic; verify its runtime environment explicitly"
                )
            for utility in ("basename", "dirname", "sh"):
                _executable(utility, cwd, environment)
            hook = hooks.parent / name
            if not hook.exists():
                continue
            text = _read(hook)
            interpreter = "sh"
            hook_env["PATH"] = "node_modules/.bin" + os.pathsep + environment.get("PATH", os.defpath)
        body = "\n".join(text.splitlines()[1:]) if text.startswith("#!") else text
        if body.strip().startswith(_SIMPLE_PREFIX):
            if environment.get("SKIP_SIMPLE_GIT_HOOKS") == "1":
                raise MergePreflightError(
                    "merge hook SKIP_SIMPLE_GIT_HOOKS disables hooks; restore the required hook environment"
                )
            startup = environment.get("SIMPLE_GIT_HOOKS_RC")
            if startup and Path(startup).exists():
                raise MergePreflightError(
                    "merge hook initialization is dynamic; verify its runtime environment explicitly"
                )
            body = body.strip()[len(_SIMPLE_PREFIX) :].strip()
        body = body.strip()
        if interpreter in ("sh", "bash", "dash") and body in ("exit 0", ":"):
            continue
        at = cwd
        if body.startswith("cd "):
            split = body.split(" && ", 1)
            if len(split) != 2:
                raise MergePreflightError(f"merge {name} hook dependencies are unknown; verify its commands explicitly")
            try:
                directory = shlex.split(split[0])
            except ValueError:
                directory = []
            if len(directory) != 2 or re.search(r"[$`\n]", directory[1]):
                raise MergePreflightError(
                    f"merge {name} hook dependencies use a dynamic directory; verify them explicitly"
                )
            at = (cwd / directory[1]).resolve()
            if not at.is_relative_to(cwd) or not at.is_dir():
                raise MergePreflightError(
                    f"merge {name} hook dependencies directory is unavailable; restore its checkout"
                )
            body = split[1]
        if name != "commit-msg" or interpreter not in ("sh", "bash", "dash"):
            raise MergePreflightError(
                f"merge {name} hook dependencies are unknown; verify or simplify the hook explicitly"
            )
        try:
            node, constraints = _commitlint(at, hook_env, body, deadline=deadline)
            output = observe([str(node), "--version"], at=at)
            match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", output)
            if not match:
                raise ValueError
            version = tuple(map(int, match.groups()))
            for constraint in constraints:
                if constraint and (not isinstance(constraint, str) or not _version_satisfies(version, constraint)):
                    raise ValueError
        except (ValueError, TypeError, AttributeError, OSError) as exc:
            if isinstance(exc, MergePreflightError):
                raise
            raise MergePreflightError(
                "merge hook node version or declared engines are unsupported; install a compatible Node runtime"
            ) from None
