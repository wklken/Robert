"""Pure TAPD comment classification and tag evidence policy."""

from dataclasses import dataclass, field
from html.parser import HTMLParser
import re
import unicodedata

from robert_agent.foundation.tapd_identity import DEFAULT_BOT_NAME, comment_name

COMMENT_NORMALIZATION_VERSION = 1

# Normalize compatibility forms, then remove explicitly recognized zero-width
# separators and bidi formatting that can split visually continuous tokens.
_ZERO_WIDTH = dict.fromkeys(
    map(
        ord,
        "\u00ad\u061c\u180e\u200b\u200c\u200d\u200e\u200f"
        "\u202a\u202b\u202c\u202d\u202e\u2060\u2061\u2062\u2063\u2064\u2066\u2067\u2068\u2069\ufeff",
    )
)
_BLOCKS = frozenset(
    {
        "blockquote",
        "div",
        "p",
        "pre",
        "li",
        "ol",
        "ul",
        "section",
        "hr",
        "table",
        "tr",
        "td",
        "th",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "br",
    }
)
_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
)
_CONTEXT_TAGS = {
    "blockquote": "quote",
    "q": "quote",
    "pre": "code",
    "code": "code",
    "script": "hidden",
    "style": "hidden",
    "template": "hidden",
}
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_INLINE_CODE = re.compile(r"(?<!`)(`+)(?!`)(.*?)(?<!`)\1(?!`)")


def normalize_comment_text(text):
    return unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH).replace("\r\n", "\n").replace("\r", "\n")


@dataclass(frozen=True)
class CommentSegment:
    kind: str
    text: str = field(repr=False)


@dataclass(frozen=True)
class ParsedComment:
    segments: tuple[CommentSegment, ...]
    own_text: str = field(repr=False)
    structure_verified: bool
    is_bot_prefixed: bool
    has_trigger: bool


class _CommentHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.context = []
        self.has_tags = False
        self.verified = True

    def handle_data(self, text):
        kind = self.context[-1][1] if self.context else "own"
        if self.parts and self.parts[-1][0] == kind:
            self.parts[-1][1].append(text)
        else:
            self.parts.append((kind, [text]))

    def handle_starttag(self, tag, attrs):
        self.has_tags = True
        if tag in _BLOCKS:
            self.handle_data("\n")
        if tag in _CONTEXT_TAGS:
            self.context.append((tag, _CONTEXT_TAGS[tag]))

    def handle_endtag(self, tag):
        if tag in _CONTEXT_TAGS:
            if not self.context or self.context[-1][0] != tag:
                self.verified = False
                return
            self.context.pop()
        if tag in _BLOCKS:
            self.handle_data("\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)


def _markdown_segments(text):
    fence = None
    quoted = False
    for line in text.splitlines(keepends=True):
        marker = _FENCE.match(line)
        if fence is not None:
            yield CommentSegment("code", line)
            if marker and marker[1].startswith(fence) and not line[marker.end() :].strip():
                fence = None
            continue
        if marker:
            fence = marker[1]
            yield CommentSegment("code", line)
        elif line.lstrip().startswith(">") or (quoted and line.strip()):
            # Markdown blockquotes can lazily continue on unmarked lines.
            quoted = True
            yield CommentSegment("quote", line)
        elif line.startswith(("    ", "\t")):
            yield CommentSegment("code", line)
        else:
            quoted = False
            offset = 0
            for match in _INLINE_CODE.finditer(line):
                yield CommentSegment("own", line[offset : match.start()])
                yield CommentSegment("code", match[2])
                offset = match.end()
            yield CommentSegment("own", line[offset:])


def _mention_marker(bot_name):
    selected = re.escape(comment_name(bot_name, "bot_name"))
    return re.compile(rf"(?<![\w@])@(?ai:{selected})(?![\w-]|\.[\w-])")


def parse_comment(body, *, reply_id=None, bot_name=DEFAULT_BOT_NAME, reply_name=None):
    """Classify a comment body without conferring authorization on its author."""
    marker = _mention_marker(bot_name)
    signature = comment_name(bot_name if reply_name is None else reply_name, "reply_name")
    parser = _CommentHTML()
    parser.feed(body)
    parser.close()
    parts = []
    for kind, chunks in parser.parts:
        text = normalize_comment_text("".join(chunks))
        if kind == "own" and not parser.has_tags:
            parts.extend(_markdown_segments(text))
        else:
            parts.append(CommentSegment(kind, text))

    reply_format_known = reply_id in (None, "", "0")
    if not reply_format_known:
        parts = [CommentSegment("reply" if part.kind == "own" else part.kind, part.text) for part in parts]
    verified = parser.verified and not parser.context and reply_format_known
    own_text = "".join(part.text if part.kind == "own" else "\n" for part in parts).strip()
    prefixed = own_text.casefold().startswith(f"{signature.casefold()}:")
    return ParsedComment(
        tuple(part for part in parts if part.text.strip()),
        own_text,
        verified,
        prefixed,
        verified and not prefixed and bool(marker.search(own_text)),
    )


def scrub_comment_markers(body, *, bot_name=DEFAULT_BOT_NAME):
    """Extract visible text and strip trigger tokens throughout the outbound body."""
    marker = _mention_marker(bot_name)
    parser = _CommentHTML()
    parser.feed(body)
    parser.close()
    visible = normalize_comment_text("".join("".join(parts) for kind, parts in parser.parts if kind != "hidden"))
    # Case-insensitive matching must not alter the surrounding visible text.
    return marker.sub(lambda match: match[0][1:], visible).strip()


def matching_tag_rule(*, owners, labels, status, terminal_states, archived, owner, label):
    """Authorize only from explicit, exact owner/label and workflow facts."""
    return (
        archived is False
        and bool(status)
        and bool(terminal_states)
        and status not in terminal_states
        and owners is not None
        and not isinstance(owners, str)
        and owner in owners
        and labels is not None
        and not isinstance(labels, str)
        and label in labels
    )
