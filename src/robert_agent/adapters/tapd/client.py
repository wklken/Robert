"""TAPD CLI protocol: explicit workspace, fixed directory, bounded pages and safe errors."""

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from robert_agent.foundation.commands import ControlDeadlineExceeded, run_command
from robert_agent.foundation.tapd_identity import ENVIRONMENT_ALLOWLIST, numeric_id_is_valid

_ENTRY_TYPES = {"story": "stories", "bug": "bug"}
_KINDS = {entry_type: kind for kind, entry_type in _ENTRY_TYPES.items()}
_TIME = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
_NOT_FOUND = re.compile(r"(?:story|bug) \d+ not found")
PAGE_LIMIT = 200


class TapdError(RuntimeError):
    """A safe adapter failure code; `effect_unknown` marks a write that may have happened."""

    def __init__(self, code, *, effect_unknown=False):
        self.code = code
        self.effect_unknown = effect_unknown
        super().__init__(f"TAPD CLI: {code.replace('_', ' ')}")


@dataclass(frozen=True)
class TapdComment:
    comment_id: str
    kind: str
    entry_id: str
    author: str
    body: str = field(repr=False)
    created: str
    modified: str | None


@dataclass(frozen=True)
class TapdItem:
    kind: str
    external_id: str
    title: str = field(repr=False)
    status: str
    labels: str | None
    description: str = field(repr=False)
    workitem_type_id: str | None
    archived: bool | None
    url: str | None


def _id(value, code="invalid_input"):
    if not numeric_id_is_valid(value):
        raise TapdError(code)
    return value


def _text(row, name, *, required=False):
    value = row.get(name)
    if (value is not None and not isinstance(value, str)) or (required and not value):
        raise TapdError("invalid_response")
    return value


def _comment(raw, workspace_id, entry_type, *, entry_id=None, author=None):
    if not isinstance(raw, dict) or raw.get("workspace_id") != workspace_id or raw.get("entry_type") != entry_type:
        raise TapdError("invalid_response")
    created = _text(raw, "created", required=True)
    row = TapdComment(
        comment_id=_id(raw.get("id"), "invalid_response"),
        kind=_KINDS[entry_type],
        entry_id=_id(raw.get("entry_id"), "invalid_response"),
        author=_text(raw, "author", required=True),
        body=_text(raw, "description") or "",
        created=created,
        modified=_text(raw, "modified"),
    )
    if (
        not _TIME.fullmatch(created)
        or (entry_id is not None and row.entry_id != entry_id)
        or (author is not None and row.author != author)
    ):
        raise TapdError("invalid_response")
    return row


class TapdClient:
    """Run the CLI in an adapter-owned directory so a local `.tapd.json` cannot apply."""

    def __init__(self, *, executable, cwd, environment, runner=None, timeout=30.0, max_output_bytes=8 * 1024 * 1024):
        executable, cwd = Path(executable), Path(cwd)
        if not executable.is_absolute() or not cwd.is_absolute() or not cwd.is_dir():
            raise TapdError("invalid_configuration")
        self._executable = str(executable)
        self._cwd = cwd.resolve()
        self._environment = {key: value for key, value in environment.items() if key in ENVIRONMENT_ALLOWLIST}
        self._runner = runner or subprocess.run
        self._timeout = timeout
        self._max_output_bytes = max_output_bytes

    def _request(self, workspace_id, args, *, payload=None):
        _id(workspace_id)
        write = payload is not None
        try:
            result = run_command(
                [self._executable, "--workspace-id", workspace_id, "--json", *args],
                runner=self._runner,
                cwd=self._cwd,
                env=dict(self._environment),
                input=payload if write else b"",
                capture_output=True,
                timeout=self._timeout,
            )
        except ControlDeadlineExceeded:
            raise
        except subprocess.TimeoutExpired:
            raise TapdError("timeout", effect_unknown=write) from None
        except (OSError, subprocess.SubprocessError):
            raise TapdError("command_failed", effect_unknown=write) from None
        stdout, stderr = result.stdout or b"", result.stderr or b""
        if len(stdout) + len(stderr) > self._max_output_bytes:
            raise TapdError("output_limit", effect_unknown=write)
        if result.returncode:
            try:
                message = json.loads(stderr.decode("utf-8")).get("message")
            except (ValueError, AttributeError):
                message = None
            not_found = isinstance(message, str) and _NOT_FOUND.fullmatch(message)
            raise TapdError("not_found" if not_found and not write else "command_failed", effect_unknown=write)
        try:
            data, _end = json.JSONDecoder().raw_decode(stdout.decode("utf-8").lstrip())
        except (ValueError, RecursionError):
            raise TapdError("invalid_response", effect_unknown=write) from None
        if not isinstance(data, dict):
            raise TapdError("invalid_response", effect_unknown=write)
        return data

    def _pages(self, workspace_id, args, *, page_size, max_pages):
        if not 1 <= page_size <= PAGE_LIMIT or max_pages < 1:
            raise TapdError("invalid_input")
        for number in range(1, max_pages + 1):
            data = self._request(workspace_id, [*args, "--page", str(number), "--limit", str(page_size)])
            rows = data.get("items")
            if data.get("page") != number or not isinstance(rows, list) or len(rows) > page_size:
                raise TapdError("invalid_response")
            yield rows
            if len(rows) < page_size:  # The CLI's has_more is advisory.
                return
        raise TapdError("scan_limit")

    def author_comments(self, workspace_id, author, *, since, page_size=PAGE_LIMIT, max_pages=50):
        """Story and Bug comments by `author`, newest first, down to `since` (TAPD wall-clock text)."""
        if not isinstance(author, str) or not author.strip() or not _TIME.fullmatch(since or ""):
            raise TapdError("invalid_input")
        found, seen = [], set()
        for entry_type in _ENTRY_TYPES.values():
            args = ["comment", "list", f"--author={author}", f"--entry-type={entry_type}", "--order=created desc"]
            pages = self._pages(workspace_id, args, page_size=page_size, max_pages=max_pages)
            for rows in pages:
                parsed = [_comment(raw, workspace_id, entry_type, author=author) for raw in rows]
                for row in parsed:
                    if row.comment_id in seen:
                        raise TapdError("duplicate_record")
                    seen.add(row.comment_id)
                found.extend(row for row in parsed if row.created >= since)
                if parsed and parsed[-1].created < since:
                    pages.close()
                    break
        return tuple(found)

    def ticket_comments(self, workspace_id, kind, external_id, *, page_size=PAGE_LIMIT, max_pages=50):
        entry_type = _ENTRY_TYPES.get(kind)
        if not entry_type:
            raise TapdError("invalid_input")
        args = ["comment", "list", f"--entry-id={_id(external_id)}", f"--entry-type={entry_type}"]
        found = {}
        for rows in self._pages(workspace_id, args, page_size=page_size, max_pages=max_pages):
            for raw in rows:
                row = _comment(raw, workspace_id, entry_type, entry_id=external_id)
                if row.comment_id in found:
                    raise TapdError("duplicate_record")
                found[row.comment_id] = row
        return tuple(sorted(found.values(), key=lambda row: (row.created, row.comment_id)))

    def get_item(self, workspace_id, kind, external_id):
        if kind not in _ENTRY_TYPES:
            raise TapdError("invalid_input")
        raw = self._request(workspace_id, [kind, "show", _id(external_id), "--no-comments"])
        if raw.get("id") != external_id or raw.get("workspace_id") not in (None, workspace_id):
            raise TapdError("invalid_response")
        archived = _text(raw, "is_archived")
        return TapdItem(
            kind=kind,
            external_id=external_id,
            title=_text(raw, "name" if kind == "story" else "title") or "",
            status=_text(raw, "status", required=True),
            labels=_text(raw, "label"),
            description=_text(raw, "description") or "",
            workitem_type_id=_text(raw, "workitem_type_id"),
            archived=None if archived is None else archived == "1",
            url=_text(raw, "url"),
        )

    def terminal_states(self, workspace_id, kind, workitem_type_id=None):
        if kind not in _ENTRY_TYPES or (kind == "story" and workitem_type_id is None):
            raise TapdError("invalid_input")
        args = ["workflow", "last-steps", f"--system={kind}"]
        if workitem_type_id is not None:
            args.append(f"--workitem-type-id={_id(workitem_type_id)}")
        data = self._request(workspace_id, args)
        if not data or any(not isinstance(key, str) or not isinstance(value, str) for key, value in data.items()):
            raise TapdError("invalid_response")
        return data

    def add_comment(self, workspace_id, kind, external_id, body):
        """Return the acknowledged comment ID; callers confirm the effect by reading it back."""
        entry_type = _ENTRY_TYPES.get(kind)
        if not entry_type or not isinstance(body, str) or not body.strip() or "\0" in body:
            raise TapdError("invalid_input")
        data = self._request(
            workspace_id,
            ["comment", "add", f"--entry-type={entry_type}", f"--entry-id={_id(external_id)}"],
            payload=body.encode("utf-8"),
        )
        if data.get("success") is not True:
            raise TapdError("invalid_response", effect_unknown=True)
        return _id(data.get("id"), "invalid_response")


def client_for(settings, data_dir, *, runner=None, environment=None):
    """Build the configured client; the executable and adapter directory must exist locally."""
    executable = shutil.which(settings.get("executable") or "tapd")
    if not executable:
        raise TapdError("invalid_configuration")
    cwd = Path(data_dir) / "tapd"
    cwd.mkdir(parents=True, exist_ok=True)
    return TapdClient(
        executable=Path(executable).resolve(),
        cwd=cwd.resolve(),
        environment=os.environ if environment is None else environment,
        runner=runner,
    )
