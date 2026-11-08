"""Compose remote delivery and Issue dispatch without blocking the local lane."""

from robert_agent.configuration import command as validation
from robert_agent.delivery.application import lane
from robert_agent.work.application import dispatch_requests


def publish(config_path, *, dry_run=False):
    result = lane.publish(config_path, dry_run=dry_run)
    if not dry_run and (result.get("ok") or result.get("failure_scope") == "task"):
        config = validation.validate_config(config_path, skip_external=True)
        if config.get("ok"):
            result["dispatch_requests_processed"] = dispatch_requests.advance(config)
    return result
