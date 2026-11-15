"""Build TAPD receipts and scoped GitHub text from reviewed output facts."""

from dataclasses import dataclass, field
from hashlib import sha256
from html import escape
import re

from robert_agent.delivery.contract import CONTROL_MARKER
from robert_agent.foundation.tapd_identity import DEFAULT_BOT_NAME, comment_name
from robert_agent.security.redaction import redact_text
from robert_agent.work.tapd_policy import (
    COMMENT_NORMALIZATION_VERSION,
    normalize_comment_text,
    parse_comment,
    scrub_comment_markers,
)

NATIVE_COMMIT_MESSAGE = "Update repository code"

_RECEIPT_ID = re.compile(r"rbt-[a-z0-9]{12,64}")
_RECEIPT_LABEL = "Robert 回执"
_PATH = re.compile(r"[a-zA-Z0-9._/-]{1,240}")
_CHANGE_LABELS = {"added": "Added", "modified": "Modified", "deleted": "Deleted"}
_CHECK_LABELS = {
    "unit_tests": "Unit tests",
    "integration_tests": "Integration tests",
    "build": "Build",
    "lint": "Lint",
    "format": "Formatting",
    "type_check": "Type check",
}
_CHECK_RESULTS = frozenset({"passed", "failed", "skipped"})


@dataclass(frozen=True)
class PreparedComment:
    body: str = field(repr=False)
    receipt_id: str
    body_digest: str
    normalization_version: int
    bot_name: str = DEFAULT_BOT_NAME
    reply_name: str = DEFAULT_BOT_NAME


@dataclass(frozen=True)
class ObservedReceipt:
    external_id: str
    target: tuple[str, str, str, str]
    verified_identity: str | None
    body: str = field(repr=False)
    reply_id: str | None = None


@dataclass(frozen=True)
class CommentMatch:
    status: str
    external_id: str | None = None


def _canonical_body(own_text):
    # The CLI converts Markdown to HTML and the server may change wrapping.
    return " ".join(own_text.split())


def _body_digest(own_text):
    return sha256(_canonical_body(own_text).encode()).hexdigest()


def prepare_comment(message, receipt_id, *, bot_name=DEFAULT_BOT_NAME, reply_name=None):
    """Create the controller-owned public body and its readback evidence."""
    if not isinstance(receipt_id, str) or not _RECEIPT_ID.fullmatch(receipt_id):
        raise ValueError("Invalid TAPD receipt ID.")
    signature = comment_name(bot_name if reply_name is None else reply_name, "reply_name")
    identity = {"bot_name": bot_name, "reply_name": signature}
    cleaned = scrub_comment_markers(message, bot_name=bot_name)
    if (
        not cleaned
        or parse_comment(cleaned, **identity).is_bot_prefixed
        or _RECEIPT_LABEL.casefold() in cleaned.casefold()
    ):
        raise ValueError("Invalid TAPD comment content.")
    # Use explicit HTML paragraphs so user-supplied punctuation is rendered
    # literally by the CLI's Markdown-to-HTML conversion.
    text = "<br>".join(escape(line) for line in cleaned.split("\n"))
    body = f"<p>{signature}：</p>\n<p>{text}</p>\n<p>{_RECEIPT_LABEL} {receipt_id}</p>"
    parsed = parse_comment(body, **identity)
    if (
        not parsed.structure_verified
        or not parsed.is_bot_prefixed
        or scrub_comment_markers(body, bot_name=bot_name) != parsed.own_text
    ):
        raise ValueError("Invalid rendered TAPD comment.")
    return PreparedComment(body, receipt_id, _body_digest(parsed.own_text), COMMENT_NORMALIZATION_VERSION, **identity)


def reconcile_comment(observations, *, prepared, target, expected_identity, complete):
    """Match a unique visible receipt and trusted evidence in a complete target read."""
    if (
        complete is not True
        or not expected_identity
        or not isinstance(target, tuple)
        or len(target) != 4
        or any(not isinstance(part, str) or not part.strip() for part in target)
        or target[2] not in ("story", "bug")
        or prepared.normalization_version != COMMENT_NORMALIZATION_VERSION
    ):
        return CommentMatch("ambiguous")

    try:
        identity = {
            "bot_name": comment_name(prepared.bot_name, "bot_name"),
            "reply_name": comment_name(prepared.reply_name, "reply_name"),
        }
    except ValueError:
        return CommentMatch("ambiguous")

    receipt = re.compile(rf"(?<![\w-]){re.escape(prepared.receipt_id)}(?![\w-])")
    candidates = []
    for comment in observations:
        parsed = parse_comment(comment.body, reply_id=comment.reply_id, **identity)
        all_visible = " ".join(part.text for part in parsed.segments if part.kind != "hidden")
        references = receipt.findall(all_visible)
        if references:
            candidates.append((comment, parsed, len(references)))
    if not candidates:
        return CommentMatch("missing")
    if len(candidates) == 1:
        comment, parsed, count = candidates[0]
        if (
            count == 1
            and parsed.structure_verified
            and parsed.is_bot_prefixed
            and scrub_comment_markers(comment.body, bot_name=identity["bot_name"]) == parsed.own_text
            and all(part.kind == "own" for part in parsed.segments)
            and comment.target == target
            and comment.verified_identity == expected_identity
            and bool(comment.external_id)
            and parsed.own_text.splitlines()[-1].strip() == f"{_RECEIPT_LABEL} {prepared.receipt_id}"
            and _body_digest(parsed.own_text) == prepared.body_digest
        ):
            return CommentMatch("matched", comment.external_id)
    return CommentMatch("ambiguous")


@dataclass(frozen=True)
class CodeChange:
    path: str
    operation: str


@dataclass(frozen=True)
class VerificationResult:
    check: str
    outcome: str


@dataclass(frozen=True)
class GithubOutput:
    title: str
    body: str
    commit_message: str


def project_github_output(*, changes, verification, approved_paths, forbidden_text=()):
    """Render a code-only fallback from externally approved paths and known facts."""
    approved = frozenset(approved_paths)
    blocked = tuple(normalize_comment_text(value).casefold() for value in forbidden_text if value)
    lines = []
    has_changes = False
    for change in changes:
        has_changes = True
        if (
            change.operation not in _CHANGE_LABELS
            or change.path not in approved
            or not _PATH.fullmatch(change.path)
            or any(part in ("", ".", "..") for part in change.path.split("/"))
            or any(value in change.path.casefold() for value in blocked)
            or redact_text(change.path, strict_assignments=True).get("text") != change.path
        ):
            raise ValueError("Unapproved GitHub code fact.")
        lines.append(f"- {_CHANGE_LABELS[change.operation]} `{change.path}`")
    for result in verification:
        if result.check not in _CHECK_LABELS or result.outcome not in _CHECK_RESULTS:
            raise ValueError("Unknown GitHub verification fact.")
        lines.append(f"- {_CHECK_LABELS[result.check]}: {result.outcome} (Worker-reported)")
    title = NATIVE_COMMIT_MESSAGE if has_changes else "Repository work status"
    return GithubOutput(title, "\n".join(lines) or "Request processed.", title)


_EXPLICIT_REFERENCE = re.compile(
    r"(?<![\w/])[\w.~-]+(?:/[\w.~-]+)+|"
    r"(?<![\w/])[-\w]+\.(?:py|go|js|ts|tsx|jsx|rs|java|md|yaml|yml|toml|json|sql|rb|cpp|h)\b",
    re.IGNORECASE,
)
_NETWORK_REFERENCE = re.compile(r"[a-z][a-z0-9+.-]*://|file:|git@", re.IGNORECASE)
_ABSOLUTE_REFERENCE = re.compile(r"(?<![\w.~-])(?:[a-z]:)?[\\/](?=[\w./\\~-])", re.IGNORECASE)
_PR_LINK = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*")


def mapped_text_paths(body):
    """Collect explicit lexical file references, including filename-like code references."""
    return frozenset(path.rstrip(".") for path in _EXPLICIT_REFERENCE.findall(body))


def safe_mapped_text(body, approved_paths):
    """Validate TAPD text against separately mapped evidence, without editing stored text."""
    return (
        isinstance(body, str)
        and bool(body.strip())
        and not CONTROL_MARKER.search(body)
        and "robert 回执" not in body.casefold()
        and redact_text(body, strict_assignments=True).get("text") == body
        and not _NETWORK_REFERENCE.search(body)
        and not _ABSOLUTE_REFERENCE.search(body)
        and "\\" not in body
        and all(
            path in approved_paths and redact_text(path, strict_assignments=True).get("text") == path
            for path in mapped_text_paths(body)
        )
    )


def append_confirmed_pr_urls(body, approved_paths, urls):
    """Add caller-verified PR receipts after all candidate-authored text is validated."""
    if not safe_mapped_text(body, approved_paths) or any(
        not isinstance(url, str) or not _PR_LINK.fullmatch(url) for url in urls
    ):
        raise ValueError("Unsafe TAPD publication text or PR receipt URL")
    return "\n\n".join((body, *urls))
