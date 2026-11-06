"""Supervision fences around the existing publisher's exact action and task lock."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3

from robert_agent import supervision_actions, supervision_store


def _connect(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def needs_config(config):
    settings = (config or {}).get('supervision') or {}
    return settings.get('mode') == 'recover_known' or bool(settings.get('repair'))


def fresh_config(config, db=None):
    if config and config.get('config_path'):
        from robert_agent import validate_config
        current = validate_config.validate_config(config['config_path'], skip_external=True)
        if not current.get('ok') or (db and Path(current['db_path']).resolve() != Path(db).resolve()):
            return {}
        return current
    return config or {}


def _repair_allowed(conn, action, config):
    from robert_agent import supervision_repair
    repair = supervision_repair.repair_task(conn, action['task_id'])
    if not repair:
        return False, True
    repo = conn.execute('''SELECT w.repo_id FROM workstreams w JOIN tasks t ON t.workstream_id=w.workstream_id
        WHERE t.task_id=?''', (action['task_id'],)).fetchone()
    return True, bool(repo and supervision_repair.authorized_repair(
        conn, action['task_id'], config, repo[0], action_type=action['action_type']))


def begin(db, action, config, now):
    """Run inside the publisher's already-held task publication lock."""
    current = fresh_config(config, db)
    if config and config.get('config_path') and not current:
        return {'status': 'blocked', 'reason_code': 'recovery_policy_unavailable'}
    config = current
    with closing(_connect(db)) as conn, conn:
        repair, allowed = _repair_allowed(conn, action, config)
        if not allowed:
            return {'status': 'blocked', 'reason_code': 'maintenance_policy_rejected'}
        intent = conn.execute('SELECT status,remote_evidence_json FROM publication_intents WHERE action_id=?',
                              (action['action_id'],)).fetchone()
        executing = intent and intent['status'] == 'executing'
        evidence = json.loads(intent['remote_evidence_json'] or '{}') if intent else {}
        needs_claim = bool(evidence.get('supervision_retry_required'))
        kind = 'publication_reconcile' if executing else 'publication_retry'
        native_retry = bool(executing or needs_claim or (json.loads(action['metadata_json'] or '{}').get('publish') or {}).get('attempt_count'))
        if not native_retry and supervision_store.has_schema(conn) and conn.execute("""SELECT 1
            FROM supervision_recovery_actions WHERE scope_type='task' AND scope_id=?
              AND action_kind NOT IN ('publication_retry','publication_reconcile')
              AND status IN ('requested','applying','observing','unknown')""", (action['task_id'],)).fetchone():
            # The domain has already accepted this first publication. Do not
            # wait for an unrelated recovery's observational bookkeeping.
            return {'status': 'legacy', 'guard_required': repair, 'reconcile_only': False}
        verdict = supervision_actions.begin(conn, task_id=action['task_id'], kind=kind,
            target_id=action['action_id'], config=config, now=now, native_retry=native_retry)
        # A recovered task lock permits reading the same earlier remote effect,
        # even if the writer crashed after it charged its recovery claim.
        if verdict['status'] == 'blocked' and executing and supervision_store.has_schema(conn):
            row = conn.execute("""SELECT * FROM supervision_recovery_actions WHERE scope_type='task'
                AND scope_id=? AND target_id=? AND action_kind IN ('publication_retry','publication_reconcile')
                AND status IN ('applying','unknown','observing')""", (action['task_id'], action['action_id'])).fetchone()
            if row:
                return {'status': 'reconcile_only', 'claim': dict(row), 'reconcile_only': True}
        if needs_claim and not executing and verdict['status'] != 'claimed':
            return {'status': 'blocked', 'reason_code': verdict.get('reason_code', 'recovery_policy_required')}
    return {**verdict, 'guard_required': repair or verdict['status'] == 'claimed',
            'reconcile_only': bool(executing and (needs_claim or verdict['status'] == 'claimed'))}


def guard(db, action, state, config, now):
    """Check current policy and the durable claim just before a remote mutation."""
    config = fresh_config(config, db)
    with closing(_connect(db)) as conn:
        repair, allowed = _repair_allowed(conn, action, config)
        if not allowed or state.get('reconcile_only'):
            return False
        if state.get('claim') and state['status'] == 'claimed':
            if not supervision_actions.publication_current(conn, state['claim'], config=config, now=now):
                return False
            if not repair:
                from robert_agent import native_rounds, run_once
                row = conn.execute('''SELECT r.full_name FROM tasks t JOIN workstreams w ON w.workstream_id=t.workstream_id
                    JOIN repos r ON r.repo_id=w.repo_id WHERE t.task_id=?''', (action['task_id'],)).fetchone()
                repo = next((value for value in config.get('repos', ()) if row and value['full_name'] == row[0]), None)
                if not repo:
                    return False
                native = native_rounds.session(conn, task_id=action['task_id'])
                return (run_once._native_request_allowed(native, repo, config) if native
                        else run_once._ordinary_dispatch_allowed(conn, action['task_id'], repo))
    return True


def finish(conn, state, *, outcome, action_id, now, reason=None):
    """Write the claim receipt in the same transaction as the publisher result."""
    claim = state.get('claim')
    if claim:
        receipt = {'action_id': action_id}
        if reason:
            receipt['reason_code'] = reason
        supervision_actions.finish(conn, claim, outcome=outcome, receipt=receipt, now=now)
