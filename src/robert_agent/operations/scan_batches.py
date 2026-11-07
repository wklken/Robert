"""operations scan_batches responsibilities."""

from robert_agent.persistence import database as db_database

from robert_agent.persistence.queries import github_scan_batches as db_read_github_scan_batches
from robert_agent.persistence.queries import repos as db_read_repos
from contextlib import closing
from robert_agent.persistence.schema import database as storage
from robert_agent.configuration import command as validate_config

from robert_agent.work import scan_batches as scan_batches


def manage_batches(config_path, *, retry=None, dry_run=False):
    config = validate_config.validate_config(config_path, skip_external=True)
    if not config["ok"]:
        return config
    storage.init_database(config["db_path"])
    with closing(db_database.connect(config["db_path"])) as conn:
        repo_ids = [
            row[0]
            for repo in config["repos"]
            for row in db_read_repos.find_repo_id_by_full_name(conn, repo["full_name"])
        ]
        if retry and not dry_run:
            return scan_batches.retry_batch(config["db_path"], retry, repo_ids)
        batches = []
        for repo_id in repo_ids:
            for row in db_read_github_scan_batches.find_for_manage_batches(conn, repo_id):
                batches.append(
                    dict(
                        zip(("batch_id", "created_at", "retry_at", "retry_count", "attention_reason"), row),
                        repo_id=repo_id,
                    )
                )
    return {"ok": True, "status": "dry_run" if retry else "listed", "batches": batches}
