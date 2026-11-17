"""Explicit backport command syntax and stable per-target request identities."""

from dataclasses import dataclass
import hashlib
import re

from robert_agent.work import tapd_policy


@dataclass(frozen=True)
class BackportRequest:
    source_pr: int
    targets: tuple[str, ...]


def valid_branch(value):
    """Accept literal branch names, never revisions, options or ref expressions."""
    return bool(
        value
        and len(value) <= 255
        and not value.startswith(("-", "/", "."))
        and not value.endswith(("/", "."))
        and not any(part in value for part in ("..", "@{", "//"))
        and not re.search(r"[\s\x00-\x20\x7f~^:?*\[\\]", value)
        and value != "@"
        and all(not part.startswith(".") and not part.endswith(".lock") for part in value.split("/"))
    )


def parse_request(body, mention_name):
    """Parse a complete command line outside quoted/code blocks; other prose stays inert."""
    text = tapd_policy.own_comment_text(body)
    marker = re.compile(rf"(?<![\w@])@(?ai:{re.escape(mention_name)})(?![\w-])")
    commands = []
    for line in text.splitlines():
        line = marker.sub("", line).strip()
        match = re.fullmatch(
            r"(?:please\s+|请\s*)?(?:cherry[- ]pick|backport|回移)\s+#([1-9][0-9]*)\s*(?:to\s+|到\s*)(.+)",
            line,
            re.I,
        )
        if match:
            targets = tuple(dict.fromkeys(re.split(r"\s*(?:[,，、]|\band\b|和)\s*", match[2])))
            if not targets or not all(valid_branch(target) for target in targets):
                return None
            commands.append(BackportRequest(int(match[1]), targets))
    return commands[0] if len(commands) == 1 else None


def target_key(base):
    return hashlib.sha256(base.encode()).hexdigest()[:16]


def target_stream(ticket_stream, base):
    return f"{ticket_stream}:backport:{target_key(base)}"
