"""Revalidate administrator policy against the database owned by the caller."""

from robert_agent.foundation.contracts.supervision import SupervisionMode

from pathlib import Path
from robert_agent.persistence.dialects import sqlite as db_sqlite


def current_config(conn, config):
    config = config or {}
    config_path = config.get("config_path")
    if not config_path or (config.get("supervision") or {}).get("mode") not in {
        SupervisionMode.RECOVER_KNOWN.value,
        SupervisionMode.DECIDE.value,
    }:
        # Explicit local observe/off gates also serve paused consumers.
        return config
    try:
        from robert_agent.configuration.command import validate_config

        current = validate_config(config_path, skip_external=True)
        main_path = next((row[2] for row in db_sqlite.database_list(conn) if row[1] == "main"), "")
        if current["ok"] and main_path and Path(current["db_path"]).resolve() == Path(main_path).resolve():
            return current
    except (OSError, RuntimeError, ValueError):
        pass
    return None
