"""operations scan_batches responsibilities."""

from contextlib import closing
import sqlite3
from robert_agent.adapters.sqlite import database as storage
from robert_agent.configuration import command as validate_config

from robert_agent.work import scan_batches as scan_batches


def manage_batches(config_path, *, retry=None, dry_run=False):
    config = validate_config.validate_config(config_path, skip_external=True)
    if not config["ok"]:
        return config
    storage.init_database(config["db_path"])
    with closing(sqlite3.connect(config["db_path"])) as conn:
        repo_ids = [
            row[0]
            for repo in config["repos"]
            for row in conn.execute("SELECT repo_id FROM repos WHERE full_name=?", (repo["full_name"],))
        ]
        if retry and not dry_run:
            return scan_batches.retry_batch(config["db_path"], retry, repo_ids)
        batches = []
        for repo_id in repo_ids:
            for row in conn.execute(
                """SELECT batch_id,created_at,retry_at,retry_count,attention_reason
                    FROM github_scan_batches WHERE repo_id=? AND completed_at IS NULL
                    ORDER BY created_at,batch_id""",
                (repo_id,),
            ):
                batches.append(
                    dict(
                        zip(
                            (
                                "batch_id",
                                "created_at",
                                "retry_at",
                                "retry_count",
                                "attention_reason",
                            ),
                            row,
                        ),
                        repo_id=repo_id,
                    )
                )
    return {"ok": True, "status": "dry_run" if retry else "listed", "batches": batches}
