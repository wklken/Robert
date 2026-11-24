"""Strict native resume values; no scheduling, process or database authority."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from uuid import UUID


PROVIDERS = {"codex": "codex", "tcodex": "codex", "claude": "claude", "tclaude": "claude"}
NATIVE_HOME_VARIABLES = {"codex": "CODEX_HOME", "claude": "CLAUDE_CONFIG_DIR"}
RESUME_KEYS = frozenset(
    {
        "version",
        "agent",
        "provider",
        "session_id",
        "task_id",
        "run_id",
        "parent_run_id",
        "segment",
        "prompt_path",
        "prompt_sha256",
        "cwd",
        "add_dirs",
        "history_root",
        "history_path",
        "history_identity",
        "native_home",
        "native_home_identity",
    }
)


@contextmanager
def open_regular(path):
    """Open absolute paths without following symlinks in any component."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("resume path must be absolute")
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    descriptor = None
    try:
        for component in path.parts[1:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("resume path must be a regular file")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            yield stream
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


def directory_path(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("resume directory must be absolute")
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise ValueError("symlink resume directory")
    if not path.is_dir():
        raise ValueError("resume directory is missing")
    return str(path)


def directory_identity(value):
    """Pin the runtime directory without following symlink components."""
    path = Path(directory_path(value))
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        return {"dev": info.st_dev, "ino": info.st_ino}
    finally:
        os.close(descriptor)


def native_home_from_environment(agent, environment):
    """Select a home only from the registered trusted launch environment.

    Never read ambient environment here: a restarted controller may have a
    different configuration. Callers must freeze this value before launching.
    """
    if agent not in PROVIDERS or not isinstance(environment, dict):
        raise ValueError("unsupported native runtime environment")
    variable = NATIVE_HOME_VARIABLES[PROVIDERS[agent]]
    home = environment.get(variable, str(Path.home().absolute() / ("." + agent)))
    return directory_path(home)


def prompt_digest(path):
    with open_regular(path) as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return digest


def validate_resume(resume, *, agent, prompt_path, cwd, native_home=None):
    if not isinstance(resume, dict) or set(resume) != RESUME_KEYS:
        raise ValueError("invalid native resume descriptor fields")
    if resume["version"] != 1 or type(resume["version"]) is not int:
        raise ValueError("unsupported native resume descriptor version")
    if agent not in PROVIDERS or (resume["agent"], resume["provider"]) != (agent, PROVIDERS[agent]):
        raise ValueError("native resume provider changed")
    try:
        sid = str(UUID(resume["session_id"]))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("invalid native resume session ID") from exc
    if sid != resume["session_id"]:
        raise ValueError("native resume session ID must be canonical")
    if any(
        not isinstance(resume[key], str) or not resume[key].strip() for key in ("task_id", "run_id", "parent_run_id")
    ):
        raise ValueError("native resume run identity missing")
    if (
        resume["run_id"] == resume["parent_run_id"]
        or type(resume["segment"]) is not int
        or not 1 <= resume["segment"] <= 3
    ):
        raise ValueError("invalid native resume segment")
    if str(prompt_path) != resume["prompt_path"] or directory_path(cwd) != resume["cwd"]:
        raise ValueError("native resume prompt path or cwd changed")
    if not isinstance(resume["prompt_sha256"], str) or not re.fullmatch("[0-9a-f]{64}", resume["prompt_sha256"]):
        raise ValueError("invalid native resume prompt hash")
    if prompt_digest(prompt_path) != resume["prompt_sha256"]:
        raise ValueError("native resume prompt changed")
    dirs = [str(Path(prompt_path).parent)]
    if str(cwd) != dirs[0]:
        dirs.append(str(cwd))
    if resume["add_dirs"] != dirs:
        raise ValueError("native resume permission directories changed")
    home = directory_path(resume["native_home"])
    if native_home is not None and directory_path(native_home) != home:
        raise ValueError("native resume runtime home changed")
    if resume["native_home_identity"] != directory_identity(home):
        raise ValueError("native resume runtime home replaced")
    root = Path(directory_path(resume["history_root"]))
    expected = Path(home) / ("sessions" if PROVIDERS[agent] == "codex" else "projects")
    if root != expected or not Path(resume["history_path"]).is_relative_to(root):
        raise ValueError("native resume history root changed")
    path = Path(resume["history_path"])
    if PROVIDERS[agent] == "codex":
        if not path.name.startswith("rollout-") or not path.name.endswith(f"-{sid}.jsonl"):
            raise ValueError("native resume history session changed")
    elif path != root / re.sub(r"[^a-zA-Z0-9]", "-", str(cwd)) / f"{sid}.jsonl":
        raise ValueError("native resume history project changed")
    identity = resume["history_identity"]
    if (
        not isinstance(identity, dict)
        or set(identity) != {"dev", "ino"}
        or any(type(v) is not int for v in identity.values())
    ):
        raise ValueError("invalid native history identity")
    with open_regular(resume["history_path"]) as stream:
        info = os.fstat(stream.fileno())
        if identity != {"dev": info.st_dev, "ino": info.st_ino}:
            raise ValueError("native history file replaced")
        if not native_prompt_binding(stream, agent, sid, resume["prompt_sha256"], str(cwd)):
            raise ValueError("native session initial prompt binding changed")
    return {
        **resume,
        "add_dirs": list(resume["add_dirs"]),
        "history_identity": dict(identity),
        "native_home_identity": dict(resume["native_home_identity"]),
    }


def _json_object(line):
    try:
        value = json.loads(line)
        return value if isinstance(value, dict) else {}
    except (ValueError, UnicodeDecodeError, RecursionError):
        return {}


def _text_content(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        part if isinstance(part, str) else str(part.get("text", ""))
        for part in content
        if isinstance(part, (str, dict))
    )


def native_prompt_binding(fh, agent, session_id, prompt_sha256, cwd):
    """Resume binds the first user prompt, never a later matching message."""
    fh.seek(0)
    rows = [_json_object(line) for line in fh.read(524288).split(b"\n")[:-1]]
    if agent in {"codex", "tcodex"}:
        if not rows or rows[0].get("type") != "session_meta":
            return False
        meta = rows[0].get("payload")
        if not isinstance(meta, dict) or (meta.get("id"), meta.get("source"), meta.get("cwd")) != (
            session_id,
            "exec",
            cwd,
        ):
            return False
        for row in rows[1:]:
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            if payload.get("type") in {"function_call", "custom_tool_call"}:
                return False
            if (
                row.get("type") == "response_item"
                and payload.get("type") == "message"
                and payload.get("role") == "user"
            ):
                text = _text_content(payload.get("content"))
                if hashlib.sha256(text.encode()).hexdigest() == prompt_sha256:
                    return True
                # Native exec writes its AGENTS/environment bundle as a user
                # message before the stdin task. This exact envelope is runtime
                # context; the first actual task must still match the digest.
                if (
                    text.startswith("# AGENTS.md instructions for ")
                    and "<INSTRUCTIONS>" in text
                    and "</INSTRUCTIONS>" in text
                    and "<environment_context>" in text
                    and text.rstrip().endswith("</environment_context>")
                ):
                    continue
                return False
    elif agent in {"claude", "tclaude"}:
        for row in rows:
            if row.get("type") in {"assistant", "tool"}:
                return False
            if row.get("type") != "user":
                continue
            message = row.get("message")
            return (
                row.get("sessionId") == session_id
                and row.get("cwd") == cwd
                and not row.get("isSidechain")
                and isinstance(message, dict)
                and message.get("role") == "user"
                and hashlib.sha256(_text_content(message.get("content")).encode()).hexdigest() == prompt_sha256
            )
    return False
