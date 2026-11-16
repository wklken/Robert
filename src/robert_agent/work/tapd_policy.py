"""Pure TAPD comment, label and authority policy; no adapter or storage effects."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import hashlib
import re

from robert_agent.foundation.timestamps import utc_timestamp

TAPD_TIMEZONE = timezone(timedelta(hours=8))
TICKET_ROUTE_IDS = frozenset({"classification-result", "comment-analysis", "waiting-for-user", "new-pr"})
DERIVED_PR_ROUTE_IDS = frozenset(
    {"classification-result", "comment-analysis", "waiting-for-user", "update-existing-pr"}
)
ROUTE_IDS = TICKET_ROUTE_IDS | DERIVED_PR_ROUTE_IDS
DIRECT_INTENTS = frozenset(
    {"analysis", "discussion", "requirement_analysis", "bug_fix", "small_change", "add_tests", "pr_followup_fix"}
)
LABEL_NOTICE = "tapd_repository_label"
MULTIPLE_PRS_NOTICE = "tapd_multiple_pull_requests"
PR_MERGED_NOTICE = "tapd_pr_merged"
PR_CLOSED_NOTICE = "tapd_pr_closed"
REMEDIATION_RESOLVED_NOTICE = "tapd_remediation_resolved"
REMEDIATION_STOPPED_NOTICE = "tapd_remediation_stopped"
REMEDIATION_BUDGET_NOTICE = "tapd_remediation_budget_exhausted"
PR_REVIEW_NOTICE = "tapd_pr_review_submitted"
_BLOCKS = frozenset(
    {"blockquote", "br", "div", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "li", "ol", "p", "pre", "table", "tr", "ul"}
)
_HIDDEN = frozenset({"blockquote", "code", "pre", "q", "script", "style", "template"})
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_INLINE_CODE = re.compile(r"(`+)(?!`).*?(?<!`)\1(?!`)")


@dataclass(frozen=True)
class ParsedComment:
    text: str = field(repr=False)
    automatic: bool
    mentions: bool


class _Visible(HTMLParser):
    """Collect visible text and, separately, text outside quotes and code."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.visible, self.own, self.hidden, self.tags = [], [], 0, False

    def _break(self):
        self.visible.append("\n")
        self.own.append("\n")

    def handle_starttag(self, tag, attrs):
        self.tags = True
        if tag in _BLOCKS:
            self._break()
        if tag in _HIDDEN:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in _HIDDEN and self.hidden:
            self.hidden -= 1
        if tag in _BLOCKS:
            self._break()

    def handle_data(self, data):
        self.visible.append(data)
        if not self.hidden:
            self.own.append(data)


def _plain_own_text(text):
    """Remove Markdown quotes and code from a body that has no HTML structure."""
    kept, fence, quoted = [], None, False
    for line in text.splitlines():
        marker = _FENCE.match(line)
        if fence:
            fence = None if marker and marker[1].startswith(fence) else fence
            continue
        if marker:
            fence = marker[1]
        elif line.lstrip().startswith(">") or (quoted and line.strip()):
            quoted = True
        elif not line.startswith(("    ", "\t")):
            quoted = False
            kept.append(_INLINE_CODE.sub(" ", line))
    return "\n".join(kept)


def _squeeze(text):
    lines = [line.strip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def own_comment_text(body):
    """Visible command text outside HTML/Markdown quotations and code."""
    parser = _Visible()
    parser.feed(body or "")
    parser.close()
    return _squeeze("".join(parser.own) if parser.tags else _plain_own_text(body or ""))


def _marker(mention_name):
    return re.compile(rf"(?<![\w@])@(?ai:{re.escape(mention_name)})(?![\w-]|\.[\w-])")


def automatic_prefix(mention_name):
    return f"{mention_name}："


def parse_comment(body, mention_name):
    """Recognize the visible ASCII mention; full-width and zero-width variants stay inert."""
    parser = _Visible()
    parser.feed(body or "")
    parser.close()
    text = _squeeze("".join(parser.visible))
    own = _squeeze("".join(parser.own) if parser.tags else _plain_own_text(body or ""))
    automatic = own.casefold().startswith((f"{mention_name}：".casefold(), f"{mention_name}:".casefold()))
    return ParsedComment(text, automatic, not automatic and bool(_marker(mention_name).search(own)))


def neutralize_mentions(text, mention_name):
    return _marker(mention_name).sub(lambda match: match[0][1:], text)


def allowed_routes(event):
    """A request in a derived PR's queue updates that PR; otherwise it follows issue rules."""
    return DERIVED_PR_ROUTE_IDS if event.get("has_open_dd_pr") else TICKET_ROUTE_IDS


def split_labels(raw):
    return [label.strip() for label in (raw or "").split("|") if label.strip()]


def select_repository(raw_labels, label_rules):
    labels = set(split_labels(raw_labels))
    matched = [rule for rule in label_rules if rule["label"] in labels]
    if not matched:
        return "missing", None, None
    if len(matched) > 1:
        return "conflict", None, None
    return "selected", matched[0]["repository"], matched[0]["label"]


def parse_time(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TAPD_TIMEZONE)
    except (TypeError, ValueError):
        return None


def is_ended(status, terminal_states):
    return bool(status) and status in (terminal_states or {})


def _decision(status, *, creates_task=False, drives_execution=False):
    return {"authorization_status": status, "creates_task": creates_task, "drives_execution": drives_execution}


def _authority(*, author, created, automatic, mentions, project, known):
    started = utc_timestamp(project.get("accept_after"))
    observed = parse_time(created)
    if automatic:
        return _decision("context_only")
    if author != project["trusted_user"] or not started or not observed or observed < started or not mentions:
        return _decision("ignored_context")
    if known:
        return _decision("accepted_context", drives_execution=True)
    return _decision("authorized_trigger", creates_task=True, drives_execution=True)


def authorize_comment(comment, project, *, mention_name, automatic_ids, known):
    parsed = parse_comment(comment["body"], mention_name)
    return _authority(
        author=comment["author"],
        created=comment["created"],
        automatic=parsed.automatic or comment["comment_id"] in automatic_ids,
        mentions=parsed.mentions,
        project=project,
        known=known,
    )


def notice_id(kind, dedupe_key):
    return "tapd-notice-" + hashlib.sha256(f"{kind}\0{dedupe_key}".encode()).hexdigest()[:32]


def project_for(config, project_key):
    for project in (config or {}).get("projects", []):
        if f"{project['host']}/{project['workspace_id']}" == project_key:
            return project
    return None


def reauthorize(event, config, *, known_workstreams, ended=False):
    """Check a saved request against current configuration and the stored end state; never re-read TAPD."""
    project = project_for(config, event.get("project_key"))
    rule = next(
        (
            rule
            for rule in (project or {}).get("label_rules", [])
            if rule["label"] == event.get("repository_label") and rule["repository"] == event.get("repo")
        ),
        None,
    )
    if not (config or {}).get("enabled") or not project or not rule:
        return {**event, **_decision("ignored_untrusted_trigger")}
    if ended:
        return {**event, "state": "closed", **_decision("ignored_current_state_closed")}
    return {
        **event,
        "state": "open",
        "tapd_trusted_user": project["trusted_user"],
        **_authority(
            author=event.get("actor_login"),
            created=event.get("comment_created"),
            automatic=bool(event.get("automatic")),
            mentions=bool(event.get("mentions_dd")),
            project=project,
            known=event.get("workstream_id") in set(known_workstreams),
        ),
    }
