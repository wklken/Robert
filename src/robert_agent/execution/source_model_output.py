"""Validate source model output, bound to the exact structured prompt and evidence."""

from hashlib import sha256
import json
from pathlib import PurePosixPath
import re

from robert_agent.foundation.contracts.execution import SourceModelStage
from robert_agent.security.redaction import redact_text

MAX_OUTPUT_BYTES = 8192
MAX_ANSWER_CHARS = 4000
MAX_QUESTION_CHARS = 700


def reference(evidence):
    try:
        repo, path, revision = (evidence[key] for key in ("repo_id", "path", "revision"))
    except (KeyError, TypeError):
        raise ValueError("invalid source evidence reference") from None
    if (
        not isinstance(repo, str)
        or not repo
        or not isinstance(path, str)
        or not path
        or PurePosixPath(path).is_absolute()
        or any(part.casefold() in ("", ".", "..", ".git") for part in path.split("/"))
        or any(ord(char) < 32 for char in path)
        or "\\" in path
        or not isinstance(revision, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", revision) is None
    ):
        raise ValueError("invalid source evidence reference")
    return repo, path, revision


def validate_evidence(evidence, candidates):
    """Reject foreign, forged, duplicated or mixed byte revisions before rendering."""
    seen = {}
    for record in evidence:
        if not isinstance(record, dict) or set(record) != {"repo_id", "path", "revision", "text"}:
            raise ValueError("invalid source evidence")
        repo, path, revision = reference(record)
        text = record["text"]
        if (
            repo not in candidates
            or not isinstance(text, str)
            or revision != "sha256:" + sha256(text.encode()).hexdigest()
            or redact_text(text, strict_assignments=True).get("text") != text
            or (repo, path) in seen
        ):
            raise ValueError("inconsistent source evidence")
        seen[(repo, path)] = revision
    return {(repo, path, revision) for (repo, path), revision in seen.items()}


def _object(pairs):
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError("duplicate source model output key")
    return obj


def _text(value, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\0" in value:
        raise ValueError("invalid source model prose")
    value.encode("utf-8")


def _references(value, supplied):
    if not isinstance(value, list) or not value or len(value) > 12:
        raise ValueError("invalid source model references")
    claimed = set()
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"repo_id", "path", "revision"}:
            raise ValueError("invalid source model reference shape")
        item = reference(entry)
        if item not in supplied or item in claimed:
            raise ValueError("unprovided or duplicate source model evidence")
        claimed.add(item)
    return {identifier for identifier, _path, _revision in claimed}


def handled_members(value, members):
    """Select complete frozen members by their explicitly supplied ledger fingerprints."""
    if not isinstance(value, list) or not value or any(not isinstance(item, str) for item in value):
        raise ValueError("missing or invalid handled event fingerprints")
    wanted = set(value)
    if len(wanted) != len(value):
        raise ValueError("duplicate handled event fingerprint")
    available, request_ids, handled = set(), set(), []
    for member in members:
        if (
            not isinstance(member, dict)
            or set(member) != {"request_id", "content_version", "events"}
            or any(not isinstance(member[key], str) or not member[key] for key in ("request_id", "content_version"))
            or member["request_id"] in request_ids
        ):
            raise ValueError("invalid frozen model request member")
        request_ids.add(member["request_id"])
        events = member["events"]
        if not isinstance(events, list) or not events:
            raise ValueError("empty frozen model request member")
        if any(
            not isinstance(event, dict)
            or set(event) != {"row_id", "event_id", "fingerprint"}
            or type(event["row_id"]) is not int
            or not isinstance(event["event_id"], str)
            or not event["event_id"]
            or not isinstance(event["fingerprint"], str)
            or not event["fingerprint"]
            for event in events
        ):
            raise ValueError("invalid frozen model event")
        fingerprints = {event["fingerprint"] for event in events}
        if len(fingerprints) != len(events) or available.intersection(fingerprints):
            raise ValueError("ambiguous frozen model event fingerprints")
        available.update(fingerprints)
        if wanted.intersection(fingerprints):
            if not fingerprints <= wanted:
                raise ValueError("incomplete handled source member")
            handled.append(member)
    if not wanted <= available:
        raise ValueError("handled event is outside the frozen source members")
    return handled


def validate(raw, *, stage, binding, candidates, supplied, members=None):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_OUTPUT_BYTES:
        raise ValueError("source model output size")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("invalid source model JSON") from None
    if not isinstance(value, dict) or any(value.get(key) != expected for key, expected in binding.items()):
        raise ValueError("source model output binding mismatch")
    extra = value.keys() - binding.keys()
    kind = value.get("kind")
    if members is not None and kind in ("answer", "question"):
        handled_members(value.get("handled_event_fingerprints"), members)
        extra = extra - {"handled_event_fingerprints"}
    if (
        stage in (SourceModelStage.IDENTIFY, SourceModelStage.SYNTHESIS)
        and kind == "question"
        and extra == {"kind", "question"}
    ):
        _text(value["question"], MAX_QUESTION_CHARS)
    elif stage == SourceModelStage.IDENTIFY and kind == "selection" and extra == {"kind", "repositories", "references"}:
        selected = value["repositories"]
        if (
            not isinstance(selected, list)
            or not selected
            or not all(isinstance(repo, str) for repo in selected)
            or len(set(selected)) != len(selected)
            or any(repo not in candidates for repo in selected)
            or set(selected) != _references(value["references"], supplied)
        ):
            raise ValueError("source model repository selection lacks authorized evidence")
    elif stage == SourceModelStage.SYNTHESIS and kind == "answer" and extra == {"kind", "answer", "references"}:
        _text(value["answer"], MAX_ANSWER_CHARS)
        _references(value["references"], supplied)
    else:
        raise ValueError("unknown source model output shape")
    return value
