"""Publish Robert's TAPD comments and find them again by their visible receipt."""

import hashlib
import subprocess

from robert_agent.adapters.tapd import client as tapd_client
from robert_agent.delivery import values as publication_values
from robert_agent.foundation import tapd_identity
from robert_agent.work import tapd_policy

RECEIPT_LABEL = "回执"
PROTOCOL_ERRORS = (tapd_client.TapdError, subprocess.TimeoutExpired, OSError, ValueError)


class Publisher:
    """TAPD comment access for one publication pass; the client is built on first use."""

    def __init__(self, settings, data_dir, *, runner=None, client=None):
        self.settings = settings
        self._data_dir = data_dir
        self._runner = runner
        self._client = client

    def client(self):
        if self._client is None:
            self._client = tapd_client.client_for(self.settings, self._data_dir, runner=self._runner)
        return self._client


def publisher_for(config, runner=None):
    settings = (config or {}).get("tapd") or {}
    return Publisher(settings, config["data_dir"], runner=runner) if settings.get("enabled") else None


def receipt_token(marker):
    return hashlib.sha256(marker.encode("utf-8")).hexdigest()[:16]


def ticket_body(body, marker, mention_name):
    """TAPD drops HTML comments, so the receipt is a visible final paragraph."""
    text = tapd_policy.neutralize_mentions(body.replace(marker, "").strip(), mention_name)
    return f"{tapd_policy.automatic_prefix(mention_name)}\n\n{text}\n\n{RECEIPT_LABEL} {receipt_token(marker)}"


def ticket_target(url, settings):
    """Return (workspace_id, kind, external_id) for a ticket of a configured project, else None."""
    parsed = tapd_identity.parse_ticket_url(url)
    if not parsed:
        return None
    host, workspace_id, kind, external_id = parsed
    if not tapd_policy.project_for(settings, tapd_identity.project_key(host, workspace_id)):
        return None
    return workspace_id, kind, external_id


def receipts(client, target, marker, mention_name):
    """Automatic comments on the ticket that end with this action's receipt."""
    ending = f"{RECEIPT_LABEL} {receipt_token(marker)}"
    found = []
    for comment in client.ticket_comments(*target):
        parsed = tapd_policy.parse_comment(comment.body, mention_name)
        if parsed.automatic and parsed.text.endswith(ending):
            found.append(comment.comment_id)
    return found


def _failure(safe_error, *, retryable=True):
    return {"ok": False, "status": "publish_failed", "safe_error": safe_error, "retryable": retryable, "command": []}


def _published(action, comment_id, *, deduplicated=False):
    return {
        "ok": True,
        "status": "published",
        "deduplicated": deduplicated,
        "external_id": comment_id,
        "target_url": action["target_url"],
        "response": {"tapd_comment_id": comment_id},
        "command": [],
    }


def publish(action, publisher, marker, *, scope_guard):
    body = publication_values.decode_json(action["metadata_json"]).get("body")
    target = ticket_target(action.get("target_url"), publisher.settings) if publisher else None
    if not target or not isinstance(body, str) or not body.strip():
        return _failure("comment target is not a configured TAPD ticket", retryable=False)
    mention_name = publisher.settings["mention_name"]
    try:
        client = publisher.client()
        existing = receipts(client, target, marker, mention_name)
    except PROTOCOL_ERRORS:
        return _failure("TAPD comment preflight failed")
    if len(existing) > 1:
        return _failure("TAPD receipt matches several comments", retryable=False)
    if existing:
        return _published(action, existing[0], deduplicated=True)
    blocked = scope_guard()
    if blocked:
        return blocked
    try:
        comment_id = client.add_comment(*target, ticket_body(body, marker, mention_name))
    except PROTOCOL_ERRORS as exc:
        return _failure(f"TAPD comment write failed: {getattr(exc, 'code', type(exc).__name__)}")
    try:
        confirmed = receipts(client, target, marker, mention_name)
    except PROTOCOL_ERRORS:
        return _published(action, comment_id)
    if confirmed != [comment_id]:
        return _failure("TAPD comment readback did not confirm the write")
    return _published(action, comment_id)
