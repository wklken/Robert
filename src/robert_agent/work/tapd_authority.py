"""Pure identity, observation and current-authority decisions for TAPD intake."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
from zoneinfo import ZoneInfo

from robert_agent.foundation.contracts.work import SourceAuthorizationType, SourceCapability
from robert_agent.foundation.tapd_identity import canonical_host, workspace_id_is_valid
from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.work.tapd_policy import COMMENT_NORMALIZATION_VERSION, matching_tag_rule, parse_comment


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class TicketIdentity:
    host: str
    workspace_id: str
    kind: str
    external_id: str

    def __post_init__(self):
        object.__setattr__(self, "host", canonical_host(self.host))
        if not all(workspace_id_is_valid(value) for value in (self.workspace_id, self.external_id)):
            raise ValueError("TAPD workspace and ticket identity must use digit strings")
        if self.kind not in ("story", "bug"):
            raise ValueError("Unsupported TAPD ticket kind")

    @property
    def key(self):
        return (self.host, self.workspace_id, self.kind, self.external_id)

    @property
    def source_id(self):
        return "tapd:" + digest(self.key)

    @property
    def entry_types(self):
        return ("stories",) if self.kind == "story" else ("bug", "bug_remark")


@dataclass(frozen=True, slots=True)
class CommentVersion:
    ticket: TicketIdentity
    entry_type: str
    external_id: str
    created: str
    modified: str | None
    body_sha256: str
    reply_id: str | None = None

    def __post_init__(self):
        if self.entry_type not in self.ticket.entry_types or not workspace_id_is_valid(self.external_id):
            raise ValueError("TAPD comment must match its ticket and use a digit-string ID")

    @property
    def event_id(self):
        return "tapd-comment:" + digest((*self.ticket.key, self.entry_type, self.external_id))


@dataclass(frozen=True, slots=True)
class CreatorEvidence:
    observation: CommentVersion
    principal: str
    evidence_id: str


@dataclass(frozen=True, slots=True)
class OwnBodyEvidence:
    observation: CommentVersion
    principal: str
    evidence_id: str


@dataclass(frozen=True, slots=True)
class CommentObservation:
    version: CommentVersion
    body_html: str = field(repr=False)
    display_author: str | None = field(default=None, repr=False)
    creator: CreatorEvidence | None = field(default=None, repr=False)
    own_body: OwnBodyEvidence | None = field(default=None, repr=False)

    def __post_init__(self):
        if self.version.body_sha256 != sha256(self.body_html.encode()).hexdigest():
            raise ValueError("TAPD comment proof must bind exact body bytes")

    @classmethod
    def from_cli(cls, ticket, row):
        if (row.workspace_id, row.kind, row.entry_id) != (ticket.workspace_id, ticket.kind, ticket.external_id):
            raise ValueError("TAPD CLI comment scope mismatch")
        version = CommentVersion(
            ticket,
            row.entry_type,
            row.external_id,
            row.created,
            row.modified,
            sha256(row.description.encode()).hexdigest(),
            row.reply_id,
        )
        return cls(version, row.description, row.display_author)

    def payload(self, *, bot_name, reply_name, bot_owned):
        version = self.version
        creator = self.creator
        own = self.own_body
        creator_known = bool(creator and creator.observation == version and creator.principal and creator.evidence_id)
        own_known = bool(
            creator_known
            and own
            and own.observation == version
            and own.principal == creator.principal
            and own.evidence_id
        )
        parsed = parse_comment(self.body_html, reply_id=version.reply_id, bot_name=bot_name, reply_name=reply_name)
        return {
            "body_html": self.body_html,
            "body_sha256": version.body_sha256,
            "created": version.created,
            "modified": version.modified,
            "display_author": self.display_author,
            "reply_id": version.reply_id,
            "entry_type": version.entry_type,
            "external_id": version.external_id,
            "creator": creator.principal if creator_known else None,
            "creator_evidence": creator.evidence_id if creator_known else None,
            "own_body_evidence": own.evidence_id if own_known else None,
            "normalized_text": parsed.own_text,
            "normalization_version": COMMENT_NORMALIZATION_VERSION,
            "structure_verified": parsed.structure_verified,
            "bot_prefix": parsed.is_bot_prefixed,
            "bot_owned": bot_owned,
            "bot_name": bot_name,
            "reply_name": reply_name,
            "mentions_bot": parsed.has_trigger,
        }


@dataclass(frozen=True, slots=True)
class TicketFacts:
    identity: TicketIdentity
    title: str | None
    body: str | None = field(repr=False)
    status: str | None
    owners: tuple[str, ...] | None = None
    labels: tuple[str, ...] | None = None
    archived: bool | None = None
    terminal_states: tuple[str, ...] | None = None

    @classmethod
    def from_cli(cls, identity, row):
        if (row.workspace_id, row.kind, row.external_id) != (
            identity.workspace_id,
            identity.kind,
            identity.external_id,
        ):
            raise ValueError("TAPD CLI ticket scope mismatch")
        return cls(identity, row.title, row.description, row.status)


@dataclass(frozen=True, slots=True)
class CommentAbsence:
    ticket: TicketIdentity
    entry_type: str
    external_id: str
    evidence_id: str

    def __post_init__(self):
        if (
            self.entry_type not in self.ticket.entry_types
            or not workspace_id_is_valid(self.external_id)
            or not self.evidence_id
        ):
            raise ValueError("TAPD absence requires scoped authoritative evidence")

    @property
    def event_id(self):
        return "tapd-comment:" + digest((*self.ticket.key, self.entry_type, self.external_id))


@dataclass(frozen=True, slots=True)
class TicketObservation:
    facts: TicketFacts
    # Time the external snapshot was begun. Delayed observations cannot overwrite a newer one.
    observed_at: str
    comments: tuple[CommentObservation, ...] = ()
    coverage: str = "incomplete"
    absences: tuple[CommentAbsence, ...] = ()


@dataclass(frozen=True, slots=True)
class PublicationFacts:
    ticket: TicketIdentity
    own_comments: frozenset[tuple[str, str]] = frozenset()
    unknown_intents: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AuthorityDecision:
    reason: str
    capability: str | None = None
    repos: tuple[str, ...] = ()
    rule: dict | None = None


def remote_timestamp(value, api_timezone):
    aware = utc_timestamp(value)
    if aware is not None:
        return aware
    if not isinstance(value, str) or value != value.strip() or (" " not in value and "T" not in value):
        return None
    try:
        naive = datetime.fromisoformat(value)
        if naive.tzinfo is not None:
            return naive.astimezone(timezone.utc)
        tz = ZoneInfo(api_timezone)
        first, last = naive.replace(tzinfo=tz, fold=0), naive.replace(tzinfo=tz, fold=1)
        return first.astimezone(timezone.utc) if first.utcoffset() == last.utcoffset() else None
    except (ValueError, OverflowError):
        return None


def ticket_state(facts):
    if facts.archived is True or (
        facts.status and isinstance(facts.terminal_states, tuple) and facts.status in facts.terminal_states
    ):
        return "terminal"
    if (
        facts.archived is not False
        or not facts.status
        or not isinstance(facts.terminal_states, tuple)
        or not facts.terminal_states
        or not all(isinstance(status, str) and status for status in facts.terminal_states)
    ):
        return "ticket_state_unknown"
    return "open"


def label_authority(facts, project):
    state = ticket_state(facts)
    if state != "open":
        return AuthorityDecision(state)
    rules = [rule for rule in project["rules"] if facts.identity.kind in rule["kinds"]]
    if rules and (not isinstance(facts.owners, tuple) or not isinstance(facts.labels, tuple)):
        return AuthorityDecision("label_facts_unknown")
    matches = [
        rule
        for rule in rules
        if matching_tag_rule(
            owners=facts.owners,
            labels=facts.labels,
            status=facts.status,
            terminal_states=facts.terminal_states,
            archived=facts.archived,
            owner=rule["assignee"],
            label=rule["label"],
        )
    ]
    repos = tuple(sorted({rule["repo"] for rule in matches}))
    if len(repos) > 1:
        return AuthorityDecision("repository_conflict", repos=repos)
    if not repos:
        return AuthorityDecision("no_label_match")
    rule = sorted(matches, key=lambda item: item["id"])[0]
    identity = {key: rule[key] for key in ("id", "assignee", "label", "repo")}
    return AuthorityDecision("allowed", SourceCapability.DEVELOPMENT.value, repos, identity)


def comment_authority(payload, project):
    if payload.get("deleted"):
        return "comment_deleted"
    if payload.get("bot_owned") or payload.get("bot_prefix"):
        return "automated_comment"
    if not payload.get("structure_verified"):
        return "comment_structure_unknown"
    if not payload.get("mentions_bot"):
        return "mention_removed"
    if not payload.get("creator"):
        return "creator_unknown"
    if payload["creator"] != project["trusted_author"]:
        return "creator_changed"
    if not payload.get("own_body_evidence"):
        return "body_provenance_unknown"
    return "allowed"


def request_authority(auth_type, authorization, *, facts, project, label, binding, comment):
    state = ticket_state(facts)
    if state != "open":
        return AuthorityDecision(state)
    if auth_type == SourceAuthorizationType.TAPD_MENTION:
        reason = comment_authority(comment, project) if comment is not None else "comment_unknown"
        if reason != "allowed":
            return AuthorityDecision(reason)
        candidate = tuple(sorted(set(authorization.get("repos", ())) & set(project["repositories"])))
        if not candidate:
            return AuthorityDecision("repository_scope_removed")
        if authorization.get("rule") is None:
            if label.reason in ("repository_conflict", "label_facts_unknown"):
                return label
            return AuthorityDecision("allowed", SourceCapability.READONLY.value, candidate)
    elif auth_type != SourceAuthorizationType.LABEL_QUEUE:
        return AuthorityDecision("unsupported_authorization")
    if not binding or binding.get("state") != "active":
        return AuthorityDecision("confirmation_required")
    if label.reason != "allowed":
        return label
    expected = authorization.get("rule", {})
    if binding.get("rule") != expected or label.rule != expected:
        return AuthorityDecision("repository_change_requires_confirmation")
    return label
