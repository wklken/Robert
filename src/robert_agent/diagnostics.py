from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import zipfile

import yaml

from robert_agent import redaction
from robert_agent import status
from robert_agent import validate_config


def _safe_json(payload):
    text = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    redacted = redaction.redact_text(text)
    if not redacted["ok"]:
        return json.dumps(
            {"status": "omitted_sensitive"},
            sort_keys=True,
        )
    return redacted["text"]


def _config_summary(config_path):
    try:
        raw = yaml.safe_load(
            Path(config_path).read_text(encoding="utf-8")
        )
    except (OSError, ValueError, yaml.YAMLError):
        raw = {}
    github = raw.get("github") if isinstance(raw, dict) else {}
    repos = raw.get("repos") if isinstance(raw, dict) else []
    workers = raw.get("workers") if isinstance(raw, dict) else {}
    return {
        "version": raw.get("version") if isinstance(raw, dict) else None,
        "github_account": (
            github.get("account")
            if isinstance(github, dict)
            else None
        ),
        "repositories": [
            repo.get("full_name")
            for repo in repos or []
            if isinstance(repo, dict)
        ],
        "workers": (
            list(workers)
            if isinstance(workers, dict)
            else []
        ),
    }


def _database_payloads(config_path):
    validated = validate_config.validate_config(
        config_path,
        skip_external=True,
    )
    if not validated.get("ok"):
        return {}, [], "unknown"
    db_path = Path(validated["db_path"])
    if not db_path.is_file():
        return {}, [], "unknown"
    with closing(sqlite3.connect(db_path)) as conn:
        status_payload = status.build_status(db_path)
        runs_payload = status.build_runs(conn, limit=5)
        row = conn.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()
    return status_payload, runs_payload, str(row[0] or "unknown")


def export_diagnostics(config_path, output_path):
    output = Path(output_path).expanduser()
    status_payload, runs_payload, schema_version = _database_payloads(
        config_path
    )
    files = {
        "manifest.json": _safe_json(
            {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "files": [
                    "config-summary.json",
                    "status.json",
                    "recent-runs.json",
                    "schema-version.txt",
                ],
            }
        ),
        "config-summary.json": _safe_json(
            _config_summary(config_path)
        ),
        "status.json": _safe_json(status_payload),
        "recent-runs.json": _safe_json(runs_payload),
        "schema-version.txt": schema_version,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    with zipfile.ZipFile(
        temporary,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    temporary.replace(output)
    return {
        "ok": True,
        "status": "exported",
        "output_path": str(output),
        "files": sorted(files),
    }
