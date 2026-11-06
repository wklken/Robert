"""Claim and close execution subjects in the caller-owned transaction."""
import json
from datetime import datetime, timezone
from robert_agent import execution_store, execution_operations
from robert_agent.execution import notifications as execution_notifications


def _json_object(value):
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def subject_for_task(conn,task_id):
    row=conn.execute('SELECT task_kind,metadata_json FROM tasks WHERE task_id=?',(task_id,)).fetchone()
    episode=_json_object(row[1]).get('remediation_episode_id') if row else None
    return ('remediation_episode',episode) if episode and row[0] in {'ci_remediation','merge_conflict_remediation'} else ('task',task_id)


def claim_task(conn, task_id, run_now=None):
    """Check a subject's persisted execution state before a task mutation.

    Remediation children share their Episode control and recovery budget.
    """

    row = conn.execute(
        "SELECT task_kind, metadata_json FROM tasks WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    if not row:
        return False
    run_now = run_now or datetime.now(timezone.utc).isoformat()
    task_kind, metadata_json = row
    if (_json_object(metadata_json).get('execution_attention')
            or _json_object(metadata_json).get('audit_retry_required')):
        return False
    if task_kind == "human_request":
        return execution_operations.claim_subject(
            conn,
            "task",
            task_id,
            now=run_now,
        ) is not None
    if task_kind in {"ci_remediation", "merge_conflict_remediation"}:
        episode_id = _json_object(metadata_json).get("remediation_episode_id")
        if not episode_id:
            metadata=_json_object(metadata_json)
            if 'execution_attention' not in metadata:
                metadata['execution_attention']={'reason_code':'remediation_episode_missing','required_action':'Inspect the historical remediation task and restore its Episode association before retrying.'}
                conn.execute('UPDATE tasks SET metadata_json=? WHERE task_id=?',(json.dumps(metadata),task_id))
                execution_notifications.insert(conn,'execution_attention_required','recorded',run_now,{'task_id':task_id,**metadata['execution_attention']})
            return False
        return execution_operations.claim_subject(
            conn,
            "remediation_episode",
            episode_id,
            now=run_now,
        ) is not None
    return False


def claim_episode(conn, episode_id, run_now=None):
    if not episode_id:
        return False
    return (
        execution_operations.claim_subject(
            conn,
            "remediation_episode",
            episode_id,
            now=run_now or datetime.now(timezone.utc).isoformat(),
        )
        is not None
    )


def parent_allows_child(conn, parent_task_id, *, retrigger_wakeup_id=None):
    if not parent_task_id:
        return True
    if conn.execute("SELECT 1 FROM tasks WHERE task_id=? AND json_extract(metadata_json,'$.execution_attention') IS NOT NULL", (parent_task_id,)).fetchone():
        return False
    binding = execution_store.get_binding(conn, "task", parent_task_id)
    if retrigger_wakeup_id is not None:
        # An audited operator request starts a new subject. It never reopens
        # the failed parent or transfers a DBOS-owned subject to legacy.
        requested = conn.execute(
            """SELECT 1 FROM wakeups wu JOIN tasks t ON t.task_id=wu.task_id
            JOIN workstreams w ON w.workstream_id=t.workstream_id
            JOIN github_sources gs ON gs.source_id=w.primary_source_id
            WHERE wu.wakeup_id=? AND wu.task_id=? AND wu.repo_id=w.repo_id
              AND wu.status='pending' AND wu.reason='manual_operator_request'
              AND json_extract(wu.metadata_json,'$.kind')='github_retrigger'
              AND t.task_kind='human_request' AND t.lifecycle='failed'
              AND w.lifecycle='failed' AND w.active_task_id IS NULL
              AND lower(gs.state)='open'""", (retrigger_wakeup_id, parent_task_id),
        ).fetchone()
        return bool(requested) and (not binding or (
            binding['binding_status'] == 'terminal' or (binding['binding_status'] == 'active')
        ))
    return not binding or (
        binding["binding_status"] == "active"
    )


def reconcile_task_allowed(conn, task_id, run_now):
    if conn.execute("SELECT 1 FROM tasks WHERE task_id=? AND json_extract(metadata_json,'$.execution_attention') IS NOT NULL", (task_id,)).fetchone():
        return False
    binding = execution_store.get_binding(conn, "task", task_id)
    if binding:
        return binding["binding_status"] in {
            "active",
            "terminal",
        }
    row = conn.execute(
        "SELECT lifecycle FROM tasks WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    if not row:
        return False
    if row[0] in {"completed", "failed", "canceled", "ignored"}:
        return True
    return claim_task(conn, task_id, run_now)


def close_subject(conn, subject_type, subject_id, run_now):
    binding = execution_store.get_binding(conn, subject_type, subject_id)
    if binding and binding["binding_status"] == "active":
        execution_store.close_binding(conn, subject_type, subject_id, run_now)


def close_terminal_task(conn, task_id, run_now):
    row = conn.execute(
        "SELECT task_kind, lifecycle, metadata_json FROM tasks WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    if not row:
        return
    task_kind, lifecycle, metadata_json = row
    if lifecycle not in {"completed", "failed", "canceled", "ignored"}:
        return
    if task_kind == "human_request":
        close_subject(conn, "task", task_id, run_now)
        return
    episode_id = _json_object(metadata_json).get("remediation_episode_id")
    if episode_id:
        episode = conn.execute(
            "SELECT status FROM pr_remediation_episodes WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
        if episode and episode[0] in {"resolved", "exhausted", "superseded", "canceled"}:
            close_subject(conn, "remediation_episode", episode_id, run_now)


def recovery_subject(conn, task_id):
    row = conn.execute(
        "SELECT metadata_json FROM tasks WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    episode_id = _json_object(row[0] if row else None).get("remediation_episode_id")
    if episode_id and execution_store.get_binding(
        conn, "remediation_episode", episode_id
    ):
        return "remediation_episode", episode_id
    return "task", task_id


def mark_attention(conn, task_id, run_now):
    subject_type, subject_id = recovery_subject(conn, task_id)
    candidates = [(subject_type, subject_id)]
    if (subject_type, subject_id) != ("task", task_id):
        candidates.append(("task", task_id))
    for candidate_type, candidate_id in candidates:
        binding = execution_store.get_binding(conn, candidate_type, candidate_id)
        if binding and binding["binding_status"] == "active":
            execution_store.mark_binding_attention(
                conn, candidate_type, candidate_id, run_now
            )
            return


def waiting_reply_allowed(conn, task_id, now):
    row = conn.execute('SELECT task_kind,lifecycle,metadata_json FROM tasks WHERE task_id=?', (task_id,)).fetchone()
    if not row or row[1] != 'waiting_for_user':
        return False
    if row[0] in {'ci_remediation', 'merge_conflict_remediation'}:
        episode = _json_object(row[2]).get('remediation_episode_id')
        state = conn.execute('SELECT status,metadata_json FROM pr_remediation_episodes WHERE episode_id=?', (episode,)).fetchone()
        if state and state[0] == 'exhausted' and _json_object(state[1]).get('last_transition_reason') == 'worker_waiting_for_user':
            return True  # The trusted reply owns a fresh human subject, not the ended Episode.
    return claim_task(conn, task_id, now)
