"""application delivery publish responsibilities."""
import argparse
from contextlib import closing
import sqlite3
import subprocess
from robert_agent.delivery.application import reconcile as publication_reconcile
from robert_agent.execution import operations as execution_operations
from robert_agent.adapters.sqlite import execution_control as execution_store
from robert_agent.supervision.application import publication as supervision_publication
from robert_agent.foundation.commands import emit, run_command as control_command
from robert_agent.delivery.lock import task_publication_lock

from robert_agent.delivery.application import effects as publication_effects
from robert_agent.delivery.application import scope as publication_scope
from robert_agent.delivery.application import intents as publication_intents
from robert_agent.delivery.application import outcomes as publication_outcomes
from robert_agent.delivery.application import receipts as publication_receipts
from robert_agent.delivery import intents as intent_facts
from robert_agent.delivery import queries as publication_queries
from robert_agent.delivery import values as publication_values

PUBLISH_COMMAND_TIMEOUT_SECONDS = 60


def publish_ready_actions(db_path, dry_run=False, limit=20, run_command=subprocess.run, repo_id=None, task_ids=None,
                          *, action_ids=None, config=None):
    def bounded_command(*args, **kwargs):
        kwargs["timeout"] = min(kwargs.get("timeout") or PUBLISH_COMMAND_TIMEOUT_SECONDS, PUBLISH_COMMAND_TIMEOUT_SECONDS)
        return control_command(*args, runner=run_command, **kwargs)

    with closing(sqlite3.connect(db_path)) as conn:
        execution_store.require_migrated(conn)
        from robert_agent.execution.application import cutover as result_cutover
        cutover = result_cutover.inspect(conn, db_path) if dry_run else result_cutover.prepare(db_path)
        if not cutover["ok"]:
            return {**cutover, "pending_count": 0, "published_count": 0,
                    "deduplicated_count": 0, "skipped_count": 0, "failed_count": 0}
        conn.row_factory = sqlite3.Row
        actions = publication_queries.ready_actions(conn, limit, repo_id=repo_id, task_ids=task_ids, action_ids=action_ids)
    if dry_run:
        return {
            "ok": True,
            "status": "dry_run",
            "pending_count": len(actions),
            "published_count": 0,
            "deduplicated_count": 0,
            "skipped_count": 0,
            "failed_count": 0,
        }
    deferred_count = 0
    published_count = 0
    deduplicated_count = 0
    skipped_count = 0
    failed_count = 0
    failures = []
    failed_result_ids = set()
    failed_remediation_result_ids = set()
    busy_task_ids = set()
    for action in actions:
        if action["task_id"] in busy_task_ids:
            continue
        with task_publication_lock(db_path, action["task_id"]) as acquired:
            if not acquired:
                busy_task_ids.add(action["task_id"])
                continue
            # A failed remediation push retires its following comment even
            # when that failure has already made the execution non-runnable.
            if action['result_id'] in failed_remediation_result_ids:
                with closing(sqlite3.connect(db_path)) as conn, conn:
                    conn.row_factory = sqlite3.Row
                    latest = conn.execute('SELECT * FROM github_actions WHERE action_id=?', (action['action_id'],)).fetchone()
                    if latest and latest['publish_status'] not in {'published', 'skipped'}:
                        publication_outcomes.mark_skipped(conn, dict(latest), {'ok': False, 'status': 'publish_failed',
                            'safe_error': 'skipped because the preceding remediation push failed'}, publication_values.now())
                        skipped_count += 1
                continue
            # The selection page predates this lock. Another publisher may
            # already have completed the action or changed its eligibility.
            with closing(sqlite3.connect(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                ready = publication_queries.ready_actions(conn, 1, action_ids=[action['action_id']])
            if not ready:
                continue
            action = ready[0]
            # Consume the request before this pass, including reconciliation
            # failures, so a failed attempt never leaves retry disabled.
            with closing(sqlite3.connect(db_path)) as retry_conn, retry_conn:
                retry_conn.execute(
                    """UPDATE wakeups SET status='consumed', updated_at=?
                    WHERE task_id=? AND status='pending'
                    AND json_extract(metadata_json, '$.kind')='publication_retry'""",
                    (publication_values.now(), action["task_id"]),
                )
            remote_effect_started = False
            supervision = {}
            try:
                result_id = action["result_id"]
                now = publication_values.now()
                if result_id and result_id in failed_result_ids:
                    if result_id in failed_remediation_result_ids:
                        result = {
                            "ok": False,
                            "status": "publish_failed",
                            "safe_error": (
                                "skipped because the preceding remediation "
                                "push failed"
                            ),
                        }
                        with closing(sqlite3.connect(db_path)) as conn, conn:
                            conn.row_factory = sqlite3.Row
                            publication_outcomes.mark_skipped(conn, action, result, now)
                        skipped_count += 1
                    continue
                with closing(sqlite3.connect(db_path)) as conn, conn:
                    conn.row_factory = sqlite3.Row
                    prior = publication_values.decode_json(action['metadata_json']).get('publish') or {}
                    existing_intent = intent_facts.load_intent(conn, action['action_id'])
                    # Unknown effects can still be reconciled after the write budget is exhausted.
                    if (not existing_intent or existing_intent['status'] not in {'executing', 'published'}) and publication_queries.retry_budget_exhausted(prior, now):
                        publication_outcomes.record_failure(conn, action, {'safe_error': 'Automatic publication retry budget exhausted. Inspect and retry explicitly.', 'retryable': False}, now)
                        failed_count += 1
                        if result_id:
                            failed_result_ids.add(result_id)
                        continue
                try:
                    supervision = supervision_publication.begin(db_path, action, config, now)
                except sqlite3.Error:
                    # No remote effect or publication attempt has started. Leave
                    # this task's ordered actions pending and try other tasks.
                    deferred_count += 1
                    busy_task_ids.add(action['task_id'])
                    continue
                if supervision['status'] == 'blocked':
                    if result_id:
                        failed_result_ids.add(result_id)
                    if supervision.get('reason_code') == 'maintenance_policy_rejected':
                        failed_count += 1
                        failures.append({'action_id': action['action_id'], 'safe_error': 'Maintenance publication policy rejected.', 'command': []})
                    continue
                current = publication_intents.prepare_publication_intent(db_path, action["action_id"], bounded_command)
                if current["state"] == "missing":
                    if supervision.get('claim'):
                        with closing(sqlite3.connect(db_path)) as conn, conn:
                            supervision_publication.finish(conn, supervision, outcome='rejected', action_id=action['action_id'], now=now)
                    continue
                action = current["action"]
                intent = current["intent"]
                scope = current.get("scope")

                if current["state"] == "published":
                    # A completed intent is replay success.  Normally the action was
                    # committed in the same transaction; this branch repairs an old
                    # partial local update without contacting GitHub again.
                    if action.get("publish_status") == "not_published":
                        result = {
                            "ok": True,
                            "status": "published",
                            "deduplicated": True,
                            "response": {"replayed_intent": True},
                        }
                        with closing(sqlite3.connect(db_path)) as conn, conn:
                            conn.row_factory = sqlite3.Row
                            publication_receipts.mark_published(conn, action, result, now)
                            supervision_publication.finish(conn, supervision, outcome='observing', action_id=action['action_id'], now=now)
                    published_count += 1
                    deduplicated_count += 1
                    continue

                if current["state"] == "attention":
                    if supervision.get('claim'):
                        with closing(sqlite3.connect(db_path)) as conn, conn:
                            supervision_publication.finish(conn, supervision, outcome='unknown', action_id=action['action_id'], now=now)
                    failed_count += 1
                    failures.append(
                        {
                            "action_id": action["action_id"],
                            "safe_error": "publication intent requires operator attention",
                            "command": [],
                        }
                    )
                    failed_result_ids.add(result_id) if result_id else None
                    continue

                if current["state"] in {
                    "scope_missing",
                    "source_sha_missing",
                    "head_owner_missing",
                }:
                    # There is no safe idempotency key without the accepted audit
                    # scope.  Protect the action with one deterministic local
                    # notification and stop before any remote query or write.
                    with closing(sqlite3.connect(db_path)) as conn, conn:
                        conn.row_factory = sqlite3.Row
                        reason_code = (
                            "audited_scope_missing"
                            if current["state"] == "scope_missing"
                            else (
                                "source_sha_unavailable"
                                if current["state"] == "source_sha_missing"
                                else "head_owner_unavailable"
                            )
                        )
                        publication_outcomes.record_failure(conn, action, {"status": "publish_failed", "safe_error": f"Publication requires attention: {reason_code}", "retryable": False}, now)
                        execution_operations.mark_publication_attention(
                            conn,
                            action.get("task_id"),
                            scope,
                            reason_code,
                            now=now,
                        )
                        publication_outcomes.insert_workflow_attention_notification(
                            conn,
                            action,
                            reason_code,
                            now,
                        )
                        supervision_publication.finish(conn, supervision, outcome='rejected', action_id=action['action_id'], now=now, reason=reason_code)
                    failed_count += 1
                    failures.append(
                        {
                            "action_id": action["action_id"],
                            "safe_error": (
                                "publication reconciliation: audited_scope_missing"
                                if current["state"] == "scope_missing"
                                else (
                                    "publication reconciliation: source_sha_unavailable"
                                    if current["state"] == "source_sha_missing"
                                    else "publication reconciliation: head_owner_unavailable"
                                )
                            ),
                            "command": [],
                        }
                    )
                    if result_id:
                        failed_result_ids.add(result_id)
                    continue

                if current["state"] == "reconcile":
                    reconcile_scope = dict(scope or {})
                    evidence = publication_values.decode_json(intent.get("remote_evidence_json"))
                    if evidence.get("source_sha") and not reconcile_scope.get("result_head_sha"):
                        reconcile_scope["result_head_sha"] = evidence["source_sha"]
                    if evidence.get("head_owner"):
                        reconcile_scope["head_owner"] = evidence["head_owner"]
                    reconciliation = publication_reconcile.classify_remote_state(
                        action,
                        reconcile_scope if scope is not None else None,
                        bounded_command,
                    )
                    if reconciliation["status"] == "confirmed_effect":
                        result = {
                            "ok": True,
                            "status": "published",
                            "deduplicated": True,
                            "external_id": reconciliation["evidence"].get("external_id", ""),
                            "target_url": reconciliation["evidence"].get("target_url", ""),
                            "response": {"reconciliation": reconciliation["evidence"]},
                        }
                        with closing(sqlite3.connect(db_path)) as conn, conn:
                            conn.row_factory = sqlite3.Row
                            latest = intent_facts.load_intent(conn, action["action_id"])
                            publication_receipts.mark_published(conn, action, result, now, intent=latest)
                            publication_outcomes.record_remediation_publish_outcome(conn, action, result, now)
                            supervision_publication.finish(conn, supervision, outcome='observing', action_id=action['action_id'], now=now)
                        published_count += 1
                        deduplicated_count += 1
                        continue
                    if reconciliation["status"] == "unknown":
                        current = {**current, "state": "attention"}
                    if reconciliation["status"] == "confirmed_absent" and scope is not None:
                        with closing(sqlite3.connect(db_path)) as conn, conn:
                            conn.row_factory = sqlite3.Row
                            terminal_task = conn.execute("SELECT lifecycle FROM tasks WHERE task_id=?", (action['task_id'],)).fetchone()
                            if terminal_task and terminal_task[0] in {'canceled', 'failed', 'ignored'}:
                                latest = intent_facts.load_intent(conn, action['action_id'])
                                execution_store.transition_publication_intent(conn, latest['publication_id'], 'confirmed_absent', now,
                                    remote_evidence={**reconciliation.get('evidence', {}), 'status': 'confirmed_absent'})
                                publication_outcomes.mark_skipped(conn, action, {'status': 'source_closed', 'safe_error': 'Stopped execution; remote effect confirmed absent.'}, now)
                                supervision_publication.finish(conn, supervision, outcome='failed', action_id=action['action_id'], now=now,
                                    reason='request_no_longer_active')
                                skipped_count += 1
                                current = {**current, 'state': 'retired'}
                            elif not execution_operations.publication_scope_is_current(
                                conn, action.get("task_id"), scope
                            ):
                                reconciliation = {
                                    "status": "unknown",
                                    "reason_code": "publication_scope_stale",
                                    "evidence": {},
                                }
                            else:
                                latest = intent_facts.load_intent(conn, action["action_id"])
                                persisted = publication_values.decode_json(latest.get("remote_evidence_json"))
                                source_sha = persisted.get("source_sha") or current.get(
                                    "source_sha"
                                )
                                head_owner = persisted.get("head_owner") or current.get(
                                    "head_owner"
                                )
                                absent_evidence = {**reconciliation.get('evidence', {}), 'status': 'confirmed_absent'}
                                if source_sha:
                                    absent_evidence["source_sha"] = source_sha
                                if head_owner:
                                    absent_evidence["head_owner"] = head_owner
                                if supervision.get('reconcile_only'):
                                    absent_evidence['supervision_retry_required'] = True
                                execution_store.transition_publication_intent(
                                    conn,
                                    latest["publication_id"],
                                    "confirmed_absent",
                                    now,
                                    remote_evidence=absent_evidence,
                                )
                                if supervision.get('reconcile_only'):
                                    claim = supervision.get('claim') or {}
                                    outcome = 'failed' if claim.get('action_kind') == 'publication_retry' else 'observing'
                                    supervision_publication.finish(conn, supervision, outcome=outcome, action_id=action['action_id'], now=now,
                                        reason='remote_effect_absent')
                                    if result_id:
                                        failed_result_ids.add(result_id)
                                    continue
                                retry_evidence = {"retry_after_absence": True}
                                if source_sha:
                                    retry_evidence["source_sha"] = source_sha
                                if head_owner:
                                    retry_evidence["head_owner"] = head_owner
                                execution_store.transition_publication_intent(
                                    conn,
                                    latest["publication_id"],
                                    "executing",
                                    now,
                                    remote_evidence=retry_evidence,
                                )
                        if current.get('state') == 'retired':
                            continue
                        if reconciliation["status"] == "confirmed_absent":
                            current = {**current, "state": "ready", "intent": latest}
                        else:
                            current = {**current, "state": "attention"}
                    if current["state"] == "attention":
                        with closing(sqlite3.connect(db_path)) as conn, conn:
                            conn.row_factory = sqlite3.Row
                            latest = intent_facts.load_intent(conn, action["action_id"])
                            publication_outcomes.mark_attention(conn, action, latest, scope, reconciliation, now)
                            supervision_publication.finish(conn, supervision, outcome='unknown', action_id=action['action_id'], now=now)
                        failed_count += 1
                        failures.append(
                            {
                                "action_id": action["action_id"],
                                "safe_error": f"publication reconciliation: {reconciliation.get('reason_code', 'unknown')}",
                                "command": [],
                            }
                        )
                        if result_id:
                            failed_result_ids.add(result_id)
                        continue

                if current.get("state") == "ready" and scope is None:
                    reconciliation = {
                        "status": "unknown",
                        "reason_code": "audited_scope_missing",
                        "evidence": {},
                    }
                    with closing(sqlite3.connect(db_path)) as conn, conn:
                        conn.row_factory = sqlite3.Row
                        latest = intent_facts.load_intent(conn, action["action_id"])
                        publication_outcomes.mark_attention(conn, action, latest, scope, reconciliation, now)
                        supervision_publication.finish(conn, supervision, outcome='rejected', action_id=action['action_id'], now=now)
                    failed_count += 1
                    failures.append(
                        {
                            "action_id": action["action_id"],
                            "safe_error": "publication reconciliation: audited_scope_missing",
                            "command": [],
                        }
                    )
                    if result_id:
                        failed_result_ids.add(result_id)
                    continue

                # Charge the durable budget before remote I/O, including a process
                # crash that never reaches failure recording.
                with closing(sqlite3.connect(db_path)) as conn, conn:
                    conn.row_factory = sqlite3.Row
                    fresh = conn.execute('SELECT metadata_json FROM github_actions WHERE action_id=?',(action['action_id'],)).fetchone()
                    before_charge = fresh[0]
                    metadata = publication_values.decode_json(before_charge)
                    prior = metadata.get('publish') or {}
                    first = prior.get('first_failed_at') or now
                    if publication_queries.retry_budget_exhausted(prior, now):
                        publication_outcomes.record_failure(conn, action, {'safe_error':'Automatic publication retry budget exhausted. Inspect and retry explicitly.', 'retryable':False}, now)
                        supervision_publication.finish(conn, supervision, outcome='rejected', action_id=action['action_id'], now=now,
                            reason='native_budget_exhausted')
                        failed_count += 1
                        if result_id:
                            failed_result_ids.add(result_id)
                        continue
                    metadata['publish'] = {**prior, 'attempt_count':int(prior.get('attempt_count',0))+1,
                        'first_failed_at':first, 'attempt_charged':True, 'last_attempt_at':now}
                    conn.execute('UPDATE github_actions SET metadata_json=? WHERE action_id=?',(publication_values.encode_json(metadata), action['action_id']))
                    action = {**action,'metadata_json':publication_values.encode_json(metadata)}
                # The intent is committed as executing before this call.  No SQLite
                # transaction spans the remote effect, so a crash leaves an executing
                # intent for the next publisher pass to reconcile.
                with closing(sqlite3.connect(db_path)) as conn:
                    conn.row_factory = sqlite3.Row
                    remote_effect_started = True
                    guard_kwargs = {}
                    if supervision.get('guard_required') or supervision.get('reconcile_only'):
                        guard_kwargs['recovery_guard'] = lambda: supervision_publication.guard(
                            db_path, action, supervision, config, publication_values.now())
                    result = publication_effects.publish_action(
                        action,
                        bounded_command,
                        conn=conn,
                        source_sha=current.get("source_sha"),
                        trusted_head_owner=current.get("head_owner"),
                        **guard_kwargs,
                    )
                now = publication_values.now()
                if result['status'] == 'supervision_fence_rejected':
                    with closing(sqlite3.connect(db_path)) as conn, conn:
                        conn.row_factory = sqlite3.Row
                        latest = intent_facts.load_intent(conn, action['action_id'])
                        # The final guard refused the command before a mutation. Preserve
                        # the immutable intent and require a fresh typed claim for a retry.
                        if latest and latest['status'] == 'executing':
                            evidence = {**publication_values.decode_json(latest.get('remote_evidence_json')),
                                        'status': 'confirmed_absent', 'reason_code': 'supervision_fence_rejected'}
                            if supervision.get('claim'):
                                evidence['supervision_retry_required'] = True
                            execution_store.transition_publication_intent(conn, latest['publication_id'], 'confirmed_absent', now,
                                remote_evidence=evidence)
                        conn.execute('UPDATE github_actions SET metadata_json=? WHERE action_id=?', (before_charge, action['action_id']))
                        supervision_publication.finish(conn, supervision, outcome='rejected', action_id=action['action_id'], now=now,
                            reason='supervision_fence_rejected')
                    if result_id:
                        failed_result_ids.add(result_id)
                    continue
                if result["status"] == "published":
                    with closing(sqlite3.connect(db_path)) as conn, conn:
                        conn.row_factory = sqlite3.Row
                        latest = intent_facts.load_intent(conn, action["action_id"])
                        publication_receipts.mark_published(conn, action, result, now, intent=latest)
                        publication_outcomes.record_remediation_publish_outcome(conn, action, result, now)
                        supervision_publication.finish(conn, supervision, outcome='observing', action_id=action['action_id'], now=now)
                    published_count += 1
                    if result.get("deduplicated"):
                        deduplicated_count += 1
                    continue
                if result["status"] in {"unsupported_action", "superseded"}:
                    with closing(sqlite3.connect(db_path)) as conn, conn:
                        conn.row_factory = sqlite3.Row
                        publication_outcomes.record_remediation_publish_outcome(conn, action, result, now)
                        publication_outcomes.mark_skipped(conn, action, result, now)
                        supervision_publication.finish(conn, supervision, outcome='failed', action_id=action['action_id'], now=now,
                            reason=result['status'])
                        skipped_count += 1
                        if result["status"] == "superseded" and result_id:
                            skipped_count += publication_outcomes.skip_pending_result_actions(
                                conn,
                                result_id=result_id,
                                excluding_action_id=action["action_id"],
                                result={
                                    "ok": True,
                                    "status": "superseded",
                                    "safe_error": (
                                        "skipped because the preceding push was superseded"
                                    ),
                                },
                                now=now,
                            )
                        scope = publication_scope.trusted_action_scope(conn, action) or {}
                        if (result['status'] == 'superseded' and result_id
                                and not scope.get('review_round_id') and not scope.get('vote_round_id')
                                and scope.get('task_kind') not in {'ci_remediation', 'merge_conflict_remediation'}):
                            from robert_agent.work.application import lifecycle as work_application_lifecycle
                            work_application_lifecycle.finalize_superseded_result(conn, result_id, now)
                    continue
                # A non-zero/raised remote command is ambiguous.  Keep the intent in
                # executing and let the next pass reconcile before any retry.
                with closing(sqlite3.connect(db_path)) as conn, conn:
                    conn.row_factory = sqlite3.Row
                    publication_outcomes.record_remediation_publish_outcome(conn, action, result, now)
                    publication_outcomes.record_failure(conn, action, result, now,
                                    reconcile_required=result.get("retryable") is not False)
                    supervision_publication.finish(conn, supervision, outcome='unknown', action_id=action['action_id'], now=now)
                if result_id:
                    failed_result_ids.add(result_id)
                    action_scope = scope
                    if (
                        action["action_type"] == "push_existing_pr"
                        and isinstance(action_scope, dict)
                        and action_scope.get("task_kind")
                        in {"ci_remediation", "merge_conflict_remediation"}
                    ):
                        failed_remediation_result_ids.add(result_id)
                failed_count += 1
                failures.append(
                    {
                        "action_id": action["action_id"],
                        "safe_error": result.get("safe_error", ""),
                        "command": result.get("command", []),
                    }
                )
            except subprocess.TimeoutExpired:
                result = {"ok": False, "status": "publish_failed",
                          "safe_error": "Publication command timed out after 60 seconds. Check connectivity and retry publication."}
                with closing(sqlite3.connect(db_path)) as conn, conn:
                    conn.row_factory = sqlite3.Row
                    now = publication_values.now()
                    publication_outcomes.record_failure(conn, action, result, now, reconcile_required=remote_effect_started)
                    supervision_publication.finish(conn, supervision, outcome='unknown' if remote_effect_started or supervision.get('reconcile_only') else 'rejected',
                        action_id=action['action_id'], now=now)
                failed_count += 1
                failures.append({"action_id": action["action_id"], "safe_error": result["safe_error"], "command": []})
                if action["result_id"]:
                    failed_result_ids.add(action["result_id"])
            except OSError as exc:
                result = {'status':'publish_failed','safe_error':type(exc).__name__, 'retryable': not isinstance(exc, FileNotFoundError)}
                with closing(sqlite3.connect(db_path)) as conn, conn:
                    conn.row_factory = sqlite3.Row
                    now = publication_values.now()
                    publication_outcomes.record_failure(conn, action, result, now, reconcile_required=remote_effect_started)
                    supervision_publication.finish(conn, supervision, outcome='unknown' if remote_effect_started or supervision.get('reconcile_only') else 'rejected',
                        action_id=action['action_id'], now=now)
                failed_count += 1
                failures.append({'action_id':action['action_id'],'safe_error':result['safe_error'],'command':[]})
                if action['result_id']:
                    failed_result_ids.add(action['result_id'])
    return {
        "ok": failed_count == 0,
        "status": "published" if failed_count == 0 else "publish_failed",
        "pending_count": len(actions),
        "published_count": published_count,
        "deferred_count": deferred_count,
        "deduplicated_count": deduplicated_count,
        "skipped_count": skipped_count,
        "failed_count": failed_count,
        "failures": failures,
    }



def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--config", help="Current Robert configuration for supervised or maintenance publication.")
    args = parser.parse_args(argv)
    config = supervision_publication.fresh_config({'config_path': args.config}, args.db) if args.config else None
    if args.config and not config:
        return emit({'ok': False, 'status': 'failed_config', 'safe_error': 'Configuration is invalid or names a different database.'}, 3)
    result = publish_ready_actions(args.db, dry_run=args.dry_run, limit=args.limit, config=config)
    return emit(result, 0 if result.get("ok") else 3)



if __name__ == "__main__":
    raise SystemExit(main())
