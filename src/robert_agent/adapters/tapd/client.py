"""TAPD CLI v0.7.0 protocol with explicit workspace and bounded pagination."""

from collections.abc import Mapping
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import re
import subprocess
import time
from typing import Generic, TypeVar

from robert_agent.adapters.tapd.process import TapdError, run_bounded
from robert_agent.foundation.commands import run_command

_ENTRY_TYPES = {"story": ("stories",), "bug": ("bug", "bug_remark")}
_ENVIRONMENT_KEYS = frozenset(
    {"HOME", "TAPD_ACCESS_TOKEN", "TAPD_API_USER", "TAPD_API_PASSWORD", "TAPD_API_BASE_URL", "TAPD_BASE_URL"}
)
_ID = re.compile(r"[0-9]{1,64}")
_Row = TypeVar("_Row")


@dataclass(frozen=True)
class ReadBudget:
    """A shared deadline for a bounded discovery or full-ticket read."""

    deadline: float

    def remaining(self):
        duration = self.deadline - time.monotonic()
        if duration <= 0:
            raise TapdError("timeout")
        return duration


@dataclass(frozen=True)
class TapdItem:
    workspace_id: str
    kind: str
    external_id: str
    title: str | None = field(repr=False)
    status: str | None
    owner: str | None = field(repr=False)
    label: str | None = field(repr=False)
    description: str | None = field(repr=False)
    workitem_type_id: str | None
    raw: dict = field(repr=False)


@dataclass(frozen=True)
class TapdComment:
    workspace_id: str
    kind: str
    external_id: str
    entry_type: str
    entry_id: str
    description: str = field(repr=False)
    display_author: str | None = field(repr=False)
    created: str
    modified: str | None
    reply_id: str | None
    root_id: str | None
    raw: dict = field(repr=False)


@dataclass(frozen=True)
class CompleteScan(Generic[_Row]):
    """All requested streams reached a validated empty page within budget."""

    items: tuple[_Row, ...]
    complete = True


def _id(value, *, code="invalid_input", effect_unknown=False):
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise TapdError(code, effect_unknown=effect_unknown)
    return value


def _entry_types(kind):
    if not isinstance(kind, str) or kind not in _ENTRY_TYPES:
        raise TapdError("invalid_input")
    return _ENTRY_TYPES[kind]


def _text(row, field, *, required=False):
    value = row.get(field)
    if (value is not None and not isinstance(value, str)) or (required and not value):
        raise TapdError("invalid_response")
    return value


def _item(raw, workspace_id, kind, *, expected_id=None):
    workspace = _text(raw, "workspace_id", required=expected_id is not None)
    external_id = _id(raw.get("id"), code="invalid_response")
    if (workspace is not None and workspace != workspace_id) or (expected_id and external_id != expected_id):
        raise TapdError("invalid_response")
    return TapdItem(
        workspace_id=workspace_id,
        kind=kind,
        external_id=external_id,
        title=_text(raw, "name" if kind == "story" else "title"),
        status=_text(raw, "status", required=True),
        owner=_text(raw, "owner" if kind == "story" else "current_owner"),
        label=_text(raw, "label"),
        description=_text(raw, "description"),
        workitem_type_id=_text(raw, "workitem_type_id"),
        raw=raw,
    )


def _comment(raw, workspace_id, entry_type, *, entry_id=None, author=None):
    expected_entry = _id(raw.get("entry_id"), code="invalid_response")
    display_author = _text(raw, "author")
    if (
        raw.get("workspace_id") != workspace_id
        or raw.get("entry_type") != entry_type
        or (entry_id is not None and expected_entry != entry_id)
        or (author is not None and display_author != author)
    ):
        raise TapdError("invalid_response")
    return TapdComment(
        workspace_id=workspace_id,
        kind="story" if entry_type == "stories" else "bug",
        external_id=_id(raw.get("id"), code="invalid_response"),
        entry_type=entry_type,
        entry_id=expected_entry,
        description=_text(raw, "description") or "",
        display_author=display_author,
        created=_text(raw, "created", required=True),
        modified=_text(raw, "modified"),
        reply_id=_text(raw, "reply_id"),
        root_id=_text(raw, "root_id"),
        raw=raw,
    )


def _unique_object(pairs):
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError("duplicate JSON key")
    return obj


def _positive_seconds(seconds):
    return type(seconds) in (int, float) and math.isfinite(seconds) and seconds > 0


class TapdClient:
    """Run the configured executable in an adapter-owned, stable directory."""

    def __init__(
        self,
        *,
        executable,
        cwd,
        environment: Mapping[str, str],
        timeout=15.0,
        max_output_bytes=4 * 1024 * 1024,
        runner=None,
    ):
        executable, cwd = Path(executable), Path(cwd)
        if (
            not executable.is_absolute()
            or not cwd.is_absolute()
            or not cwd.is_dir()
            or not _positive_seconds(timeout)
            or type(max_output_bytes) is not int
            or max_output_bytes <= 0
        ):
            raise TapdError("invalid_configuration")
        self._executable = str(executable)
        self._cwd = cwd.resolve()
        self._environment = {key: value for key, value in environment.items() if key in _ENVIRONMENT_KEYS}
        if any(not isinstance(v, str) or "\0" in v for v in self._environment.values()):
            raise TapdError("invalid_configuration")
        self._timeout = timeout
        self._max_output_bytes = max_output_bytes
        self._runner = runner or run_bounded

    def _request(self, workspace_id, args, *, stop=None, timeout=None, payload=None):
        _id(workspace_id)
        if stop is not None and stop.is_set():
            raise TapdError("cancelled")
        command = [self._executable, "--workspace-id", workspace_id, "--json", *args]
        write = payload is not None
        try:
            result = run_command(
                command,
                runner=self._runner,
                cwd=self._cwd,
                env=dict(self._environment),
                input=payload,
                stop=stop,
                timeout=self._timeout if timeout is None else min(self._timeout, timeout),
                max_output_bytes=self._max_output_bytes,
            )
            if stop is not None and stop.is_set():
                raise TapdError("cancelled")
            if result.returncode:
                raise TapdError("command_failed")
            stdout = result.stdout
            stderr = result.stderr or b""
            if not isinstance(stdout, bytes) or not isinstance(stderr, bytes):
                raise TapdError("invalid_response")
            if len(stdout) + len(stderr) > self._max_output_bytes:
                raise TapdError("output_limit")
            data = json.loads(stdout.decode("utf-8"), object_pairs_hook=_unique_object)
            if not isinstance(data, dict):
                raise TapdError("invalid_response")
            return data, len(stdout)
        except TapdError as exc:
            raise TapdError(exc.code, effect_unknown=write) from None
        except subprocess.TimeoutExpired:
            raise TapdError("timeout", effect_unknown=write) from None
        except (OSError, subprocess.SubprocessError):
            raise TapdError("command_failed", effect_unknown=write) from None
        except (ValueError, RecursionError):
            raise TapdError("invalid_response", effect_unknown=write) from None

    def _scan(self, workspace_id, streams, *, page_size, max_pages, scan_timeout, max_scan_bytes, stop):
        if (
            type(page_size) is not int
            or not 1 <= page_size <= 200
            or type(max_pages) is not int
            or max_pages < 1
            or not _positive_seconds(scan_timeout)
            or type(max_scan_bytes) is not int
            or max_scan_bytes < 1
        ):
            raise TapdError("invalid_input")
        deadline = time.monotonic() + scan_timeout
        observed = {}
        for args, parse_row in streams:
            for page in range(1, max_pages + 1):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TapdError("timeout")
                page_data, size = self._request(
                    workspace_id,
                    [*args, "--page", str(page), "--limit", str(page_size)],
                    timeout=remaining,
                    stop=stop,
                )
                max_scan_bytes -= size
                if max_scan_bytes < 0:
                    raise TapdError("scan_limit")
                rows = page_data.get("items")
                if (
                    type(page_data.get("page")) is not int
                    or page_data["page"] != page
                    or type(page_data.get("limit")) is not int
                    or page_data["limit"] != page_size
                    or not isinstance(rows, list)
                    or len(rows) > page_size
                ):
                    raise TapdError("invalid_response")
                # CLI count is advisory, even on a zero-row page.
                if not rows:
                    break
                for raw in rows:
                    if not isinstance(raw, dict):
                        raise TapdError("invalid_response")
                    row = parse_row(raw)
                    identity = (row.entry_type, row.external_id) if isinstance(row, TapdComment) else row.external_id
                    if identity in observed:
                        raise TapdError("duplicate_record")
                    observed[identity] = row
            else:
                raise TapdError("scan_limit")
        if stop is not None and stop.is_set():
            raise TapdError("cancelled")
        if time.monotonic() >= deadline:
            raise TapdError("timeout")
        return CompleteScan(tuple(observed.values()))

    def list_items(
        self,
        workspace_id,
        kind,
        *,
        owner=None,
        label=None,
        page_size=100,
        max_pages=100,
        scan_timeout=120.0,
        max_scan_bytes=16 * 1024 * 1024,
        stop=None,
    ):
        _entry_types(kind)
        args = [kind, "list", "--order=id asc"]
        for flag, value in (("owner", owner), ("label", label)):
            if value is not None:
                if not isinstance(value, str) or not value.strip() or "\0" in value:
                    raise TapdError("invalid_input")
                args.append(f"--{flag}={value}")
        return self._scan(
            workspace_id,
            [(args, lambda raw: _item(raw, workspace_id, kind))],
            page_size=page_size,
            max_pages=max_pages,
            scan_timeout=scan_timeout,
            max_scan_bytes=max_scan_bytes,
            stop=stop,
        )

    def get_item(self, workspace_id, kind, external_id, *, stop=None, timeout=None):
        _entry_types(kind)
        data, _size = self._request(
            workspace_id, [kind, "show", _id(external_id), "--no-comments"], stop=stop, timeout=timeout
        )
        return _item(data, workspace_id, kind, expected_id=external_id)

    def _comments(
        self,
        workspace_id,
        types,
        *,
        entry_id=None,
        author=None,
        page_size=100,
        max_pages=100,
        scan_timeout=120.0,
        max_scan_bytes=16 * 1024 * 1024,
        stop=None,
    ):
        args = ["comment", "list", "--order=id asc"]
        if entry_id is not None:
            args.append(f"--entry-id={_id(entry_id)}")
        if author is not None:
            args.append(f"--author={author}")
        streams = [
            (
                [*args, f"--entry-type={entry_type}"],
                lambda raw, entry_type=entry_type: _comment(
                    raw, workspace_id, entry_type, entry_id=entry_id, author=author
                ),
            )
            for entry_type in types
        ]
        return self._scan(
            workspace_id,
            streams,
            page_size=page_size,
            max_pages=max_pages,
            scan_timeout=scan_timeout,
            max_scan_bytes=max_scan_bytes,
            stop=stop,
        )

    def list_comments(self, workspace_id, kind, external_id, **limits):
        return self._comments(workspace_id, _entry_types(kind), entry_id=_id(external_id), **limits)

    def discover_comments(self, workspace_id, author, **limits):
        """Find display-author matches as observations for a separate identity check."""
        if not isinstance(author, str) or not author.strip() or "\0" in author:
            raise TapdError("invalid_input")
        return self._comments(workspace_id, ("stories", "bug", "bug_remark"), author=author, **limits)

    def terminal_states(self, workspace_id, kind, *, workitem_type_id=None, stop=None, timeout=None):
        _entry_types(kind)
        args = ["workflow", "last-steps", f"--system={kind}"]
        if workitem_type_id is not None:
            args.append(f"--workitem-type-id={_id(workitem_type_id)}")
        elif kind == "story":
            raise TapdError("invalid_input")
        data, _size = self._request(workspace_id, args, stop=stop, timeout=timeout)
        if not data or any(not key or not isinstance(value, str) or not value for key, value in data.items()):
            raise TapdError("invalid_response")
        return data

    def add_comment(self, workspace_id, kind, external_id, body, *, stop=None):
        """Return the CLI-acknowledged ID. Verification requires source readback."""
        entry_type = _entry_types(kind)[0]
        if not isinstance(body, str) or not body.strip() or "\0" in body:
            raise TapdError("invalid_input")
        try:
            payload = body.encode("utf-8")
        except UnicodeError:
            raise TapdError("invalid_input") from None
        if len(payload) > self._max_output_bytes:
            raise TapdError("input_limit")
        data, _size = self._request(
            workspace_id,
            ["comment", "add", f"--entry-type={entry_type}", f"--entry-id={_id(external_id)}"],
            payload=payload,
            stop=stop,
        )
        if data.get("success") is not True or data.get("workspace_id", workspace_id) != workspace_id:
            raise TapdError("invalid_response", effect_unknown=True)
        return _id(data.get("id"), code="invalid_response", effect_unknown=True)
