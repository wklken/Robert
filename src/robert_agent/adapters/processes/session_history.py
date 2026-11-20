"""Bounded, read-only access to local Codex histories and registered JSONL logs."""

from contextlib import contextmanager
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from uuid import UUID


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


def stdout_session_id(root, path):
    if not path:
        return None
    try:
        with open_history(root, path, trusted_root_alias=True) as fh:
            for line in fh.read(65536).splitlines():
                event = json_object(line)
                if event.get("type") == "thread.started":
                    return valid_session_id(event.get("thread_id"))
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


def find_native_history(agent, started_at, task_id, attempt_id, session_id=None, *, prompt_sha256=None):
    """Search only the launch's date directories; return no unbound history."""
    if started_at is None:
        return None
    agents = [agent] if agent in {"tcodex", "codex"} else ["tcodex", "codex"]
    for candidate_agent in agents:
        found = _find_native_history(candidate_agent, started_at, task_id, attempt_id, session_id, prompt_sha256)
        if found:
            return found
    return None


def _find_native_history(agent, started_at, task_id, attempt_id, session_id, prompt_sha256):
    home = Path.home().resolve() / (".tcodex" if agent == "tcodex" else ".codex")
    root = home / "sessions"
    for day_offset in (0, 1, -1):
        day = started_at + timedelta(days=day_offset)
        directory = root / day.strftime("%Y/%m/%d")
        candidates = []
        try:
            # Refuse symlink directories before discovery as well as when opening.
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
