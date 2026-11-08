"""Authenticated host identity shared by chat commands and durable dispatch."""

from robert_agent.work.errors import WorkItemValidationError

IDENTITY_FIELDS = ("provider", "channel", "account", "conversation", "thread", "sender_id")


def authenticate(config, request):
    settings = config.get("messaging", {})
    if not settings.get("enabled") or request.get("protocol") != "robert.message.v1":
        raise WorkItemValidationError("Messaging is disabled or the protocol is unsupported.")
    for field in IDENTITY_FIELDS[:-1]:
        if request.get(field, "") != settings.get(field, ""):
            raise WorkItemValidationError("Messaging identity is not authorized.")
    if request.get("sender_id") not in settings["allowed_senders"]:
        raise WorkItemValidationError("Messaging identity is not authorized.")
    return f"{settings['provider']}:{settings['channel']}:{settings['account']}:{request['sender_id']}"


def dispatch_allowed(config, identity, repo):
    try:
        authenticate(config, identity)
    except WorkItemValidationError:
        return False
    settings = config.get("messaging", {})
    return bool(
        settings.get("dispatch_enabled")
        and repo in settings.get("repos", [])
        and repo in {item["full_name"] for item in config.get("repos", [])}
    )
