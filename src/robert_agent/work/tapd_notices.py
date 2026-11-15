"""Record durable TAPD notices for a PR linked to a ticket."""

import json

from robert_agent.foundation import tapd_identity
from robert_agent.persistence.queries import notifications as db_read_notifications
from robert_agent.persistence.stores import notifications as db_write_notifications
from robert_agent.work import queries as work_queries
from robert_agent.work import tapd_policy


def record_pr_notices(conn, workstream_id, kind, keys, now):
    target = work_queries.tapd_origin(conn, workstream_id)
    if not target or not target["pr_number"]:
        return
    dedupe_keys = {tapd_policy.notice_id(kind, f"{workstream_id}:{key}"): f"{workstream_id}:{key}" for key in keys}
    existing = {row[0] for row in db_read_notifications.find_existing_ids(conn, dedupe_keys)}
    for notification_id, dedupe_key in dedupe_keys.items():
        if notification_id in existing:
            continue
        metadata = {
            "dedupe_key": dedupe_key,
            "kind": tapd_identity.KINDS[target["source_type"]],
            "project_key": target["project_key"],
            "external_id": target["external_id"],
            "url": target["url"],
            "pr_url": f"https://github.com/{target['repo']}/pull/{target['pr_number']}",
        }
        db_write_notifications.insert_tapd_notification(
            conn, notification_id, kind, now, json.dumps(metadata, sort_keys=True)
        )
