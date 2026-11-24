"""Bounded lifecycle and current-execution records for a board request."""

from collections import defaultdict
import json

from sqlalchemy import and_, case, func, or_, select

from robert_agent.foundation.contracts.work import PUBLICATION_RETRY_WAKEUP_KIND
from robert_agent.persistence import statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.expressions import valid_json_or_empty
from robert_agent.persistence.queries import attempts, github_actions
from robert_agent.persistence.queries.readviews import task_history
from robert_agent.persistence.queries import sqlite_master
from robert_agent.persistence.rounds import pool, pre_pr_review, review, vote


def _batches(ids):
    keys = tuple(dict.fromkeys(key for key in ids if key is not None))
    return (keys[start : start + 200] for start in range(0, len(keys), 200))


def lifecycle_records(conn, items, *, controls=None):
    """Load relevant records, excluding completed attempts and published actions.

    Historical tasks contribute only execution attention and existing control
    bindings. Current task selection stays in SQL so histories are not hydrated.
    """
    item, stream, task, source = (tables[name] for name in ("work_items", "workstreams", "tasks", "github_sources"))
    related = or_(stream.c.workstream_id == item.c.workstream_id, stream.c.origin_workstream_id == item.c.workstream_id)
    result = {
        row["work_item_id"]: {
            "streams": [],
            "attempts": [],
            "actions": [],
            "task_metadata": [],
            "controls": {},
            "pull_requests": [],
            "source": None,
            "publication_retry_pending": False,
            "approval_task_id": None,
            "task": None,
        }
        for row in items
    }
    owners = defaultdict(list)
    for ids in _batches(result):
        scope = item.c.work_item_id.in_(ids)
        rows = statements.read(
            conn,
            select(item.c.work_item_id, stream.c.workstream_id).select_from(item.join(stream, related)).where(scope),
        )
        for row in rows:
            result[row["work_item_id"]]["streams"].append(row["workstream_id"])
            owners[row["workstream_id"]].append(row["work_item_id"])

        for row in statements.read(
            conn,
            select(item.c.work_item_id, source)
            .select_from(item.join(source, source.c.source_id == item.c.origin_source_id))
            .where(scope),
        ):
            result[row["work_item_id"]]["source"] = row

        attention = func.json_extract(valid_json_or_empty(task.c.metadata_json), "$.execution_attention")
        empty_metadata = or_(task.c.metadata_json.is_(None), task.c.metadata_json == "")
        object_metadata = and_(
            func.json_valid(task.c.metadata_json), func.json_type(valid_json_or_empty(task.c.metadata_json)) == "object"
        )
        metadata = case(
            (empty_metadata, "{}"),
            (object_metadata, func.json_object("execution_attention", attention)),
            else_='{"execution_attention":{"reason_code":"invalid_task_metadata"}}',
        ).label("metadata_json")
        for row in statements.read(
            conn,
            select(item.c.work_item_id, metadata)
            .select_from(item.join(stream, related).join(task, task.c.workstream_id == stream.c.workstream_id))
            .where(scope, ~empty_metadata, or_(~object_metadata, attention.is_not(None))),
        ):
            result[row["work_item_id"]]["task_metadata"].append(row["metadata_json"])

        control = tables["execution_subjects"]
        episode = func.json_extract(valid_json_or_empty(task.c.metadata_json), "$.remediation_episode_id")
        binding = or_(
            and_(control.c.subject_type == "task", control.c.subject_id == task.c.task_id),
            and_(control.c.subject_type == "remediation_episode", control.c.subject_id == episode),
        )
        if controls is None:
            for row in statements.read(
                conn,
                select(item.c.work_item_id, control)
                .select_from(
                    item.join(stream, related)
                    .join(task, task.c.workstream_id == stream.c.workstream_id)
                    .join(control, binding)
                )
                .where(scope),
            ):
                result[row["work_item_id"]]["controls"][(row["subject_type"], row["subject_id"])] = dict(row)
        else:
            # Supplied controls are authoritative, including observations that
            # have no persisted binding. Only matching task subjects are read.
            # Global control snapshots can contain thousands of unrelated
            # subjects. Bound both SQLite expression depth and parameter count.
            for subject_batch in _batches(controls):
                task_ids = [key for kind, key in subject_batch if kind == "task"]
                episode_ids = [key for kind, key in subject_batch if kind == "remediation_episode"]
                if not task_ids and not episode_ids:
                    continue
                for row in statements.read(
                    conn,
                    select(item.c.work_item_id, task.c.task_id, episode.label("episode_id"))
                    .select_from(item.join(stream, related).join(task, task.c.workstream_id == stream.c.workstream_id))
                    .where(scope, or_(task.c.task_id.in_(task_ids), episode.in_(episode_ids))),
                ):
                    subjects = [("task", row["task_id"])]
                    if row["episode_id"]:
                        subjects.append(("remediation_episode", row["episode_id"]))
                    for key in subjects:
                        if key in controls:
                            result[row["work_item_id"]]["controls"][key] = controls[key]

        for row in statements.read(
            conn,
            select(item.c.work_item_id, task.c.task_id)
            .select_from(
                item.join(stream, stream.c.workstream_id == item.c.workstream_id).join(
                    task,
                    and_(task.c.task_id == stream.c.active_task_id, task.c.workstream_id == stream.c.workstream_id),
                )
            )
            .where(
                scope,
                stream.c.lifecycle == "active",
                task.c.lifecycle.in_(("detected", "authorized", "classified", "queued", "running")),
            ),
        ):
            result[row["work_item_id"]]["approval_task_id"] = row["task_id"]

        for row in statements.read(
            conn,
            select(
                item.c.work_item_id,
                source.c.number,
                source.c.html_url,
                source.c.state,
                stream.c.workstream_id,
                source.c.metadata_json,
            )
            .select_from(item.join(stream, related).join(source, source.c.source_id == stream.c.primary_source_id))
            .where(scope, source.c.source_type == "pull_request")
            .order_by(case((source.c.state == "open", 0), else_=1), stream.c.updated_at.desc(), source.c.number.desc()),
        ):
            result[row["work_item_id"]]["pull_requests"].append(
                {key: value for key, value in dict(row).items() if key != "work_item_id"}
            )

        wakeup = tables["wakeups"]
        for row in statements.read(
            conn,
            select(wakeup.c.work_item_id)
            .where(
                wakeup.c.work_item_id.in_(ids),
                wakeup.c.status == "pending",
                func.json_extract(wakeup.c.metadata_json, "$.kind") == PUBLICATION_RETRY_WAKEUP_KIND,
            )
            .distinct(),
        ):
            result[row["work_item_id"]]["publication_retry_pending"] = True

        running = (
            select(1)
            .where(
                tables["attempts"].c.task_id == task.c.task_id,
                tables["attempts"].c.status.in_(("running", "stale")),
            )
            .correlate(task)
            .exists()
        )
        current = (
            select(task.c.task_id)
            .select_from(task.join(stream, task.c.workstream_id == stream.c.workstream_id))
            .where(related)
            .order_by(
                running.desc(),
                (task.c.task_id == stream.c.active_task_id).desc(),
                task.c.created_at.desc(),
                task.c.task_id.desc(),
            )
            .limit(1)
            .correlate(item)
            .scalar_subquery()
        )
        for row in statements.read(
            conn,
            select(item.c.work_item_id, task).select_from(item.join(task, task.c.task_id == current)).where(scope),
        ):
            result[row["work_item_id"]]["task"] = row

    for ids in _batches(owners):
        for capability, key in (
            (attempts.lifecycle_facts_filtered, "attempts"),
            (github_actions.lifecycle_facts_filtered, "actions"),
        ):
            for row in capability(conn, ids, include_workstream=True):
                record = {key: value for key, value in dict(row).items() if key != "workstream_id"}
                for owner in owners[row["workstream_id"]]:
                    result[owner][key].append(record)
    # Each batch preserves action ordering; a card spanning batches needs the
    # same chronology as the single-item query.
    for records in result.values():
        records["actions"].sort(key=lambda row: (row["created_at"], row["action_id"]))
    return result


def current_execution_records(conn, task_ids):
    attempt = tables["attempts"]
    latest = attempt.alias("latest")
    current_id = (
        select(latest.c.attempt_id)
        .where(latest.c.task_id == attempt.c.task_id)
        .order_by(latest.c.attempt_no.desc())
        .limit(1)
        .correlate(attempt)
        .scalar_subquery()
    )
    rows, counts, retriggers = {}, {}, set()
    for ids in _batches(task_ids):
        for row in statements.read(
            conn, select(attempt).where(attempt.c.task_id.in_(ids), attempt.c.attempt_id == current_id)
        ):
            rows[row["task_id"]] = row
        for row in statements.read(
            conn,
            select(
                attempt.c.task_id,
                func.count().label("total"),
                func.sum(case((attempt.c.status == "failed", 1), else_=0)).label("failed"),
            )
            .where(attempt.c.task_id.in_(ids))
            .group_by(attempt.c.task_id),
        ):
            counts[row["task_id"]] = {"total": row["total"], "failed": row["failed"]}
        wakeup = tables["wakeups"]
        retriggers.update(
            row[0]
            for row in statements.read(
                conn,
                select(wakeup.c.task_id)
                .where(
                    wakeup.c.task_id.in_(ids),
                    wakeup.c.status == "pending",
                    func.json_extract(wakeup.c.metadata_json, "$.kind") == "github_retrigger",
                )
                .distinct(),
            )
        )
    phases = {
        row["attempt_id"]: row
        for row in task_history.phases_for_attempts(
            conn, [row["attempt_id"] for row in rows.values()], latest_only=True
        )
    }
    return {"attempts": rows, "counts": counts, "retriggers": retriggers, "phases": phases}


def model_round_statuses(conn, task_ids):
    """Share session/run loads and pool observation across the current tasks."""
    ids = tuple(dict.fromkeys(task_ids))
    result = {task_id: {"review": None, "vote": None, "pre_pr_review": None} for task_id in ids}
    process_pool = None
    observed_pool = False
    for name, store in (("review", review), ("vote", vote), ("pre_pr_review", pre_pr_review)):
        session, run = tables[f"{name}_sessions"], tables[f"{name}_runs"]
        if not ids or not sqlite_master.table_exists(conn, session.name):
            continue
        entries = [
            row
            for batch in _batches(ids)
            for row in statements.read(conn, select(session).where(session.c.task_id.in_(batch)))
        ]
        if not entries:
            continue
        if not observed_pool:
            process_pool = pool.status(conn)
            observed_pool = True
        rounds = [entry["round_id"] for entry in entries]
        order = (run.c.attempt_no, run.c.run_id) if name == "pre_pr_review" else (run.c.created_at, run.c.run_id)
        runs = defaultdict(list)
        for batch in _batches(rounds):
            for row in statements.read(conn, select(run).where(run.c.round_id.in_(batch)).order_by(*order)):
                runs[row["round_id"]].append(review.decode(row) if name == "pre_pr_review" else store.decode(row))
        manifests = {}
        if name == "pre_pr_review":
            snapshot = tables["pre_pr_review_snapshots"]
            for batch in _batches(rounds):
                for row in statements.read(
                    conn,
                    select(snapshot.c.snapshot_id, snapshot.c.round_id, snapshot.c.manifest_json)
                    .select_from(
                        snapshot.join(
                            session,
                            and_(
                                snapshot.c.snapshot_id == session.c.snapshot_id,
                                snapshot.c.round_id == session.c.round_id,
                            ),
                        )
                    )
                    .where(session.c.round_id.in_(batch)),
                ):
                    manifests[row["round_id"]] = json.loads(row["manifest_json"])
        for row in entries:
            entry = review.decode(row) if name == "pre_pr_review" else store.decode(row)
            if name == "pre_pr_review":
                entry["proof"] = json.loads(entry.pop("proof_json") or "{}")
                entry["manifest"] = manifests.get(entry["round_id"], {})
                status = pre_pr_review.task_status_from_records(entry, runs[entry["round_id"]], process_pool)
            else:
                status = store.board_status_from_records(entry, runs[entry["round_id"]], process_pool)
            result[row["task_id"]][name] = status
    return result


def latest_remediation_records(conn, pull_requests):
    """Latest episode per kind matching each PR's currently observed snapshot."""
    episode = tables["pr_remediation_episodes"]
    conditions = {}
    for pr in pull_requests:
        try:
            metadata = json.loads(pr["metadata_json"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        health = metadata.get("pr_remediation_health") if isinstance(metadata, dict) else None
        health = health if isinstance(health, dict) else {}
        condition = episode.c.workstream_id == pr["workstream_id"]
        if health.get("head_sha") and health.get("base_sha"):
            condition = and_(
                condition,
                episode.c.observed_base_sha == health["base_sha"],
                or_(episode.c.observed_head_sha == health["head_sha"], episode.c.result_head_sha == health["head_sha"]),
            )
        conditions[pr["workstream_id"]] = condition
    rows = []
    for ids in _batches(conditions):
        latest = (
            select(
                episode,
                func.row_number()
                .over(
                    partition_by=(episode.c.workstream_id, episode.c.episode_kind),
                    order_by=(episode.c.updated_at.desc(), episode.c.episode_id.desc()),
                )
                .label("position"),
            )
            .where(or_(*(conditions[key] for key in ids)))
            .subquery()
        )
        rows.extend(
            statements.read(
                conn,
                select(latest)
                .where(latest.c.position == 1)
                .order_by(latest.c.updated_at.desc(), latest.c.episode_id.desc()),
            )
        )
    return rows
