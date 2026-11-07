"""Durable CI and merge-conflict remediation episode state."""

from robert_agent.foundation.contracts.delivery import PublishStatus
from robert_agent.foundation.contracts.work import (
    ACTIVE_REMEDIATION_STATUSES,
    REMEDIATION_TASK_KINDS,
    RemediationStatus,
    TERMINAL_REMEDIATION_STATUSES,
    TaskKind,
)

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import attempts as db_read_attempts
from robert_agent.persistence.queries import notifications as db_read_notifications
from robert_agent.persistence.queries import pr_remediation_episodes as db_read_pr_remediation_episodes
from robert_agent.persistence.queries import projections as db_read_projections
from robert_agent.persistence.queries import tasks as db_read_tasks
from robert_agent.persistence.queries import workstreams as db_read_workstreams
from robert_agent.persistence.stores import audit_events as db_write_audit_events
from robert_agent.persistence.stores import notifications as db_write_notifications
from robert_agent.persistence.stores import pr_ci_observations as db_write_pr_ci_observations
from robert_agent.persistence.stores import pr_remediation_episodes as db_write_pr_remediation_episodes

from datetime import datetime, timezone
import hashlib
import json
import subprocess
import uuid

from robert_agent.readmodels import usage


def _json_object(value):
    if isinstance(value, dict):
        return dict(value)
    try:
        decoded = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _episode_id(workstream_id, episode_kind, subject_key):
    digest = hashlib.sha256(f"{workstream_id}:{episode_kind}:{subject_key}".encode("utf-8")).hexdigest()[:24]
    return f"remediation-{digest}"


def _observation_id(source_kind, external_id, attempt_no):
    digest = hashlib.sha256(f"{source_kind}:{external_id}:{attempt_no}".encode("utf-8")).hexdigest()[:24]
    return f"ci-observation-{digest}"


def _episode_row(row):
    if not row:
        return None
    return {
        "episode_id": row[0],
        "workstream_id": row[1],
        "episode_kind": row[2],
        "subject_key": row[3],
        "observed_head_sha": row[4],
        "observed_base_sha": row[5],
        "result_head_sha": row[6],
        "status": row[7],
        "attempt_count": row[8],
        "failure_signature": row[9],
        "first_seen_at": row[10],
        "updated_at": row[11],
        "terminal_at": row[12],
        "last_task_id": row[13],
        "metadata": _json_object(row[14]),
    }


def _load_episode(conn, episode_id):
    row = db_records.first(db_read_pr_remediation_episodes.load_episode(conn, episode_id))
    return _episode_row(row)


def load_episode(conn, episode_id):
    return _load_episode(conn, episode_id)


def attest_remediation_worktree(
    *,
    task_kind,
    action_scope,
    remediation_evidence,
    run_command=subprocess.run,
):
    if task_kind not in REMEDIATION_TASK_KINDS:
        return {"status": "not_applicable"}
    if not isinstance(action_scope, dict) or not isinstance(
        remediation_evidence,
        dict,
    ):
        return {"status": "failed", "reason": "missing_attestation_context"}
    worktree_path = action_scope.get("worktree_path")
    branch_name = action_scope.get("branch_name")
    head_sha = remediation_evidence.get("observed_head_sha")
    base_sha = remediation_evidence.get("observed_base_sha")
    if not all(isinstance(value, str) and value for value in (worktree_path, branch_name, head_sha, base_sha)):
        return {"status": "failed", "reason": "missing_attestation_context"}

    def run_git(*args):
        try:
            return run_command(
                ["git", *args],
                cwd=worktree_path,
                capture_output=True,
                text=True,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return type(
                "FailedCommand",
                (),
                {"returncode": 1, "stdout": "", "stderr": str(exc)},
            )()

    branch = run_git("branch", "--show-current")
    if branch.returncode != 0 or branch.stdout.strip() != branch_name:
        return {"status": "failed", "reason": "branch_mismatch"}
    current_head = run_git("rev-parse", "HEAD")
    if current_head.returncode != 0 or not current_head.stdout.strip():
        return {"status": "failed", "reason": "head_unavailable"}
    if current_head.stdout.strip() == head_sha:
        return {"status": "failed", "reason": "unchanged_head"}
    status = run_git("status", "--porcelain")
    if status.returncode != 0:
        return {"status": "failed", "reason": "worktree_status_unavailable"}
    if status.stdout.strip():
        return {"status": "failed", "reason": "dirty_worktree"}
    head_ancestor = run_git(
        "merge-base",
        "--is-ancestor",
        head_sha,
        "HEAD",
    )
    if head_ancestor.returncode != 0:
        return {"status": "failed", "reason": "head_not_ancestor"}
    if task_kind == TaskKind.MERGE_CONFLICT_REMEDIATION:
        base_ancestor = run_git(
            "merge-base",
            "--is-ancestor",
            base_sha,
            "HEAD",
        )
        if base_ancestor.returncode != 0:
            return {"status": "failed", "reason": "base_not_ancestor"}
    unmerged = run_git("diff", "--name-only", "--diff-filter=U")
    if unmerged.returncode != 0 or unmerged.stdout.strip():
        return {"status": "failed", "reason": "unresolved_conflicts"}
    return {
        "status": "accepted",
        "branch_name": branch_name,
        "head_sha": head_sha,
        "base_sha": base_sha,
        "result_head_sha": current_head.stdout.strip(),
    }


def _audit_transition(conn, episode, old_status, new_status, reason, now):
    workstream = db_records.first(db_read_workstreams.find_repo_id_by_workstream_id(conn, episode["workstream_id"]))
    db_write_audit_events.insert_for_audit_transition(
        conn,
        f"audit-{uuid.uuid4().hex}",
        workstream[0] if workstream else None,
        episode["workstream_id"],
        episode.get("last_task_id"),
        now,
        json.dumps(
            {
                "episode_id": episode["episode_id"],
                "episode_kind": episode["episode_kind"],
                "observed_head_sha": episode["observed_head_sha"],
                "observed_base_sha": episode["observed_base_sha"],
                "old_status": old_status,
                "new_status": new_status,
                "reason": reason,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
    )


def _remediation_notification_type(reason):
    if reason == "failure_evidence_unavailable":
        return "pr_remediation_needs_evidence"
    if reason in {
        "manual_conclusion",
        "unsupported_conclusion",
        "manual_ci_signal",
    }:
        return "pr_remediation_manual_conclusion"
    if reason in {
        "blocked_dirty_worktree",
        "blocked_unmanaged_worktree",
    }:
        return "pr_remediation_dirty_worktree"
    if reason in {
        "stale_snapshot",
        "remediation_worktree_unavailable",
    }:
        return "pr_remediation_snapshot_unavailable"
    return "pr_remediation_exhausted"


def _notify_exhausted_episode(conn, episode, reason, now):
    notification_type = _remediation_notification_type(reason)
    existing = db_records.first(
        db_read_notifications.find_for_notify_exhausted_episode(conn, notification_type, episode["episode_id"])
    )
    if existing:
        return
    db_write_notifications.insert_notification(
        conn,
        f"notification-{uuid.uuid4().hex[:12]}",
        episode.get("last_task_id"),
        notification_type,
        now,
        json.dumps(
            {
                "episode_id": episode["episode_id"],
                "workstream_id": episode["workstream_id"],
                "episode_kind": episode["episode_kind"],
                "observed_head_sha": episode["observed_head_sha"],
                "observed_base_sha": episode["observed_base_sha"],
                "pr_number": episode["metadata"].get("pr_number"),
                "attempt_count": episode["attempt_count"],
                "budget": episode["metadata"].get("budget"),
                "details_urls": sorted(
                    {
                        failure.get("details_url")
                        for failure in episode["metadata"].get("failures", [])
                        if isinstance(failure, dict) and failure.get("details_url")
                    }
                ),
                "reason": reason,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
    )


def _transition(conn, episode, new_status, reason, now):
    old_status = episode["status"]
    if old_status == new_status:
        return None
    terminal_at = now if new_status in TERMINAL_REMEDIATION_STATUSES else None
    metadata = dict(episode["metadata"])
    metadata["last_transition_reason"] = reason
    db_write_pr_remediation_episodes.update_for_transition(
        conn,
        new_status,
        now,
        terminal_at,
        json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        episode["episode_id"],
        old_status,
    )
    if db_records.first(db_read_projections.find_for_transition(conn))[0] != 1:
        return None
    _audit_transition(conn, episode, old_status, new_status, reason, now)
    if new_status == RemediationStatus.EXHAUSTED.value:
        _notify_exhausted_episode(conn, episode, reason, now)
    return {
        "episode_id": episode["episode_id"],
        "old_status": old_status,
        "new_status": new_status,
        "reason": reason,
    }


def _episodes_for_workstream(conn, workstream_id):
    rows = db_read_pr_remediation_episodes.find_for_episodes_for_workstream(conn, workstream_id)
    return [_episode_row(row) for row in rows]


def _create_episode(
    conn,
    *,
    workstream,
    episode_kind,
    snapshot,
    failure_signature_value,
    metadata,
    now,
):
    subject_key = f"{snapshot['head_sha']}:{snapshot['base_sha']}"
    episode_id = _episode_id(
        workstream["workstream_id"],
        episode_kind,
        subject_key,
    )
    db_write_pr_remediation_episodes.insert_for_create_episode(
        conn,
        episode_id,
        workstream["workstream_id"],
        episode_kind,
        subject_key,
        snapshot["head_sha"],
        snapshot["base_sha"],
        failure_signature_value,
        now,
        now,
        json.dumps(metadata, ensure_ascii=False, sort_keys=True),
    )
    return _load_episode(conn, episode_id)


def _store_ci_observations(conn, episode_id, observations):
    for observation in observations:
        source_kind = observation.get("source_kind")
        external_id = str(observation.get("external_id") or "")
        attempt_no = int(observation.get("attempt_no") or 1)
        if source_kind not in {"workflow_run", "check_run"} or not external_id:
            continue
        metadata = {
            key: value
            for key, value in observation.items()
            if key
            not in {
                "source_kind",
                "external_id",
                "attempt_no",
                "check_name",
                "status",
                "conclusion",
                "details_url",
                "completed_at",
                "failure_signature",
            }
        }
        db_write_pr_ci_observations.insert_for_store_ci_observations(
            conn,
            _observation_id(source_kind, external_id, attempt_no),
            episode_id,
            source_kind,
            external_id,
            attempt_no,
            observation.get("check_name") or "",
            observation.get("status") or "",
            observation.get("conclusion"),
            observation.get("details_url"),
            observation.get("completed_at"),
            observation.get("failure_signature"),
            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        )


def upsert_health_episodes(
    conn,
    *,
    repo,
    workstream,
    snapshot,
    ci_summary,
    now,
):
    transitions = []
    episodes = _episodes_for_workstream(conn, workstream["workstream_id"])
    if snapshot.get("state") == "closed" or snapshot.get("merged"):
        for episode in episodes:
            if episode["status"] in ACTIVE_REMEDIATION_STATUSES:
                transition = _transition(
                    conn,
                    episode,
                    RemediationStatus.CANCELED.value,
                    "remote_pr_terminal",
                    now,
                )
                if transition:
                    transitions.append(transition)
        return transitions

    keep_waiting_ids = set()
    suppress_current_ci_episode = False
    suppress_current_conflict_episode = False
    for episode in episodes:
        # A failed repair remains exhausted across polls of its result snapshot.
        # A new externally supplied head/base can still start a new episode.
        if (
            episode["status"] == RemediationStatus.EXHAUSTED
            and episode["result_head_sha"] == snapshot.get("head_sha")
            and episode["observed_base_sha"] == snapshot.get("base_sha")
        ):
            reason = episode["metadata"].get("last_transition_reason")
            if reason == "merge_conflict_persisted":
                suppress_current_conflict_episode = True
            elif (
                reason == "repeated_failure_signature"
                and ci_summary
                and ci_summary.get("failure_signature") == episode["failure_signature"]
            ):
                suppress_current_ci_episode = True
        if episode["status"] != RemediationStatus.WAITING_FOR_SIGNAL or episode["result_head_sha"] != snapshot.get(
            "head_sha"
        ):
            continue
        if episode["episode_kind"] == "merge_conflict" and snapshot.get("mergeable") is True:
            transition = _transition(
                conn,
                episode,
                RemediationStatus.RESOLVED.value,
                "merge_conflict_cleared",
                now,
            )
        elif episode["episode_kind"] == "merge_conflict" and snapshot.get("mergeable") is False:
            suppress_current_conflict_episode = True
            transition = _transition(
                conn,
                episode,
                RemediationStatus.EXHAUSTED.value,
                "merge_conflict_persisted",
                now,
            )
        elif episode["episode_kind"] == "ci" and ci_summary:
            if ci_summary.get("status") == "green":
                transition = _transition(
                    conn,
                    episode,
                    RemediationStatus.RESOLVED.value,
                    "configured_checks_green",
                    now,
                )
            elif (
                ci_summary.get("status") == "failing"
                and ci_summary.get("failure_signature") == episode["failure_signature"]
            ):
                suppress_current_ci_episode = True
                transition = _transition(
                    conn,
                    episode,
                    RemediationStatus.EXHAUSTED.value,
                    "repeated_failure_signature",
                    now,
                )
            elif ci_summary.get("status") == "waiting":
                keep_waiting_ids.add(episode["episode_id"])
                transition = None
            else:
                transition = _transition(
                    conn,
                    episode,
                    RemediationStatus.SUPERSEDED.value,
                    "result_head_requires_new_episode",
                    now,
                )
        else:
            keep_waiting_ids.add(episode["episode_id"])
            transition = None
        if transition:
            transitions.append(transition)

    subject_key = f"{snapshot.get('head_sha', '')}:{snapshot.get('base_sha', '')}"
    for episode in _episodes_for_workstream(conn, workstream["workstream_id"]):
        if (
            episode["status"] in ACTIVE_REMEDIATION_STATUSES
            and episode["subject_key"] != subject_key
            and episode["episode_id"] not in keep_waiting_ids
        ):
            transition = _transition(
                conn,
                episode,
                RemediationStatus.SUPERSEDED.value,
                "pr_snapshot_changed",
                now,
            )
            if transition:
                transitions.append(transition)

    metadata = {
        "pr_number": workstream.get("number"),
        "head_sha": snapshot.get("head_sha"),
        "base_sha": snapshot.get("base_sha"),
    }
    if (
        repo["pr_automation"]["conflict"]["enabled"]
        and snapshot.get("mergeable") is False
        and not suppress_current_conflict_episode
    ):
        _create_episode(
            conn,
            workstream=workstream,
            episode_kind="merge_conflict",
            snapshot=snapshot,
            failure_signature_value=None,
            metadata=metadata,
            now=now,
        )

    if repo["pr_automation"]["ci"]["enabled"] and ci_summary:
        current_ci = next(
            (
                episode
                for episode in _episodes_for_workstream(
                    conn,
                    workstream["workstream_id"],
                )
                if episode["episode_kind"] == "ci" and episode["subject_key"] == subject_key
            ),
            None,
        )
        if ci_summary.get("status") == "failing" and not suppress_current_ci_episode:
            episode = _create_episode(
                conn,
                workstream=workstream,
                episode_kind="ci",
                snapshot=snapshot,
                failure_signature_value=ci_summary.get("failure_signature"),
                metadata={
                    **metadata,
                    "ci_status": "failing",
                    "failures": ci_summary.get("failures", []),
                },
                now=now,
            )
            _store_ci_observations(
                conn,
                episode["episode_id"],
                ci_summary.get("observations") or ci_summary.get("failures") or [],
            )
        elif ci_summary.get("status") == "manual":
            episode = _create_episode(
                conn,
                workstream=workstream,
                episode_kind="ci",
                snapshot=snapshot,
                failure_signature_value=None,
                metadata={
                    **metadata,
                    "ci_status": "manual",
                    "manual_reason": ci_summary.get("reason"),
                    "manual_conclusions": ci_summary.get(
                        "manual_conclusions",
                        [],
                    ),
                    "failures": ci_summary.get("failures", []),
                },
                now=now,
            )
            _store_ci_observations(
                conn,
                episode["episode_id"],
                ci_summary.get("observations") or ci_summary.get("failures") or [],
            )
            if episode["status"] == RemediationStatus.OPEN:
                transition = _transition(
                    conn,
                    episode,
                    RemediationStatus.EXHAUSTED.value,
                    ci_summary.get("reason") or "manual_ci_signal",
                    now,
                )
                if transition:
                    transitions.append(transition)
        elif ci_summary.get("status") == "green" and current_ci:
            if current_ci["status"] in ACTIVE_REMEDIATION_STATUSES:
                transition = _transition(
                    conn,
                    current_ci,
                    RemediationStatus.RESOLVED.value,
                    "configured_checks_green",
                    now,
                )
                if transition:
                    transitions.append(transition)
    return transitions


def _parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _episode_usage(conn, episode_id):
    payloads = []
    for row in db_read_attempts.find_for_episode_usage(conn, episode_id):
        metadata = _json_object(row[0])
        payloads.append(usage.extract_attempt_usage(metadata))
    summary = usage.summarize_usage_payloads(payloads)
    summary["attempt_usage_count"] = len(payloads)
    summary["measured_attempt_count"] = sum(1 for payload in payloads if payload.get("usage_available"))
    return summary


def budget_status(conn, *, episode, policy, now):
    usage_summary = _episode_usage(conn, episode["episode_id"])
    result = {
        "ok": True,
        "reason": "",
        **usage_summary,
    }
    if episode["attempt_count"] >= policy["max_attempts"]:
        return {**result, "ok": False, "reason": "max_attempts"}
    first_seen = _parse_time(episode.get("first_seen_at"))
    current = _parse_time(now)
    if first_seen and current:
        elapsed_minutes = (current.astimezone(timezone.utc) - first_seen.astimezone(timezone.utc)).total_seconds() / 60
        result["elapsed_minutes"] = elapsed_minutes
        if elapsed_minutes >= policy["max_wall_minutes"]:
            return {
                **result,
                "ok": False,
                "reason": "max_wall_minutes",
            }
    max_tokens = policy.get("max_total_tokens")
    max_cost = policy.get("max_cost_usd")
    if (
        episode["attempt_count"] > 0
        and (max_tokens or max_cost)
        and result["measured_attempt_count"] < episode["attempt_count"]
    ):
        return {
            **result,
            "ok": False,
            "reason": "usage_unavailable",
        }
    if max_tokens and result["total_tokens"] >= max_tokens:
        return {
            **result,
            "ok": False,
            "reason": "max_total_tokens",
        }
    if max_cost and result["total_cost_usd"] >= max_cost:
        return {
            **result,
            "ok": False,
            "reason": "max_cost_usd",
        }
    return result


def _policy_for_episode(repo, episode_kind):
    key = "conflict" if episode_kind == "merge_conflict" else "ci"
    return repo["pr_automation"][key]


def next_system_decisions(conn, *, repo, now):
    rows = db_read_pr_remediation_episodes.find_for_next_system_decisions(conn, repo["repo_id"])
    decisions = []
    for (episode_id,) in rows:
        episode = _load_episode(conn, episode_id)
        if not _policy_for_episode(
            repo,
            episode["episode_kind"],
        )["enabled"]:
            _transition(
                conn,
                episode,
                RemediationStatus.CANCELED.value,
                "automation_disabled",
                now,
            )
            continue
        budget = budget_status(
            conn,
            episode=episode,
            policy=_policy_for_episode(repo, episode["episode_kind"]),
            now=now,
        )
        if not budget["ok"]:
            episode = {
                **episode,
                "metadata": {
                    **episode["metadata"],
                    "budget": budget,
                },
            }
            _transition(
                conn,
                episode,
                RemediationStatus.EXHAUSTED.value,
                budget["reason"],
                now,
            )
            continue
        decisions.append({**episode, "budget": budget})
    return decisions


def cancel_disabled_episodes(conn, *, repo, now):
    rows = db_read_pr_remediation_episodes.find_for_cancel_disabled_episodes(conn, repo["repo_id"])
    canceled = 0
    for (episode_id,) in rows:
        episode = _load_episode(conn, episode_id)
        if _policy_for_episode(
            repo,
            episode["episode_kind"],
        )["enabled"]:
            continue
        if _transition(
            conn,
            episode,
            RemediationStatus.CANCELED.value,
            "automation_disabled",
            now,
        ):
            canceled += 1
    return canceled


def revalidate_decision(conn, *, decision, snapshot, repo):
    episode = _load_episode(conn, decision["episode_id"])
    if not episode or episode["status"] != RemediationStatus.OPEN:
        return {"status": "ineligible", "reason": "episode_not_open"}
    if snapshot.get("state") == "closed" or snapshot.get("merged"):
        return {"status": RemediationStatus.CANCELED.value, "reason": "remote_pr_terminal"}
    if (
        snapshot.get("head_sha") != episode["observed_head_sha"]
        or snapshot.get("base_sha") != episode["observed_base_sha"]
    ):
        return {"status": RemediationStatus.SUPERSEDED.value, "reason": "pr_snapshot_changed"}
    policy = _policy_for_episode(repo, episode["episode_kind"])
    if not policy["enabled"]:
        return {"status": RemediationStatus.CANCELED.value, "reason": "automation_disabled"}
    return {"status": "eligible", "episode": episode}


def exhaust_episode(conn, *, episode_id, reason, now):
    episode = _load_episode(conn, episode_id)
    if not episode or episode["status"] not in ACTIVE_REMEDIATION_STATUSES:
        return False
    return bool(
        _transition(
            conn,
            episode,
            RemediationStatus.EXHAUSTED.value,
            reason,
            now,
        )
    )


def supersede_episode(conn, *, episode_id, reason, now):
    episode = _load_episode(conn, episode_id)
    if not episode or episode["status"] not in ACTIVE_REMEDIATION_STATUSES:
        return False
    return bool(
        _transition(
            conn,
            episode,
            RemediationStatus.SUPERSEDED.value,
            reason,
            now,
        )
    )


def record_task_failure(conn, *, task_id, reason, now):
    row = db_records.first(db_read_tasks.find_task_kind_and_metadata_json_by_task_id(conn, task_id))
    if not row or row[0] not in REMEDIATION_TASK_KINDS:
        return False
    episode_id = _json_object(row[1]).get("remediation_episode_id")
    episode = _load_episode(conn, episode_id)
    if not episode or episode["status"] not in {
        RemediationStatus.QUEUED,
        RemediationStatus.REMEDIATING,
    }:
        return False
    return bool(_transition(conn, episode, RemediationStatus.OPEN.value, reason, now))


def cancel_workstream_episodes(
    conn,
    *,
    workstream_id,
    reason,
    now,
):
    canceled = 0
    for episode in _episodes_for_workstream(conn, workstream_id):
        if episode["status"] not in ACTIVE_REMEDIATION_STATUSES:
            continue
        if _transition(conn, episode, RemediationStatus.CANCELED.value, reason, now):
            canceled += 1
    return canceled


def record_task_started(conn, *, episode_id, task_id, now):
    episode = _load_episode(conn, episode_id)
    if not episode or episode["status"] != RemediationStatus.OPEN:
        return False
    db_write_pr_remediation_episodes.record_task_started(conn, task_id, now, episode_id)
    if db_records.first(db_read_projections.find_for_transition(conn))[0] != 1:
        return False
    _audit_transition(
        conn,
        {**episode, "last_task_id": task_id},
        RemediationStatus.OPEN.value,
        RemediationStatus.REMEDIATING.value,
        "task_started",
        now,
    )
    return True


def record_publish_result(
    conn,
    *,
    episode_id,
    publish_status,
    result_head_sha=None,
    now,
):
    episode = _load_episode(conn, episode_id)
    if not episode or episode["status"] not in {
        RemediationStatus.REMEDIATING,
        RemediationStatus.QUEUED,
        RemediationStatus.OPEN,
    }:
        return False
    if publish_status == PublishStatus.PUBLISHED:
        new_status = RemediationStatus.WAITING_FOR_SIGNAL.value
        reason = "repair_published"
    elif publish_status == "superseded":
        new_status = RemediationStatus.SUPERSEDED.value
        reason = "publish_snapshot_changed"
    else:
        new_status = RemediationStatus.OPEN.value
        reason = "publish_failed"
    db_write_pr_remediation_episodes.record_publish_result(
        conn,
        new_status,
        result_head_sha,
        now,
        now if new_status in TERMINAL_REMEDIATION_STATUSES else None,
        episode_id,
        episode["status"],
    )
    if db_records.first(db_read_projections.find_for_transition(conn))[0] != 1:
        return False
    _audit_transition(
        conn,
        episode,
        episode["status"],
        new_status,
        reason,
        now,
    )
    return True
