"""Feed preserved upgrade facts into the native domain pipeline.

The migration queue is acknowledged only after an existing result or recorded
Task event proves a domain effect. Wakeups alone are never consumption proof.
"""

import json

from robert_agent import execution_store


def decisions(conn, repo_id, now, fresh_system_decisions, repo=None):
    from robert_agent import run_once

    pending = execution_store.pending_facts(conn, repo_id, now)
    output = []
    for fact in pending:
        control = execution_store.get_binding(
            conn, fact["subject_type"], fact["subject_id"]
        )
        if not control or control["binding_status"] == "attention":
            continue
        if control["binding_status"] == "terminal":
            if fact["subject_type"] == "task":
                terminal = conn.execute(
                    "SELECT 1 FROM tasks WHERE task_id=? AND lifecycle IN ('completed','failed','canceled','ignored')",
                    (fact["subject_id"],),
                ).fetchone()
            else:
                terminal = conn.execute(
                    "SELECT 1 FROM pr_remediation_episodes WHERE episode_id=? AND status IN ('resolved','exhausted','superseded','canceled')",
                    (fact["subject_id"],),
                ).fetchone()
            if terminal:
                execution_store.consume_fact(
                    conn, fact["source_signal_id"], now, reason="subject_terminal"
                )
            else:
                conn.execute(
                    "UPDATE execution_subjects SET binding_status='attention',metadata_json=json_set(metadata_json,'$.reason_code','terminal_execution_domain_mismatch','$.required_action','Inspect task state before resuming migrated events.') WHERE subject_type=? AND subject_id=?",
                    (fact["subject_type"], fact["subject_id"]),
                )
            continue
        if fact["disposition"] == "worker_result":
            row = conn.execute(
                "SELECT task_id FROM worker_results WHERE result_id=?",
                (fact["fact_id"],),
            ).fetchone()
            if row and row[0] == fact["task_id"]:
                execution_store.consume_fact(conn, fact["source_signal_id"], now)
            continue
        event = json.loads(fact["payload_json"])
        if fact["subject_type"] == "remediation_episode":
            episode = conn.execute(
                "SELECT status,attempt_count FROM pr_remediation_episodes WHERE episode_id=?",
                (fact["subject_id"],),
            ).fetchone()
            if not episode:
                continue
            if (
                episode[0] in {"resolved", "exhausted", "superseded", "canceled"}
                or fact["dedupe_key"] != f"{fact['fact_id']}:{episode[1]}"
            ):
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
                or ((event.get("metadata") or {}).get("remediation") or {}).get(
                    "episode_id"
                )
                != fact["subject_id"]
            ):
                execution_store.mark_binding_attention(
                    conn, fact["subject_type"], fact["subject_id"], now
                )
                continue
            # The regular health collector revalidates snapshot, policy and
            # budgets. Never dispatch an archived system trigger on its own.
            if not any(
                fresh.get("event_fingerprint") == fact["fact_id"]
                and ((fresh.get("metadata") or {}).get("remediation") or {}).get(
                    "episode_id"
                )
                == fact["subject_id"]
                for fresh in fresh_system_decisions
            ):
                continue
            continue  # already present in the freshly validated decision batch
        driving = (
            run_once._is_driving_context_event(event)
            or event.get("authorization_status") == "authorized_trigger"
        )
        if driving and repo is not None and not run_once._is_trusted_actor(event, repo):
            execution_store.mark_binding_attention(
                conn, fact["subject_type"], fact["subject_id"], now
            )
            conn.execute(
                "UPDATE execution_subjects SET metadata_json=json_set(metadata_json,'$.reason_code','migrated_event_requires_trusted_actor','$.required_action','Review migrated event authorization before retrying.') WHERE subject_type=? AND subject_id=?",
                (fact["subject_type"], fact["subject_id"]),
            )
            continue
        if not driving:
            # Append the whole non-driving batch before any trusted reply can
            # close its parent. Context is data, never dispatch authorization.
            if run_once._append_event_to_active_task(conn, event, "context", now):
                execution_store.consume_fact(conn, fact["source_signal_id"], now)
            continue
        output.append(event)
    return output


def acknowledge_effects(conn, repo_id, now):
    for fact in execution_store.pending_facts(conn, repo_id, now):
        if fact["disposition"] != "github_event":
            continue
        linked = conn.execute(
            """SELECT t.task_id,t.lifecycle FROM task_events te
            JOIN github_events ge ON ge.event_id=te.event_id
            JOIN tasks t ON t.task_id=te.task_id WHERE ge.event_fingerprint=?""",
            (fact["fact_id"],),
        ).fetchall()
        if any(
            task_id != fact["task_id"] or lifecycle in {"queued", "running"}
            for task_id, lifecycle in linked
        ):
            execution_store.consume_fact(conn, fact["source_signal_id"], now)
