"""Compose remote delivery and Issue dispatch without blocking the local lane."""

from robert_agent.configuration import command as validation
from robert_agent.delivery.application import lane
from robert_agent.work.application import dispatch_requests


def publish(config_path, *, dry_run=False, source_only=False, native_only=False, stop=None, **publication_options):
    result = lane.publish(
        config_path,
        dry_run=dry_run,
        source_only=source_only,
        stop=stop,
        **({"native_only": True} if native_only else {}),
        **publication_options,
    )
    limit = publication_options.get("limit")
    dispatch_enabled = limit is None or limit > 0
    if (
        dispatch_enabled
        and not (dry_run or source_only or native_only or (stop is not None and stop.is_set()))
        and (result.get("ok") or result.get("failure_scope") == "task")
    ):
        config = validation.validate_config(config_path, skip_external=True)
        if config.get("ok"):
            result["dispatch_requests_processed"] = dispatch_requests.advance(config)
    return result
