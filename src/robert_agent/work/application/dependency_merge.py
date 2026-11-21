"""Replay an authorized reply through the existing transactional command."""

from contextlib import closing
import json
from robert_agent.persistence import database
from robert_agent.work import dependency_merge
from robert_agent.work.application import commands
from robert_agent.work.errors import WorkItemConflictError
from robert_agent.work.inputs import CommandContext


def resume_pending(db_path, repo_id):
    with closing(database.connect(db_path)) as conn:
        registrations = dependency_merge.pending(conn, repo_id)
    resumed = []
    for registration in registrations:
        metadata = json.loads(registration["metadata_json"])
        with closing(database.connect(db_path)) as conn:
            if not dependency_merge.merged(conn, repo_id, metadata["dependency"]["pr_url"]):
                continue
        try:
            result = commands.execute_command(
                db_path,
                registration["work_item_id"],
                context=CommandContext("operator", registration["actor_identity"], frozenset({repo_id}), frozenset()),
                command="reply",
                expected_version=metadata["expected_version"],
                expected_event_id=metadata["question_id"],
                idempotency_key="dependency-resume:" + registration["event_id"],
                body=registration["body"],
                dependency_resume=registration["event_id"],
            )
        except WorkItemConflictError:
            continue  # An ordinary reply, cancel, edit or changed generation revoked this request.
        if result.get("task_id") and not result.get("dependency_replayed"):
            resumed.append(result)
    return resumed
