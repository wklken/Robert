"""Translate observed pull request failures into remediation work."""

from robert_agent.persistence import records as db_records
from robert_agent.persistence import transactions
from robert_agent.persistence.queries import pr_remediation_episodes as db_read_pr_remediation_episodes
from robert_agent.persistence.queries import tasks as db_read_tasks
from robert_agent.persistence.queries import workstreams as db_read_workstreams
from robert_agent.work.application import source_events
from robert_agent.execution import ownership as execution_ownership
from robert_agent.supervision import decisions as supervision_decisions
from robert_agent.workflows import pr_health
from robert_agent.workflows import pr_remediation as remediation


def _system_event(
    *,
    repo,
    workstream_id,
    origin_workstream_id,
    source_key,
    number,
    snapshot,
    episode,
    observation=None,
    run_now,
):
    observation = observation or {}
    episode_kind = episode["episode_kind"]
    if episode_kind == "merge_conflict":
        event_type = "pr_merge_conflict_detected"
        fingerprint = f"github-system:merge-conflict:{number}:{snapshot['head_sha']}:{snapshot['base_sha']}"
        task_kind = "merge_conflict_remediation"
    else:
        source_kind = observation.get("source_kind") or "workflow_run"
        event_type = "ci_check_completed" if source_kind == "check_run" else "ci_run_completed"
        external_id = observation.get("external_id") or episode["episode_id"]
        attempt_no = observation.get("attempt_no") or 1
        fingerprint = f"github-system:{source_kind}:{external_id}:{attempt_no}"
        task_kind = "ci_remediation"
    metadata = {
        "remediation": {
            "episode_id": episode["episode_id"],
            "episode_kind": episode_kind,
            "observed_head_sha": snapshot["head_sha"],
            "observed_base_sha": snapshot["base_sha"],
            "failure_signature": episode.get("failure_signature"),
            "failure_summary": (observation.get("failure_summary") or episode["metadata"].get("failures") or ""),
            "failures": episode["metadata"].get("failures", []),
            "details_url": observation.get("details_url"),
        }
    }
    return {
        "repo": repo["full_name"],
        "source_type": "pull_request",
        "source_key": source_key,
        "number": number,
        "workstream_id": workstream_id,
        "origin_workstream_id": origin_workstream_id,
        "event_type": event_type,
        "event_fingerprint": fingerprint,
        "event_at": observation.get("completed_at") or run_now,
        "actor_kind": "github_system",
        "actor_login": "github",
        "source_author_login": snapshot.get("author_login"),
        "authorization_status": "authorized_system_trigger",
        "creates_task": True,
        "drives_execution": True,
        "task_kind": task_kind,
        "intent": "pr_followup_fix",
        "has_open_dd_pr": True,
        "existing_pr_head_branch": snapshot.get("head_ref"),
        "base_branch": snapshot.get("base_ref"),
        "head_sha": snapshot.get("head_sha"),
        "base_sha": snapshot.get("base_sha"),
        "mergeable": snapshot.get("mergeable"),
        "state": snapshot.get("state") or "open",
        "url": snapshot.get("html_url"),
        "check_name": observation.get("check_name"),
        "external_id": observation.get("external_id"),
        "attempt_no": observation.get("attempt_no"),
        "status": observation.get("status"),
        "conclusion": observation.get("conclusion"),
        "details_url": observation.get("details_url"),
        "completed_at": observation.get("completed_at"),
        "failure_summary": observation.get("failure_summary"),
        "failure_signature": episode.get("failure_signature"),
        "title": f"Automated {episode_kind} remediation",
        "body": "",
        "metadata": metadata,
    }


def collect_pr_remediation_decisions(
    conn,
    repo_id,
    repo,
    runner,
    run_now,
    config=None,
):
    config = config or {}
    automation = repo.get("pr_automation") or {}
    conflict_policy = automation.get("conflict") or {}
    ci_policy = automation.get("ci") or {}
    repo_with_id = {**repo, "repo_id": repo_id}
    remediation.cancel_disabled_episodes(
        conn,
        repo=repo_with_id,
        now=run_now,
    )
    if not conflict_policy.get("enabled") and not ci_policy.get("enabled"):
        return []

    rows = db_read_workstreams.find_for_collect_pr_remediation_decisions(conn, repo_id)
    event_by_episode = {}
    snapshot_by_episode = {}
    observed_workstreams = []
    for workstream_id, origin_workstream_id, source_key, number in rows:
        conn.commit()
        snapshot = pr_health.fetch_pr_snapshot(
            repo["full_name"],
            number,
            runner=runner,
        )
        if not snapshot:
            continue
        if snapshot.get("author_login", "").lower() != repo["github_account"].lower():
            continue
        if not snapshot.get("head_sha") or not snapshot.get("base_sha"):
            continue
        ci_summary = None
        if ci_policy.get("enabled"):
            observations = pr_health.collect_ci_observations(
                repo["full_name"],
                snapshot["head_sha"],
                check_allowlist=ci_policy["check_allowlist"],
                max_summary_chars=ci_policy["max_failure_summary_chars"],
                runner=runner,
            )
            ci_summary = pr_health.summarize_ci(
                observations,
                ci_policy["check_allowlist"],
            )
        workstream_record = {
            "workstream_id": workstream_id,
            "repo_id": repo_id,
            "number": number,
        }
        if db_records.first(db_read_tasks.find_for_collect_pr_remediation_decisions(conn, workstream_id)):
            continue  # Deliver and resolve this question before admitting more automatic repair.
        remediation.upsert_health_episodes(
            conn,
            repo=repo_with_id,
            workstream=workstream_record,
            snapshot=snapshot,
            ci_summary=ci_summary,
            now=run_now,
        )
        episodes = {
            row[1]: remediation._load_episode(conn, row[0])
            for row in db_read_pr_remediation_episodes.find_for_collect_pr_remediation_decisions_2(
                conn, workstream_id, f"{snapshot['head_sha']}:{snapshot['base_sha']}"
            )
        }
        observed_workstreams.append(workstream_id)
        conflict_episode = episodes.get("merge_conflict")
        if conflict_episode:
            event = _system_event(
                repo=repo,
                workstream_id=workstream_id,
                origin_workstream_id=origin_workstream_id,
                source_key=source_key,
                number=number,
                snapshot=snapshot,
                episode=conflict_episode,
                run_now=run_now,
            )
            source_events.insert_source_and_event(
                conn,
                repo_id,
                {**event, "creates_task": False, "drives_execution": False}
                if supervision_decisions.enabled(config)
                else event,
                run_now,
            )
            if not supervision_decisions.enabled(config) or snapshot.get("mergeable") is False:
                event_by_episode[conflict_episode["episode_id"]] = event
                snapshot_by_episode[conflict_episode["episode_id"]] = snapshot
        ci_episode = episodes.get("ci")
        if ci_episode:
            ci_observations = ci_summary.get("observations", []) if ci_summary else []
            completed = [observation for observation in ci_observations if observation.get("status") == "completed"]
            for observation in completed:
                event = _system_event(
                    repo=repo,
                    workstream_id=workstream_id,
                    origin_workstream_id=origin_workstream_id,
                    source_key=source_key,
                    number=number,
                    snapshot=snapshot,
                    episode=ci_episode,
                    observation=observation,
                    run_now=run_now,
                )
                source_events.insert_source_and_event(
                    conn,
                    repo_id,
                    {**event, "creates_task": False, "drives_execution": False}
                    if supervision_decisions.enabled(config)
                    else event,
                    run_now,
                )
                if observation.get("conclusion") == "failure":
                    event_by_episode.setdefault(ci_episode["episode_id"], event)
                    snapshot_by_episode[ci_episode["episode_id"]] = snapshot

    if supervision_decisions.enabled(config) and observed_workstreams:
        if not conn.in_transaction:
            transactions.begin(conn, immediate=True)
        for workstream_id in observed_workstreams:
            supervision_decisions.withdraw_repairs(conn, config, workstream_id, run_now)
    decisions = remediation.next_system_decisions(
        conn,
        repo=repo_with_id,
        now=run_now,
    )
    events = []
    for decision in decisions:
        event = event_by_episode.get(decision["episode_id"])
        if event and supervision_decisions.enabled(config):
            supervision_decisions.offer(
                conn,
                config,
                workstream_id=decision["workstream_id"],
                action="resolve_conflict" if decision["episode_kind"] == "merge_conflict" else "repair_ci",
                target_id=decision["episode_id"],
                facts=_decision_facts(decision),
                now=run_now,
            )
    for decision in decisions:
        episode_id = decision["episode_id"]
        event = event_by_episode.get(episode_id)
        snapshot = snapshot_by_episode.get(episode_id)
        if not event or not snapshot:
            continue
        eligibility = remediation.revalidate_decision(
            conn,
            decision=decision,
            snapshot=snapshot,
            repo=repo_with_id,
        )
        if eligibility["status"] != "eligible":
            continue
        policy_key = "conflict" if decision["episode_kind"] == "merge_conflict" else "ci"
        event["metadata"]["remediation"]["budget"] = {
            "limits": repo["pr_automation"][policy_key],
            "usage": decision["budget"],
        }
        action = "resolve_conflict" if decision["episode_kind"] == "merge_conflict" else "repair_ci"
        if not supervision_decisions.permit(
            conn,
            config,
            workstream_id=decision["workstream_id"],
            action=action,
            target_id=episode_id,
            facts=_decision_facts(decision),
            now=run_now,
        ):
            continue
        events.append(event)
    for (episode_id,) in db_read_pr_remediation_episodes.find_for_collect_pr_remediation_decisions(conn, repo_id):
        execution_ownership.close_subject(conn, "remediation_episode", episode_id, run_now)
    accepted = []
    for event in events:
        episode_id = ((event.get("metadata") or {}).get("remediation") or {}).get("episode_id")
        if execution_ownership.claim_episode(conn, episode_id, run_now):
            accepted.append(event)
            supervision_decisions.executed(
                conn,
                config,
                event["workstream_id"],
                {
                    "episode_id": episode_id,
                    "event_fingerprint": event["event_fingerprint"],
                    "status": "accepted_for_preparation",
                },
                run_now,
            )
    return accepted


def handle_blocked_remediation_worktree(
    conn,
    event,
    worktree_result,
    run_now,
):
    if event.get("task_kind") not in {
        "ci_remediation",
        "merge_conflict_remediation",
    }:
        return False
    if not isinstance(worktree_result, dict) or worktree_result.get(
        "ok",
        True,
    ):
        return False
    remediation_context = (event.get("metadata") or {}).get("remediation") or {}
    episode_id = remediation_context.get("episode_id")
    if episode_id and not execution_ownership.claim_episode(conn, episode_id, run_now):
        return False
    if episode_id:
        reason = worktree_result.get("status") or "remediation_worktree_unavailable"
        if reason == "stale_snapshot":
            remediation.supersede_episode(
                conn,
                episode_id=episode_id,
                reason=reason,
                now=run_now,
            )
        else:
            remediation.exhaust_episode(
                conn,
                episode_id=episode_id,
                reason=reason,
                now=run_now,
            )
        execution_ownership.close_subject(conn, "remediation_episode", episode_id, run_now)
    return True


def _decision_facts(episode):
    return {
        key: episode.get(key)
        for key in (
            "episode_id",
            "episode_kind",
            "workstream_id",
            "observed_head_sha",
            "observed_base_sha",
            "failure_signature",
            "attempt_count",
            "status",
        )
    }
