"""Inventory preinstalled tools for changed components without executing project code."""

from configparser import ConfigParser, Error as ConfigError
from functools import cache
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib

from robert_agent.foundation.commands import ControlDeadlineExceeded, run_command
from robert_agent.workflows.review.files import safe_path


_VERSION = r"[0-9]{1,6}(?:\.[0-9]{1,6}){0,2}"
_NUMERIC_RANGE = re.compile(rf"(~=|==|>=|<=|!=|>|<|=)?\s*({_VERSION})")
_PYTHON_FILES = (".python-version", "pyproject.toml", "setup.cfg", "Pipfile")
_PNPM_ENV = {
    "COREPACK_ENABLE_NETWORK": "0",
    "COREPACK_ENABLE_PROJECT_SPEC": "0",
    "npm_config_manage_package_manager_versions": "false",
}


def _project_path(tree, path):
    try:
        return safe_path(tree / path, tree)
    except (OSError, RuntimeError, ValueError):
        return None


def _read(tree, path):
    candidate = _project_path(tree, path)
    try:
        if candidate and candidate.is_file() and candidate.stat().st_size <= 256 * 1024:
            return candidate.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        pass
    return ""


def _package(tree, directory):
    try:
        data = json.loads(_read(tree, directory / "package.json"))
    except (ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def _version_tuple(version):
    parts = tuple(map(int, version.split(".")))
    return parts + (0,) * (3 - len(parts))


def _compatible(version, requirement):
    """Match numeric pins and simple numeric AND ranges; fail closed on other syntax."""
    if not requirement:
        return False
    selected = _version_tuple(version)
    parts = [part for part in re.split(r"[,\s]+", re.sub(r"([<>=!~])\s+", r"\1", requirement.strip())) if part]
    for part in parts:
        match = _NUMERIC_RANGE.fullmatch(part)
        if not match:
            return False
        op, target = match.groups()
        expected = _version_tuple(target)
        if op is None:
            if version.split(".")[: len(target.split("."))] != target.split("."):
                return False
        elif not {
            "==": selected == expected,
            "=": selected == expected,
            "!=": selected != expected,
            ">": selected > expected,
            "<": selected < expected,
            ">=": selected >= expected,
            "<=": selected <= expected,
            "~=": selected >= expected and version.split(".")[: len(target.split(".")) - 1] == target.split(".")[:-1],
        }[op]:
            return False
    return bool(parts)


def _resolver(tree):
    # PATH belongs to the service operator. Ignore relative entries and executables
    # under the source being reviewed, including symlink destinations.
    def trusted(path):
        try:
            return path.is_absolute() and not path.resolve().is_relative_to(tree)
        except (OSError, RuntimeError):
            return False

    path = list(dict.fromkeys(entry for entry in os.get_exec_path() if trusted(Path(entry))))
    env = {**os.environ, **_PNPM_ENV, "GOTOOLCHAIN": "local", "GOENV": "off", "PATH": os.pathsep.join(path)}

    @cache
    def probe(executable, arg, pattern):
        try:
            output = run_command(
                [executable, arg],
                cwd=Path(sys.executable).resolve().parent,
                env=env,
                timeout=2,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        except ControlDeadlineExceeded:
            raise
        except (OSError, UnicodeError, subprocess.SubprocessError):
            return None
        match = re.fullmatch(pattern, output)
        return match[1] if match else None

    def resolve(names, arg, pattern, requirements):
        result = {"status": "UNRESOLVED", "executable": None, "requirements": requirements}
        if not any(requirements):
            return {**result, "reason": "No supported project version declaration."}
        observed = set()
        for directory in path:
            for name in names:
                executable = shutil.which(name, path=directory)
                if not executable or not trusted(Path(executable)):
                    continue
                version = probe(executable, arg, pattern)
                if not version:
                    continue
                if all(_compatible(version, requirement) for requirement in requirements):
                    return {**result, "status": "RESOLVED", "executable": executable, "version": version}
                observed.add(version)
        if observed:
            return {
                **result,
                "reason": "Installed version mismatch or unsupported constraint.",
                "observed": sorted(observed),
            }
        return {**result, "reason": "No matching executable could be verified on Robert's PATH."}

    return resolve


def _python_config_declarations(name, text):
    """Read runtime metadata without treating tool targets as project constraints."""
    try:
        if name == "setup.cfg":
            config = ConfigParser(interpolation=None, inline_comment_prefixes=("#", ";"), default_section="")
            config.read_string(text)
            section = dict(config["options"]) if config.has_section("options") else {}
            keys = ("python_requires",)
        else:
            config = tomllib.loads(text)
            section = config.get("project" if name == "pyproject.toml" else "requires", {})
            keys = ("requires-python",) if name == "pyproject.toml" else ("python_version", "python_full_version")
        if not isinstance(section, dict):
            return ["<invalid Python runtime declaration>"]
        values = [section[key] for key in keys if key in section]
        if any(not isinstance(value, str) for value in values):
            return ["<invalid Python runtime declaration>"]
        return [value.strip() for value in values]
    except (ConfigError, tomllib.TOMLDecodeError, RecursionError):
        return ["<invalid Python runtime declaration>"]


def _python_declarations(tree, directory):
    declarations = {}
    for name in _PYTHON_FILES:
        path = directory / name
        text = _read(tree, path)
        if name == ".python-version":
            if text.strip():
                declarations[path.as_posix()] = text.strip()
        elif text and (values := _python_config_declarations(name, text)):
            declarations[path.as_posix()] = values
    return declarations


def _python_runtime(resolve, declarations, directory):
    pin = declarations.get((directory / ".python-version").as_posix())
    requirements = [pin] if pin else []
    names = ["python3", "python"]
    if pin and re.fullmatch(r"3\.\d+(?:\.\d+)?", pin):
        names.insert(0, "python" + ".".join(pin.split(".")[:2]))
    for name, values in declarations.items():
        if name.endswith(("pyproject.toml", "setup.cfg", "Pipfile")):
            requirements.extend(values)
    result = resolve(tuple(names), "--version", rf"Python ({_VERSION})", requirements)
    return {**result, "declarations": declarations, "dependencies": "UNVERIFIED"}


def _node_runtime(tree, directory, packages, resolve):
    ancestors = (directory, *directory.parents)
    pin = next((text.strip() for parent in ancestors if (text := _read(tree, parent / ".nvmrc"))), "")
    engine = packages[directory].get("engines")
    engine = engine.get("node") if isinstance(engine, dict) else None
    requirements = [pin.removeprefix("v")] if pin else []
    if isinstance(engine, str) and engine.strip():
        requirements.append(engine.strip())
    node = resolve(("node",), "--version", rf"v({_VERSION})", requirements)
    tools = {"node": node}
    manager = next(
        (value for parent in ancestors if isinstance(value := packages[parent].get("packageManager"), str)), ""
    )
    if manager.startswith("pnpm@"):
        pnpm = resolve(("pnpm",), "--version", rf"({_VERSION})", ["=" + manager[5:].split("+", 1)[0]])
        tools["pnpm"] = {**pnpm, "environment": _PNPM_ENV}
    lockfile = next(
        (path for parent in ancestors if (path := _project_path(tree, parent / "pnpm-lock.yaml")) and path.is_file()),
        None,
    )
    modules = _project_path(tree, directory / "node_modules")
    tools["node_dependencies"] = {
        "status": "PRESENT_UNVERIFIED" if modules and modules.is_dir() else "MISSING",
        "location": (directory / "node_modules").as_posix(),
        "lockfile": lockfile.relative_to(tree).as_posix() if lockfile else None,
    }
    return tools


def _go_runtime(tree, directory, module, resolve):
    workspace = next(
        (parent / "go.work" for parent in (directory, *directory.parents) if _read(tree, parent / "go.work")),
        None,
    )
    work = _read(tree, workspace) if workspace else ""
    minimums = [
        ">=" + match for match in re.findall(rf"(?m)^go[ \t]+({_VERSION})[ \t]*(?://[^\n]*)?$", module + "\n" + work)
    ]
    hints = re.findall(rf"(?m)^toolchain[ \t]+go({_VERSION})[ \t]*(?://[^\n]*)?$", work or module)
    result = resolve(("go",), "version", rf"go version go({_VERSION}) \S+", minimums)
    return {
        **result,
        "toolchain_hint": hints[0] if hints else None,
        "environment": {
            "GOTOOLCHAIN": "local",
            "GOENV": "off",
            "GOWORK": str(tree / workspace) if workspace else "off",
        },
        "dependencies": "UNVERIFIED",
    }


def runtime_info(tree, changed_paths=()):
    tree = Path(tree).resolve()
    directories = sorted({Path("."), *(parent for name in changed_paths for parent in Path(name).parents)})
    resolve = _resolver(tree)
    packages = {directory: _package(tree, directory) for directory in directories}
    components = {}
    declarations = {}
    for directory in directories:
        tools = {}
        python = _python_declarations(tree, directory)
        if python:
            declarations.update(python)
            tools["python"] = _python_runtime(resolve, python, directory)
        if packages[directory] or _read(tree, directory / ".nvmrc"):
            tools.update(_node_runtime(tree, directory, packages, resolve))
        if module := _read(tree, directory / "go.mod"):
            tools["go"] = _go_runtime(tree, directory, module, resolve)
        if tools:
            components[directory.as_posix()] = tools

    # Preserve the top-level Python shape for consumers of the original inventory.
    root_python = components.get(".", {}).get("python", {})
    return {
        "components": components,
        "python": {
            "status": root_python.get("status", "UNRESOLVED"),
            "interpreter": root_python.get("executable"),
            "declarations": declarations,
            "reason": root_python.get("reason", "See the component inventory for project-specific interpreters."),
            **({"version": "Python " + root_python["version"]} if "version" in root_python else {}),
        },
    }
