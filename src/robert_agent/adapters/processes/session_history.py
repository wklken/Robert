"""Bounded, read-only access to provider histories and registered JSONL logs."""

from contextlib import contextmanager
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from uuid import UUID

from robert_agent.worker_adapters._session import native_prompt_binding


HEADER_BYTES = 524288
PAGE_SCAN_BYTES = 4194304
MAX_LINE_BYTES = 524288


@contextmanager
def open_history(root, path, *, trusted_root_alias=False):
    """Walk with directory descriptors so symlink replacement cannot redirect reads."""
    directory_fd = None
    file_fd = None
    try:
        root = Path(root).absolute()
        path = Path(path).absolute()
        if trusted_root_alias:
            resolved = root.resolve()
            if path.is_relative_to(root):
                path = resolved / path.relative_to(root)
            elif not path.is_relative_to(resolved):
                # Older rows may use a different alias of the same data root.
                # Match its ancestor before checking the remaining components.
                for ancestor in reversed(path.parents):
                    if ancestor.resolve() == resolved:
                        path = resolved / path.relative_to(ancestor)
                        break
            root = resolved
        relative = path.relative_to(root)
        if not relative.parts or ".." in relative.parts:
            raise ValueError("invalid history path")
        directory_fd = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY)
        for component in (*root.parts[1:], *relative.parts[:-1]):
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child
        file_fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise ValueError("history is not a regular file")
        fh = os.fdopen(file_fd, "rb")
        file_fd = None
        with fh:
            yield fh
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def json_object(line):
    try:
        result = json.loads(line)
        return result if isinstance(result, dict) else {}
    except (ValueError, UnicodeDecodeError, RecursionError):
        return {}


def text_content(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        part if isinstance(part, str) else str(part.get("text", ""))
        for part in content
        if isinstance(part, (str, dict))
    )


def valid_session_id(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        return None


def stdout_session_id(root, path, *, agent="tcodex", trusted_root_alias=True):
    if not path:
        return None
    try:
        with open_history(root, path, trusted_root_alias=trusted_root_alias) as fh:
            ids = set()
            for line in fh.read(HEADER_BYTES).split(b"\n")[:-1]:
                event = json_object(line)
                if agent in {"tcodex", "codex"} and event.get("type") == "thread.started":
                    sid = valid_session_id(event.get("thread_id"))
                elif agent in {"tclaude", "claude"} and (event.get("type"), event.get("subtype")) == ("system", "init"):
                    sid = valid_session_id(event.get("session_id"))
                else:
                    continue
                if not sid:
                    return None
                ids.add(sid)
            return next(iter(ids)) if len(ids) == 1 else None
    except (OSError, ValueError):
        pass
    return None


def session_binding(fh, task_id, attempt_id, session_id, *, prompt_sha256=None):
    fh.seek(0)
    header = fh.read(HEADER_BYTES)
    rows = [json_object(line) for line in header.split(b"\n")[:-1]]
    if not rows:
        return False, False
    meta = rows[0].get("payload", {})
    if not isinstance(meta, dict) or meta.get("id") != session_id or meta.get("source") != "exec":
        return False, False
    modern = any(
        row.get("type") == "event_msg" and row.get("payload", {}).get("type") == "item_completed"
        for row in rows
        if isinstance(row.get("payload"), dict)
    )
    for row in rows:
        payload = row.get("payload", {})
        if not isinstance(payload, dict):
            continue
        if row.get("type") == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
            text = text_content(payload.get("content"))
            if prompt_sha256:
                if hashlib.sha256(text.encode()).hexdigest() == prompt_sha256:
                    return True, modern
                continue
            # Both identities must belong to the same initial prompt, before tool output.
            bound = all(
                re.search(rf"(?m)^{key}: {re.escape(value)}\s*$", text)
                for key, value in (
                    ("task_id", task_id),
                    ("attempt_id", attempt_id),
                )
            )
            if bound:
                return True, modern
        if payload.get("type") in ("function_call", "custom_tool_call"):
            break
    return False, modern


def find_native_history(
    agent,
    started_at,
    task_id,
    attempt_id,
    session_id=None,
    *,
    prompt_sha256=None,
    cwd=None,
    strict_root=False,
    native_home=None,
):
    """Search only the launch's date directories; return no unbound history."""
    if agent in {"claude", "tclaude"}:
        return _find_claude_history(agent, task_id, attempt_id, session_id, prompt_sha256, cwd, native_home)
    if agent == "command" and not strict_root and native_home is None:
        # Legacy coordinator read models have no provider stored at attempt
        # level. This read-only lookup cannot authorize native resume.
        for provider in ("tcodex", "codex"):
            found = _find_native_history(provider, started_at, task_id, attempt_id, session_id, prompt_sha256, False)
            if found:
                return found
        return None
    if agent not in {"tcodex", "codex"} or started_at is None:
        return None
    return _find_native_history(
        agent, started_at, task_id, attempt_id, session_id, prompt_sha256, strict_root, native_home
    )


def _find_native_history(
    agent, started_at, task_id, attempt_id, session_id, prompt_sha256, strict_root, native_home=None
):
    if started_at is None:
        return None
    home = (
        Path(native_home)
        if native_home is not None
        else (Path.home().absolute() if strict_root else Path.home().resolve())
        / (".tcodex" if agent == "tcodex" else ".codex")
    )
    root = home / "sessions"
    for day_offset in (0, 1, -1):
        day = started_at + timedelta(days=day_offset)
        directory = root / day.strftime("%Y/%m/%d")
        candidates = []
        try:
            # Refuse symlink directories before discovery as well as when opening.
            if home.is_symlink():
                raise ValueError("symlink provider root")
            current = home
            for part in ("sessions", *day.strftime("%Y/%m/%d").split("/")):
                current = current / part
                if current.is_symlink():
                    raise ValueError("symlink history directory")
            with os.scandir(directory) as entries:
                for index, entry in enumerate(entries):
                    if index >= 4000:
                        break
                    if not entry.name.startswith("rollout-") or not entry.name.endswith(".jsonl"):
                        continue
                    sid = valid_session_id(entry.name[-42:-6])
                    if not sid or (session_id and sid != session_id) or not entry.is_file(follow_symlinks=False):
                        continue
                    candidates.append((entry.stat(follow_symlinks=False).st_mtime, entry.path, sid))
            for _, path, sid in sorted(candidates, reverse=True)[:256]:
                try:
                    with open_history(root, path) as fh:
                        bound, modern = session_binding(fh, task_id, attempt_id, sid, prompt_sha256=prompt_sha256)
                    if bound:
                        return {"root": root, "path": Path(path), "session_id": sid, "modern": modern}
                except (OSError, ValueError):
                    continue
        except (OSError, ValueError):
            continue
    return None


def _find_claude_history(agent, task_id, attempt_id, session_id, prompt_sha256, cwd, native_home=None):
    # Claude histories are project-scoped. An explicit ID, cwd and prompt are
    # required; never scan unrelated projects or another provider's storage.
    if not valid_session_id(session_id) or not cwd or not prompt_sha256:
        return None
    home = (
        Path(native_home)
        if native_home is not None
        else Path.home().absolute() / (".tclaude" if agent == "tclaude" else ".claude")
    )
    root = home / "projects"
    project = re.sub(r"[^a-zA-Z0-9]", "-", str(cwd))
    path = root / project / f"{session_id}.jsonl"
    try:
        with open_history(root, path) as fh:
            if native_prompt_binding(fh, agent, session_id, prompt_sha256, str(cwd)):
                return {"root": root, "path": path, "session_id": session_id, "modern": False}
    except (OSError, ValueError):
        pass
    return None


def reverse_lines(fh, end):
    """Yield complete lines and their offsets with bounded memory and I/O."""
    position = end
    buffer = b""
    scanned = 0
    skipping = False
    while position > 0 and scanned < PAGE_SCAN_BYTES:
        count = min(65536, position, PAGE_SCAN_BYTES - scanned)
        position -= count
        fh.seek(position)
        chunk = fh.read(count)
        scanned += count
        combined = chunk + buffer
        parts = combined.split(b"\n")
        buffer = parts[0]
        cursor = position + len(combined)
        for line in reversed(parts[1:]):
            cursor -= len(line)
            if skipping:
                yield cursor, None
                skipping = False
            elif len(line) > MAX_LINE_BYTES:
                yield cursor, None
            elif line:
                yield cursor, line
            cursor -= 1
        if len(buffer) > MAX_LINE_BYTES:
            buffer = b""
            skipping = True
    if position == 0:
        if skipping or len(buffer) > MAX_LINE_BYTES:
            yield 0, None
        elif buffer:
            yield 0, buffer
    else:
        yield position + len(buffer), None


def complete_end(fh, size):
    """Exclude a last JSON record still being appended by the worker."""
    fh.seek(max(0, size - MAX_LINE_BYTES))
    tail = fh.read(MAX_LINE_BYTES)
    cut = tail.rfind(b"\n")
    return max(0, size - len(tail) + cut + 1) if cut >= 0 else 0
