"""Shared lifecycle facts and safe operator recovery for a work item."""
import json
from robert_agent import supervise

from robert_agent.board_execution import safe_text
from robert_agent.publication_lock import task_publication_lock


def related_workstreams(conn, workstream_id):
    return [row[0] for row in conn.execute(
        "SELECT workstream_id FROM workstreams WHERE workstream_id = ? OR origin_workstream_id = ?",
        (workstream_id, workstream_id),
    )]


def lifecycle_facts(conn, item):
    streams = related_workstreams(conn, item["workstream_id"])
    placeholders = ','.join('?' for _ in streams) or 'NULL'
    attempts = [dict(row) for row in conn.execute(
        f"""SELECT a.attempt_id, a.task_id, a.status, a.metadata_json, gs.state AS source_state FROM attempts a
        JOIN tasks t ON t.task_id=a.task_id
        JOIN workstreams w ON w.workstream_id=t.workstream_id
        LEFT JOIN github_sources gs ON gs.source_id=w.primary_source_id WHERE t.workstream_id IN ({placeholders})
        AND {supervise.occupied_attempt_sql()}""", streams,
    )]
    cleanup = []
    for attempt in attempts:
        try:
            metadata = json.loads(attempt['metadata_json'] or '{}')
            termination = metadata.get('termination') or {}
            supervision = metadata.get('supervise') or {}
            if isinstance(supervision, dict) and supervision.get('status') == 'worker_identity_attention':
                cleanup.append(safe_text(supervision.get('reason') or 'Automatic recovery stopped: verify worker identity or recovery quota, then cancel this execution.'))
        except (ValueError, AttributeError):
            termination = {}
        if isinstance(termination, dict) and termination.get('status') not in {None, 'terminated', 'not_found', 'already_dead'}:
            cleanup.append(safe_text(termination.get('reason') or termination.get('status')))
    actions = [dict(row) for row in conn.execute(
        f"""SELECT ga.*, pi.status AS intent_status FROM github_actions ga JOIN tasks t ON t.task_id=ga.task_id
        LEFT JOIN publication_intents pi ON pi.action_id=ga.action_id
        WHERE t.workstream_id IN ({placeholders}) AND ga.publish_status='not_published'
        AND ga.audit_status IN ('pending','accepted') ORDER BY ga.created_at, ga.action_id""", streams,
    )]
    failed = []
    retry_blocked = False
    for action in actions:
        try:
            metadata = json.loads(action['metadata_json'] or '{}')
            publication = metadata.get('publish') or {}
        except (ValueError, AttributeError):
            publication = {}
        if action['intent_status'] == 'attention' or (isinstance(publication, dict) and publication.get('retryable') is False):
            retry_blocked = True
        if action['intent_status'] == 'attention' and not publication:
            publication = {'status': 'publish_failed', 'safe_error': 'Publication reconciliation requires operator attention.'}
        if isinstance(publication, dict) and publication.get('status') == 'publish_failed':
            failed.append({'action_id': action['action_id'], 'type': action['action_type'],
                           'reason': safe_text(publication.get('safe_error')) + ' ' + safe_text(publication.get('required_action') or (('Next retry: ' + publication['next_retry_at']) if publication.get('next_retry_at') else '')), 'failed_at': publication.get('failed_at'),
                           **{key: publication.get(key) for key in ('reason_code','attempt_count','max_attempts','retryable','next_retry_at','required_action')}})
    attention = []
    from robert_agent import execution_store
    subjects=set()
    for task_id,episode_id in conn.execute(f"SELECT task_id,json_extract(metadata_json,'$.remediation_episode_id') FROM tasks WHERE workstream_id IN ({placeholders})",streams):
        subjects.add(('task',task_id))
        if episode_id:
            subjects.add(('remediation_episode',episode_id))
    for key,binding in execution_store.read_controls(conn).items():
        if key in subjects and (binding['binding_status']=='attention' or (binding.get('owner_engine')=='dbos' and binding['binding_status']!='terminal')):
            metadata=json.loads(binding['metadata_json'] or '{}')
            attention.append(safe_text(metadata.get('reason_code') or 'Execution requires operator attention or offline migration.'))
    source = conn.execute("SELECT state FROM github_sources WHERE source_id=?", (item['origin_source_id'],)).fetchone()
    pending = conn.execute(
        """SELECT 1 FROM wakeups WHERE work_item_id=? AND status='pending'
        AND json_extract(metadata_json, '$.kind')='publication_retry' LIMIT 1""", (item['work_item_id'],),
    ).fetchone()
    return {'streams': streams, 'running': attempts, 'actions': actions, 'publish_failures': failed,
            'workflow_attention': attention, 'publication_retry_pending': bool(pending), 'publication_retry_blocked': retry_blocked, 'cleanup_pending': cleanup,
            'source_closed': bool(source and (source[0] or '').lower() == 'closed') or any((a['source_state'] or '').lower() == 'closed' for a in attempts)}


def require_idle_repository(conn, repo_id, now):
    from robert_agent.work_items import WorkItemConflictError
    busy = conn.execute(
        """SELECT 1 FROM leases WHERE resource_type='agent_run' AND resource_key=?
        AND status='active' AND expires_at>? LIMIT 1""", (repo_id, now),
    ).fetchone()
    if busy:
        raise WorkItemConflictError('The repository is processing a cycle. Refresh and retry after it finishes.')


def cancel_execution(conn, item, facts, now, db_path, locks, *, abandon_review_run_ids=()):
    from robert_agent import run_once, work_items
    # The write transaction prevents a new engine lease. Publication has its
    # own task lock, held until this command commits; neither lock waits on I/O.
    require_idle_repository(conn, item['repo_id'], now)
    placeholders = ','.join('?' for _ in facts['streams']) or 'NULL'
    task_ids = conn.execute(f"SELECT task_id FROM tasks WHERE workstream_id IN ({placeholders}) ORDER BY task_id", facts['streams'])
    task_ids = list(task_ids)
    for (task_id,) in task_ids:
        if not locks.enter_context(task_publication_lock(db_path, task_id)):
            raise work_items.WorkItemConflictError('A publication is in progress for this task. Retry cancellation after it finishes.')
    from robert_agent import native_rounds
    unknown = {}
    for (task_id,) in task_ids:
        round_store, review = native_rounds.owner(conn, task_id)
        if review:
            unknown.update({run['run_id']: {**run, 'store': round_store}
                            for run in round_store.runs(conn, review['round_id']) if round_store.can_abandon(run)})
    if set(abandon_review_run_ids) - unknown.keys():
        raise work_items.WorkItemConflictError('The selected unknown review launch changed. Refresh and inspect before abandoning it.')
    for attempt in facts['running']:
        try:
            metadata = json.loads(attempt['metadata_json'] or '{}')
        except ValueError:
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        outcome = run_once._terminate_attempt_process(metadata)
        if outcome.get('status') not in {'terminated', 'not_found', 'already_dead'}:
            raise work_items.WorkItemConflictError('Worker could not be stopped: ' + str(outcome.get('reason') or outcome.get('status')))
        metadata['termination'] = outcome
        metadata['process_stopped_at'] = now
        conn.execute('UPDATE attempts SET metadata_json=? WHERE attempt_id=?', (json.dumps(metadata), attempt['attempt_id']))
    # This is an explicit operator attestation, never an automatic inference
    # that an unrecorded process died. The task locks prevent concurrent spawn.
    for run_id in abandon_review_run_ids:
        unknown[run_id]['store'].update_run(conn, run_id, status='abandoned', slot_reserved=0,
                                error='operator confirmed untracked model processes were stopped')
    from robert_agent.native_rounds import terminate_children
    for (task_id,) in task_ids:
        if terminate_children(conn, task_id) == 'attention':
            raise work_items.WorkItemConflictError('Review worker identity could not be verified. Inspect and stop residual processes, then explicitly confirm abandonment of unknown launches when canceling.')
    from robert_agent import execution_store
    subjects = {('task', task_id) for (task_id,) in task_ids}
    for (task_id,) in task_ids:
        task = conn.execute('SELECT task_kind, metadata_json FROM tasks WHERE task_id=?', (task_id,)).fetchone()
        if task and task[0] in {'ci_remediation', 'merge_conflict_remediation'}:
            metadata = work_items._decode_json(task[1], {}) or {}
            if metadata.get('remediation_episode_id'):
                subjects.add(('remediation_episode', metadata['remediation_episode_id']))
    for subject_type, subject_id in sorted(subjects):
        binding = execution_store.get_binding(conn, subject_type, subject_id)
        if not binding or binding['binding_status'] == 'terminal':
            continue
        execution_store.close_binding(conn, subject_type, subject_id, now)
        if subject_type == 'remediation_episode':
            conn.execute("UPDATE pr_remediation_episodes SET status='canceled', updated_at=? WHERE episode_id=? AND status NOT IN ('resolved','exhausted','superseded','canceled')", (now, subject_id))
    placeholders = ','.join('?' for _ in facts['streams']) or 'NULL'
    params = [now] + facts['streams']
    conn.execute(f"UPDATE tasks SET lifecycle='canceled', updated_at=? WHERE workstream_id IN ({placeholders}) AND lifecycle NOT IN ('completed','ignored','canceled')", params)
    conn.execute(f"UPDATE attempts SET status='canceled', finished_at=? WHERE task_id IN (SELECT task_id FROM tasks WHERE workstream_id IN ({placeholders})) AND status IN ('prepared','running','stale')", params)
    conn.execute(f"UPDATE workstreams SET lifecycle='canceled', active_task_id=NULL, updated_at=? WHERE workstream_id IN ({placeholders})", params)
    conn.execute(f"UPDATE review_sessions SET status='canceled',error='operator canceled work item',updated_at=? WHERE workstream_id IN ({placeholders}) AND status NOT IN ('published','superseded','canceled')", params)
    conn.execute(f"UPDATE vote_sessions SET status='canceled',error='operator canceled work item',updated_at=? WHERE workstream_id IN ({placeholders}) AND status NOT IN ('published','superseded','canceled')", params)
    conn.execute(f"UPDATE github_actions SET publish_status='skipped' WHERE task_id IN (SELECT task_id FROM tasks WHERE workstream_id IN ({placeholders})) AND publish_status='not_published'", facts['streams'])
    conn.execute("UPDATE wakeups SET status='canceled', updated_at=? WHERE work_item_id=? AND status='pending'", (now, item['work_item_id']))


def current_task(conn, streams):
    placeholders = ','.join('?' for _ in streams) or 'NULL'
    return conn.execute(
        f"""SELECT t.* FROM tasks t JOIN workstreams w ON w.workstream_id=t.workstream_id
        WHERE t.workstream_id IN ({placeholders})
        ORDER BY EXISTS(SELECT 1 FROM attempts a WHERE a.task_id=t.task_id AND a.status IN ('running','stale')) DESC,
          (t.task_id=w.active_task_id) DESC, t.created_at DESC, t.task_id DESC LIMIT 1""", streams,
    ).fetchone()
