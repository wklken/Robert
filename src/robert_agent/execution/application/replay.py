"""Feed preserved upgrade facts into the native domain pipeline.

The migration queue is acknowledged only after an existing result or recorded
Task event proves a domain effect. Wakeups alone are never consumption proof.
"""

from robert_agent.foundation.contracts.execution import BindingStatus
from robert_agent.foundation.contracts.work import TERMINAL_REMEDIATION_STATUSES, TaskLifecycle

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import pr_remediation_episodes as db_read_pr_remediation_episodes
from robert_agent.persistence.queries import task_events as db_read_task_events
from robert_agent.persistence.queries import tasks as db_read_tasks
from robert_agent.persistence.queries import worker_results as db_read_worker_results
from robert_agent.persistence.stores import execution_subjects as db_write_execution_subjects

import json

from robert_agent.persistence import execution_control as execution_store


def decisions(conn, repo_id, now, fresh_system_decisions, repo=None):
    from robert_agent.work.application import admission as work_admission
    from robert_agent.work import policies as work_policies

    pending = execution_store.pending_facts(conn, repo_id, now)
    output = []
    for fact in pending:
        control = execution_store.get_binding(conn, fact["subject_type"], fact["subject_id"])
        if not control or control["binding_status"] == BindingStatus.ATTENTION:
            continue
        if control["binding_status"] == BindingStatus.TERMINAL:
            if fact["subject_type"] == "task":
                terminal = db_records.first(db_read_tasks.find_for_decisions(conn, fact["subject_id"]))
            else:
                terminal = db_records.first(
                    db_read_pr_remediation_episodes.find_for_decisions(conn, fact["subject_id"])
                )
            if terminal:
                execution_store.consume_fact(conn, fact["source_signal_id"], now, reason="subject_terminal")
            else:
                db_write_execution_subjects.update_for_decisions_2(conn, fact["subject_type"], fact["subject_id"])
            continue
        if fact["disposition"] == "worker_result":
            row = db_records.first(db_read_worker_results.find_task_id_by_result_id(conn, fact["fact_id"]))
            if row and row[0] == fact["task_id"]:
                execution_store.consume_fact(conn, fact["source_signal_id"], now)
            continue
        event = json.loads(fact["payload_json"])
        if fact["subject_type"] == "remediation_episode":
            episode = db_records.first(
                db_read_pr_remediation_episodes.find_status_and_attempt_count_by_episode_id(conn, fact["subject_id"])
            )
            if not episode:
                continue
            if episode[0] in TERMINAL_REMEDIATION_STATUSES or fact["dedupe_key"] != f"{fact['fact_id']}:{episode[1]}":
                execution_store.consume_fact(
                    conn,
                    fact["source_signal_id"],
                    now,
                    reason="superseded_remediation_decision",
                )
                continue
            if (
                fact["topic"] != "remediation_decision"
                or event.get("authorization_status") != "authorized_system_trigger"
                or ((event.get("metadata") or {}).get("remediation") or {}).get("episode_id") != fact["subject_id"]
            ):
                execution_store.mark_binding_attention(conn, fact["subject_type"], fact["subject_id"], now)
                continue
            # The regular health collector revalidates snapshot, policy and
            # budgets. Never dispatch an archived system trigger on its own.
            if not any(
                fresh.get("event_fingerprint") == fact["fact_id"]
                and ((fresh.get("metadata") or {}).get("remediation") or {}).get("episode_id") == fact["subject_id"]
                for fresh in fresh_system_decisions
            ):
                continue
            continue  # already present in the freshly validated decision batch
        driving = (
            work_policies.is_driving_context_event(event) or event.get("authorization_status") == "authorized_trigger"
        )
        if driving and repo is not None and not work_policies.is_trusted_actor(event, repo):
            execution_store.mark_binding_attention(conn, fact["subject_type"], fact["subject_id"], now)
            db_write_execution_subjects.update_for_decisions(conn, fact["subject_type"], fact["subject_id"])
            continue
        if not driving:
            # Append the whole non-driving batch before any trusted reply can
            # close its parent. Context is data, never dispatch authorization.
            if work_admission.append_event_to_active_task(conn, event, "context", now):
                execution_store.consume_fact(conn, fact["source_signal_id"], now)
            continue
        output.append(event)
    return output


def acknowledge_effects(conn, repo_id, now):
    for fact in execution_store.pending_facts(conn, repo_id, now):
        if fact["disposition"] != "github_event":
            continue
        linked = db_read_task_events.find_for_acknowledge_effects(conn, fact["fact_id"])
        if any(
            task_id != fact["task_id"] or lifecycle in {TaskLifecycle.QUEUED.value, TaskLifecycle.RUNNING.value}
            for task_id, lifecycle in linked
        ):
            execution_store.consume_fact(conn, fact["source_signal_id"], now)
