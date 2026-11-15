"""application delivery publish responsibilities."""

import argparse
import os
import subprocess
import time
from contextlib import closing

from robert_agent.adapters.github import budget as github_budget
from robert_agent.adapters.github import read as github_read
from robert_agent.delivery import intents as intent_facts
from robert_agent.delivery import queries as publication_queries
from robert_agent.delivery import values as publication_values
from robert_agent.delivery.application import effects as publication_effects
from robert_agent.delivery.application import intents as publication_intents
from robert_agent.delivery.application import outcomes as publication_outcomes
from robert_agent.delivery.application import receipts as publication_receipts
from robert_agent.delivery.application import reconcile as publication_reconcile
from robert_agent.delivery.application import scope as publication_scope
from robert_agent.delivery.application import (
    source_feedback_publication,
    source_github_publication,
    source_publication,
    source_publication_queue,
)
from robert_agent.delivery.lock import task_publication_lock
from robert_agent.execution import operations as execution_operations
from robert_agent.execution import source_controller
from robert_agent.foundation.commands import ControlDeadlineExceeded, emit
from robert_agent.foundation.commands import run_command as control_command
from robert_agent.foundation.contracts.delivery import (
    GitHubActionType,
    PublicationStatus,
    PublishStatus,
    SourceControllerAction,
)
from robert_agent.foundation.contracts.supervision import RecoveryActionKind
from robert_agent.foundation.contracts.work import REMEDIATION_TASK_KINDS
from robert_agent.persistence import database as db_database
from robert_agent.persistence import errors as db_errors
from robert_agent.persistence import execution_control as execution_store
from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import dispatch_requests
from robert_agent.persistence.queries import github_actions as db_read_github_actions
from robert_agent.persistence.queries import tasks as db_read_tasks
from robert_agent.persistence.stores import github_actions as db_write_github_actions
from robert_agent.persistence.stores import wakeups as db_write_wakeups
from robert_agent.supervision.application import publication as supervision_publication

PUBLISH_COMMAND_TIMEOUT_SECONDS = 60


def github_publication_budget(db_path, config, *, host="github.com", stop=None, run_command=subprocess.run):
    """Return a per-invocation configured account budget probe for this host."""
    accounts = {}

    def command(args, **kwargs):
        if stop is not None and stop.is_set():
            raise ControlDeadlineExceeded(args[:2], 0)
        deadline = getattr(stop, "deadline", float("inf"))
        cap = max(0.001, min(20, deadline - time.monotonic()))
        env = {key: value for key, value in os.environ.items() if key not in {"GH_HOST", "GH_REPO"}}
        env.update(GH_HOST=host, GH_PROMPT_DISABLED="1")
        cap = min(cap, kwargs.pop("timeout", None) or cap)
        return control_command(args, runner=run_command, timeout=cap, env=env, **kwargs)

    def check(account):
        if account not in accounts:
            session = github_read.GitHubReadSession(db_path, account=account, host=host, runner=command)
            try:
                accounts[account] = (
                    None
                    if github_budget.budget(session, config)
                    else {
                        "ok": False,
                        "status": "core_rate_limit_floor",
                        "failure_scope": "repository",
                        "retry_after_seconds": session.poll_interval_seconds or 60,
                    }
                )
            except ControlDeadlineExceeded:
                raise
            except (subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError, AttributeError):
                accounts[account] = {"ok": False, "status": "rate_limit_check_failed", "failure_scope": "repository"}
        return accounts[account]

    return check


def publish_ready_actions(
    db_path,
    dry_run=False,
    limit=20,
    run_command=subprocess.run,
    repo_id=None,
    task_ids=None,
    *,
    action_ids=None,
    config=None,
    provider=None,
    source_owner=None,
    tapd_client_factory=None,
    tapd_observation_provider=None,
    github_feedback_runner=None,
    github_discussion_runner=None,
    tapd_identity_provider=None,
    stop=None,
    source_reconcile_only=False,
    recover_sources=True,
    github_budget_check=None,
    include_legacy=True,
):
    """Service one age-ordered effect budget and a separate bounded local recovery page."""
    if provider not in (None, "github", "tapd"):
        raise ValueError("Unsupported publication provider")

    def bounded_command(*args, **kwargs):
        kwargs["timeout"] = min(
            kwargs.get("timeout") or PUBLISH_COMMAND_TIMEOUT_SECONDS, PUBLISH_COMMAND_TIMEOUT_SECONDS
        )
        return control_command(*args, runner=run_command, **kwargs)

    with closing(db_database.connect(db_path)) as conn:
        from robert_agent.execution.application import cutover as result_cutover

        cutover = result_cutover.inspect(conn, db_path) if dry_run else result_cutover.prepare(db_path)
        if not cutover["ok"]:
            return {
                **cutover,
                "pending_count": 0,
                "published_count": 0,
                "deduplicated_count": 0,
                "skipped_count": 0,
                "failed_count": 0,
            }
        scope = {"repo_id": repo_id, "task_ids": task_ids, "action_ids": action_ids}
        actions = (
            publication_queries.ready_actions(conn, limit, **scope) if provider != "tapd" and include_legacy else []
        )
        native, completions = source_publication_queue.source_publication_page(
            conn,
            config,
            source_owner=source_owner,
            limit=limit,
            **scope,
            tapd_observation_provider=tapd_observation_provider,
            tapd_identity_provider=tapd_identity_provider,
            provider=provider,
            reconcile_only=source_reconcile_only,
            recover=recover_sources,
        )
    if native:
        native_rows = [dict(row) for row in native]
        actions = sorted([*actions, *native_rows], key=lambda row: (row["created_at"], row["action_id"]))
        if limit >= 0:
            # A full native page may have older unseen entries. Execute only
            # the provable globally ordered prefix and resume on the next pass.
            effect_limit = min(limit, len(native)) if len(native) == source_publication_queue.MAX_NATIVE_PAGE else limit
            actions = actions[:effect_limit]
    native_counts = (
        {
            "native_published_count": 0,
            "native_deduplicated_count": 0,
            "native_deferred_count": 0,
            "native_unknown_count": 0,
            "native_invalid_count": 0,
            "native_completed_count": 0,
            "native_completion_held_count": 0,
        }
        if native or completions or source_owner is not None or provider == "tapd"
        else {}
    )
    if dry_run:
        return {
            "ok": True,
            "status": "dry_run",
            "pending_count": len(actions),
            "published_count": 0,
            "deduplicated_count": 0,
            "skipped_count": 0,
            "failed_count": 0,
            **native_counts,
        }
    deferred_count = 0
    published_count = 0
    deduplicated_count = 0
    skipped_count = 0
    failed_count = 0
    failures = []
    budget_failure = None
    budget_refused = 0
    failed_result_ids = set()
    failed_remediation_result_ids = set()
    busy_task_ids = set()
    for candidate in completions:
        if stop is not None and stop.is_set():
            break
        outcome = source_publication_queue.recover_source_completion(
            db_path,
            config,
            candidate,
            source_owner=source_owner,
            tapd_client_factory=tapd_client_factory,
            tapd_observation_provider=tapd_observation_provider,
            github_discussion_runner=github_discussion_runner,
            stop=stop,
            reconcile_only=source_reconcile_only,
            **scope,
        )
        key = "native_completed_count" if outcome.status == "completed" else "native_completion_held_count"
        if outcome.status not in {"already_completed", "lineage_recovered"}:
            native_counts[key] += 1
    for action in actions:
        if stop is not None and stop.is_set():
            break
        if root_id := action.get("native_source_result_id"):
            native_options = {
                "source_result_id": root_id,
                "action_id": action["action_id"],
                "owner": source_owner,
                "observation_provider": tapd_observation_provider,
                "stop": stop,
                "respect_backoff": True,
            }
            if action["provider"] == "github":
                if github_budget_check is not None:
                    repo_accounts = [
                        repo.get("github_account")
                        for repo in config.get("repos", ())
                        if repo.get("full_name") == action["native_repository"]
                    ]
                    refusal = (
                        github_budget_check(repo_accounts[0])
                        if len(repo_accounts) == 1 and isinstance(repo_accounts[0], str) and repo_accounts[0]
                        else {"ok": False, "status": "rate_limit_check_failed", "failure_scope": "repository"}
                    )
                    if refusal:
                        budget_failure = budget_failure or refusal
                        budget_refused += 1
                        continue
                client = {"tapd_client_factory": tapd_client_factory} if tapd_client_factory is not None else {}
                discussion = (
                    {"github_discussion_runner": github_discussion_runner}
                    if github_discussion_runner is not None
                    else {}
                )
                if action["action_type"] == SourceControllerAction.GITHUB_FEEDBACK:
                    outcome = source_feedback_publication.publish_feedback_action(
                        db_path,
                        config,
                        runner=github_feedback_runner,
                        reconcile_only=source_reconcile_only,
                        **native_options,
                        **client,
                        **discussion,
                    )
                else:
                    outcome = source_github_publication.publish_github_action(
                        db_path,
                        config,
                        run_command=run_command,
                        reconcile_only=source_reconcile_only,
                        **native_options,
                        **client,
                        **discussion,
                    )
                completion = None
            else:
                client = {"client_factory": tapd_client_factory} if tapd_client_factory is not None else {}
                github_reads = {}
                if github_discussion_runner is not None:
                    github_reads["github_discussion_runner"] = github_discussion_runner
                if run_command is not subprocess.run:
                    github_reads["github_runner"] = run_command
                outcome = source_publication.publish_tapd_action(
                    db_path,
                    config,
                    identity_provider=None if source_reconcile_only else tapd_identity_provider,
                    **native_options,
                    **client,
                    **github_reads,
                )
                completion = outcome.completion
            native_counts[f"native_{outcome.status}_count"] += 1
            if outcome.status == "published":
                published_count += 1
            elif outcome.status == "deduplicated":
                published_count += 1
                deduplicated_count += 1
            elif outcome.status == "deferred":
                deferred_count += 1
            else:
                failed_count += 1
                failures.append({"action_id": action["action_id"], "safe_error": outcome.reason, "command": []})
            if completion is not None:
                if completion.status == "completed":
                    native_counts["native_completed_count"] += 1
                elif completion.status in {"held", "ineligible"}:
                    native_counts["native_completion_held_count"] += 1
            continue
        if action["task_id"] in busy_task_ids:
            continue
        with task_publication_lock(db_path, action["task_id"]) as acquired:
            if not acquired:
                busy_task_ids.add(action["task_id"])
                continue
            # A failed remediation push retires its following comment even
            # when that failure has already made the execution non-runnable.
            if action["result_id"] in failed_remediation_result_ids:
                with closing(db_database.connect(db_path)) as conn, conn:
                    latest = db_records.first(
                        db_read_github_actions.find_record_by_action_id(conn, action["action_id"])
                    )
                    if latest and latest["publish_status"] not in {PublishStatus.PUBLISHED, PublishStatus.SKIPPED}:
                        publication_outcomes.mark_skipped(
                            conn,
                            dict(latest),
                            {
                                "ok": False,
                                "status": "publish_failed",
                                "safe_error": "skipped because the preceding remediation push failed",
                            },
                            publication_values.now(),
                        )
                        skipped_count += 1
                continue
            # The selection page predates this lock. Another publisher may
            # already have completed the action or changed its eligibility.
            with closing(db_database.connect(db_path)) as conn:
                ready = publication_queries.ready_actions(conn, 1, action_ids=[action["action_id"]])
            if not ready:
                continue
            action = ready[0]
            with closing(db_database.connect(db_path)) as auth_conn:
                dispatch_bound = dispatch_requests.for_task(auth_conn, action["task_id"]) is not None
            action_config = {} if config is None and dispatch_bound else config
            # Consume the request before this pass, including reconciliation
            # failures, so a failed attempt never leaves retry disabled.
            with closing(db_database.connect(db_path)) as retry_conn, retry_conn:
                db_write_wakeups.update_for_publish_ready_actions(
                    retry_conn, publication_values.now(), action["task_id"]
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
                            "safe_error": ("skipped because the preceding remediation push failed"),
                        }
                        with closing(db_database.connect(db_path)) as conn, conn:
                            publication_outcomes.mark_skipped(conn, action, result, now)
                        skipped_count += 1
                    continue
                with closing(db_database.connect(db_path)) as conn, conn:
                    prior = publication_values.decode_json(action["metadata_json"]).get("publish") or {}
                    existing_intent = intent_facts.load_intent(conn, action["action_id"])
                    # Unknown effects can still be reconciled after the write budget is exhausted.
                    if (
                        not existing_intent
                        or existing_intent["status"] not in {PublicationStatus.EXECUTING, PublicationStatus.PUBLISHED}
                    ) and publication_queries.retry_budget_exhausted(prior, now):
                        publication_outcomes.record_failure(
                            conn,
                            action,
                            {
                                "safe_error": "Automatic publication retry budget exhausted. Inspect and retry explicitly.",
                                "retryable": False,
                            },
                            now,
                        )
                        failed_count += 1
                        if result_id:
                            failed_result_ids.add(result_id)
                        continue
                try:
                    supervision = supervision_publication.begin(db_path, action, action_config, now)
                except db_errors.Error:
                    # No remote effect or publication attempt has started. Leave
                    # this task's ordered actions pending and try other tasks.
                    deferred_count += 1
                    busy_task_ids.add(action["task_id"])
                    continue
                except ValueError:
                    # Invalid task-local recovery state cannot authorize an effect.
                    result = {
                        "ok": False,
                        "status": "publish_failed",
                        "retryable": False,
                        "safe_error": "Invalid publication recovery state; inspect the task before retrying.",
                    }
                    with closing(db_database.connect(db_path)) as conn, conn:
                        publication_outcomes.record_failure(conn, action, result, now)
                    failed_count += 1
                    busy_task_ids.add(action["task_id"])
                    if result_id:
                        failed_result_ids.add(result_id)
                    failures.append(
                        {"action_id": action["action_id"], "safe_error": result["safe_error"], "command": []}
                    )
                    continue
                if supervision["status"] == "blocked":
                    if result_id:
                        failed_result_ids.add(result_id)
                    if supervision.get("reason_code") == "maintenance_policy_rejected":
                        failed_count += 1
                        failures.append(
                            {
                                "action_id": action["action_id"],
                                "safe_error": "Maintenance publication policy rejected.",
                                "command": [],
                            }
                        )
                    continue
                current = publication_intents.prepare_publication_intent(db_path, action["action_id"], bounded_command)
                if current["state"] == "missing":
                    if supervision.get("claim"):
                        with closing(db_database.connect(db_path)) as conn, conn:
                            supervision_publication.finish(
                                conn, supervision, outcome="rejected", action_id=action["action_id"], now=now
                            )
                    continue
                action = current["action"]
                intent = current["intent"]
                scope = current.get("scope")

                if current["state"] == "published":
                    # A completed intent is replay success.  Normally the action was
                    # committed in the same transaction; this branch repairs an old
                    # partial local update without contacting GitHub again.
                    if action.get("publish_status") == PublishStatus.NOT_PUBLISHED:
                        result = {
                            "ok": True,
                            "status": "published",
                            "deduplicated": True,
                            "response": {"replayed_intent": True},
                        }
                        with closing(db_database.connect(db_path)) as conn, conn:
                            publication_receipts.mark_published(conn, action, result, now)
                            supervision_publication.finish(
                                conn, supervision, outcome="observing", action_id=action["action_id"], now=now
                            )
                    published_count += 1
                    deduplicated_count += 1
                    continue

                if current["state"] == "attention":
                    if supervision.get("claim"):
                        with closing(db_database.connect(db_path)) as conn, conn:
                            supervision_publication.finish(
                                conn, supervision, outcome="unknown", action_id=action["action_id"], now=now
                            )
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
                    with closing(db_database.connect(db_path)) as conn, conn:
                        reason_code = (
                            "audited_scope_missing"
                            if current["state"] == "scope_missing"
                            else (
                                "source_sha_unavailable"
                                if current["state"] == "source_sha_missing"
                                else "head_owner_unavailable"
                            )
                        )
                        publication_outcomes.record_failure(
                            conn,
                            action,
                            {
                                "status": "publish_failed",
                                "safe_error": f"Publication requires attention: {reason_code}",
                                "retryable": False,
                            },
                            now,
                        )
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
                        supervision_publication.finish(
                            conn,
                            supervision,
                            outcome="rejected",
                            action_id=action["action_id"],
                            now=now,
                            reason=reason_code,
                        )
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
                    if action["action_type"] in {
                        GitHubActionType.COMMENT,
                        GitHubActionType.PUSH_EXISTING_PR,
                    } and not reconcile_scope.get("github_account"):
                        with closing(db_database.connect(db_path)) as reader:
                            reconcile_scope["github_account"] = publication_scope.comment_publisher(
                                reader, action, reconcile_scope
                            )
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
                        with closing(db_database.connect(db_path)) as conn, conn:
                            latest = intent_facts.load_intent(conn, action["action_id"])
                            publication_receipts.mark_published(conn, action, result, now, intent=latest)
                            publication_outcomes.record_remediation_publish_outcome(conn, action, result, now)
                            supervision_publication.finish(
                                conn, supervision, outcome="observing", action_id=action["action_id"], now=now
                            )
                        published_count += 1
                        deduplicated_count += 1
                        continue
                    if reconciliation["status"] == "unknown":
                        current = {**current, "state": "attention"}
                    if reconciliation["status"] == "confirmed_absent" and scope is not None:
                        with closing(db_database.connect(db_path)) as conn, conn:
                            terminal_task = db_records.first(
                                db_read_tasks.find_lifecycle_by_task_id(conn, action["task_id"])
                            )
                            if terminal_task and terminal_task[0] in {"canceled", "failed", "ignored"}:
                                latest = intent_facts.load_intent(conn, action["action_id"])
                                execution_store.transition_publication_intent(
                                    conn,
                                    latest["publication_id"],
                                    "confirmed_absent",
                                    now,
                                    remote_evidence={
                                        **reconciliation.get("evidence", {}),
                                        "status": "confirmed_absent",
                                    },
                                )
                                publication_outcomes.mark_skipped(
                                    conn,
                                    action,
                                    {
                                        "status": "source_closed",
                                        "safe_error": "Stopped execution; remote effect confirmed absent.",
                                    },
                                    now,
                                )
                                supervision_publication.finish(
                                    conn,
                                    supervision,
                                    outcome="failed",
                                    action_id=action["action_id"],
                                    now=now,
                                    reason="request_no_longer_active",
                                )
                                skipped_count += 1
                                current = {**current, "state": "retired"}
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
                                source_sha = persisted.get("source_sha") or current.get("source_sha")
                                head_owner = persisted.get("head_owner") or current.get("head_owner")
                                absent_evidence = {**reconciliation.get("evidence", {}), "status": "confirmed_absent"}
                                if source_sha:
                                    absent_evidence["source_sha"] = source_sha
                                if head_owner:
                                    absent_evidence["head_owner"] = head_owner
                                if supervision.get("reconcile_only"):
                                    absent_evidence["supervision_retry_required"] = True
                                execution_store.transition_publication_intent(
                                    conn,
                                    latest["publication_id"],
                                    "confirmed_absent",
                                    now,
                                    remote_evidence=absent_evidence,
                                )
                                if supervision.get("reconcile_only"):
                                    claim = supervision.get("claim") or {}
                                    outcome = (
                                        "failed"
                                        if claim.get("action_kind") == RecoveryActionKind.PUBLICATION_RETRY.value
                                        else "observing"
                                    )
                                    supervision_publication.finish(
                                        conn,
                                        supervision,
                                        outcome=outcome,
                                        action_id=action["action_id"],
                                        now=now,
                                        reason="remote_effect_absent",
                                    )
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
                        if current.get("state") == "retired":
                            continue
                        if reconciliation["status"] == "confirmed_absent":
                            current = {**current, "state": "ready", "intent": latest}
                        else:
                            current = {**current, "state": "attention"}
                    if current["state"] == "attention":
                        with closing(db_database.connect(db_path)) as conn, conn:
                            latest = intent_facts.load_intent(conn, action["action_id"])
                            publication_outcomes.mark_attention(conn, action, latest, scope, reconciliation, now)
                            supervision_publication.finish(
                                conn, supervision, outcome="unknown", action_id=action["action_id"], now=now
                            )
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
                    with closing(db_database.connect(db_path)) as conn, conn:
                        latest = intent_facts.load_intent(conn, action["action_id"])
                        publication_outcomes.mark_attention(conn, action, latest, scope, reconciliation, now)
                        supervision_publication.finish(
                            conn, supervision, outcome="rejected", action_id=action["action_id"], now=now
                        )
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
                with closing(db_database.connect(db_path)) as conn, conn:
                    fresh = db_records.first(
                        db_read_github_actions.find_metadata_json_by_action_id(conn, action["action_id"])
                    )
                    before_charge = fresh[0]
                    metadata = publication_values.decode_json(before_charge)
                    prior = metadata.get("publish") or {}
                    first = prior.get("first_failed_at") or now
                    if publication_queries.retry_budget_exhausted(prior, now):
                        publication_outcomes.record_failure(
                            conn,
                            action,
                            {
                                "safe_error": "Automatic publication retry budget exhausted. Inspect and retry explicitly.",
                                "retryable": False,
                            },
                            now,
                        )
                        supervision_publication.finish(
                            conn,
                            supervision,
                            outcome="rejected",
                            action_id=action["action_id"],
                            now=now,
                            reason="native_budget_exhausted",
                        )
                        failed_count += 1
                        if result_id:
                            failed_result_ids.add(result_id)
                        continue
                    metadata["publish"] = {
                        **prior,
                        "attempt_count": int(prior.get("attempt_count", 0)) + 1,
                        "first_failed_at": first,
                        "attempt_charged": True,
                        "last_attempt_at": now,
                    }
                    db_write_github_actions.set_metadata_json_by_action_id(
                        conn, publication_values.encode_json(metadata), action["action_id"]
                    )
                    action = {**action, "metadata_json": publication_values.encode_json(metadata)}
                # The intent is committed as executing before this call.  No SQLite
                # transaction spans the remote effect, so a crash leaves an executing
                # intent for the next publisher pass to reconcile.
                with closing(db_database.connect(db_path)) as conn:
                    remote_effect_started = True
                    guard_kwargs = {}
                    if (
                        action_config is not None
                        or supervision.get("guard_required")
                        or supervision.get("reconcile_only")
                    ):
                        guard_kwargs["recovery_guard"] = lambda: supervision_publication.guard(
                            db_path, action, supervision, action_config, publication_values.now()
                        )
                    result = publication_effects.publish_action(
                        action,
                        bounded_command,
                        conn=conn,
                        source_sha=current.get("source_sha"),
                        trusted_head_owner=current.get("head_owner"),
                        **guard_kwargs,
                    )
                now = publication_values.now()
                if result["status"] == "supervision_fence_rejected":
                    with closing(db_database.connect(db_path)) as conn, conn:
                        latest = intent_facts.load_intent(conn, action["action_id"])
                        # The final guard refused the command before a mutation. Preserve
                        # the immutable intent and require a fresh typed claim for a retry.
                        if latest and latest["status"] == "executing":
                            evidence = {
                                **publication_values.decode_json(latest.get("remote_evidence_json")),
                                "status": "confirmed_absent",
                                "reason_code": "supervision_fence_rejected",
                            }
                            if supervision.get("claim"):
                                evidence["supervision_retry_required"] = True
                            execution_store.transition_publication_intent(
                                conn, latest["publication_id"], "confirmed_absent", now, remote_evidence=evidence
                            )
                        db_write_github_actions.set_metadata_json_by_action_id(conn, before_charge, action["action_id"])
                        supervision_publication.finish(
                            conn,
                            supervision,
                            outcome="rejected",
                            action_id=action["action_id"],
                            now=now,
                            reason="supervision_fence_rejected",
                        )
                    if action_config is not None:
                        with closing(db_database.connect(db_path)) as conn, conn:
                            publication_outcomes.record_failure(conn, action, {**result, "retryable": False}, now)
                        failed_count += 1
                        failures.append(
                            {"action_id": action["action_id"], "safe_error": result["safe_error"], "command": []}
                        )
                    if result_id:
                        failed_result_ids.add(result_id)
                    continue
                if result["status"] == "published":
                    with closing(db_database.connect(db_path)) as conn, conn:
                        latest = intent_facts.load_intent(conn, action["action_id"])
                        publication_receipts.mark_published(conn, action, result, now, intent=latest)
                        publication_outcomes.record_remediation_publish_outcome(conn, action, result, now)
                        supervision_publication.finish(
                            conn, supervision, outcome="observing", action_id=action["action_id"], now=now
                        )
                    published_count += 1
                    if result.get("deduplicated"):
                        deduplicated_count += 1
                    continue
                if result["status"] in {"unsupported_action", "superseded"}:
                    with closing(db_database.connect(db_path)) as conn, conn:
                        publication_outcomes.record_remediation_publish_outcome(conn, action, result, now)
                        publication_outcomes.mark_skipped(conn, action, result, now)
                        supervision_publication.finish(
                            conn,
                            supervision,
                            outcome="failed",
                            action_id=action["action_id"],
                            now=now,
                            reason=result["status"],
                        )
                        skipped_count += 1
                        if result["status"] == "superseded" and result_id:
                            skipped_count += publication_outcomes.skip_pending_result_actions(
                                conn,
                                result_id=result_id,
                                excluding_action_id=action["action_id"],
                                result={
                                    "ok": True,
                                    "status": "superseded",
                                    "safe_error": ("skipped because the preceding push was superseded"),
                                },
                                now=now,
                            )
                        scope = publication_scope.trusted_action_scope(conn, action) or {}
                        if (
                            result["status"] == "superseded"
                            and result_id
                            and not scope.get("review_round_id")
                            and not scope.get("vote_round_id")
                            and scope.get("task_kind") not in REMEDIATION_TASK_KINDS
                        ):
                            from robert_agent.work.application import lifecycle as work_application_lifecycle

                            work_application_lifecycle.finalize_superseded_result(conn, result_id, now)
                    continue
                # A non-zero/raised remote command is ambiguous.  Keep the intent in
                # executing and let the next pass reconcile before any retry.
                with closing(db_database.connect(db_path)) as conn, conn:
                    publication_outcomes.record_remediation_publish_outcome(conn, action, result, now)
                    publication_outcomes.record_failure(
                        conn, action, result, now, reconcile_required=result.get("retryable") is not False
                    )
                    supervision_publication.finish(
                        conn, supervision, outcome="unknown", action_id=action["action_id"], now=now
                    )
                if result_id:
                    failed_result_ids.add(result_id)
                    action_scope = scope
                    if (
                        action["action_type"] == GitHubActionType.PUSH_EXISTING_PR
                        and isinstance(action_scope, dict)
                        and action_scope.get("task_kind") in REMEDIATION_TASK_KINDS
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
            except ControlDeadlineExceeded:
                raise
            except subprocess.TimeoutExpired:
                result = {
                    "ok": False,
                    "status": "publish_failed",
                    "safe_error": "Publication command timed out after 60 seconds. Check connectivity and retry publication.",
                }
                with closing(db_database.connect(db_path)) as conn, conn:
                    now = publication_values.now()
                    publication_outcomes.record_failure(
                        conn, action, result, now, reconcile_required=remote_effect_started
                    )
                    supervision_publication.finish(
                        conn,
                        supervision,
                        outcome="unknown" if remote_effect_started or supervision.get("reconcile_only") else "rejected",
                        action_id=action["action_id"],
                        now=now,
                    )
                failed_count += 1
                failures.append({"action_id": action["action_id"], "safe_error": result["safe_error"], "command": []})
                if action["result_id"]:
                    failed_result_ids.add(action["result_id"])
            except OSError as exc:
                result = {
                    "status": "publish_failed",
                    "safe_error": type(exc).__name__,
                    "retryable": not isinstance(exc, FileNotFoundError),
                }
                with closing(db_database.connect(db_path)) as conn, conn:
                    now = publication_values.now()
                    publication_outcomes.record_failure(
                        conn, action, result, now, reconcile_required=remote_effect_started
                    )
                    supervision_publication.finish(
                        conn,
                        supervision,
                        outcome="unknown" if remote_effect_started or supervision.get("reconcile_only") else "rejected",
                        action_id=action["action_id"],
                        now=now,
                    )
                failed_count += 1
                failures.append({"action_id": action["action_id"], "safe_error": result["safe_error"], "command": []})
                if action["result_id"]:
                    failed_result_ids.add(action["result_id"])
    return {
        "ok": failed_count == 0,
        "status": "published" if failed_count == 0 else "publish_failed",
        "pending_count": len(actions) - budget_refused,
        "published_count": published_count,
        "deferred_count": deferred_count,
        "deduplicated_count": deduplicated_count,
        "skipped_count": skipped_count,
        "failed_count": failed_count,
        "failures": failures,
        **native_counts,
        **(budget_failure or {}),
    }


def main(argv=None, *, tapd_client_factory=None, tapd_observation_provider=None, tapd_identity_provider=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--config", help="Current Robert configuration for supervised or maintenance publication.")
    args = parser.parse_args(argv)
    config = supervision_publication.fresh_config({"config_path": args.config}, args.db) if args.config else None
    if args.config and not config:
        return emit(
            {
                "ok": False,
                "status": "failed_config",
                "safe_error": "Configuration is invalid or names a different database.",
            },
            3,
        )
    native = (config or {}).get("tapd", {}).get("enabled") is True and not args.dry_run
    if native:
        with source_controller.publication(args.db, args.config, seconds=120) as (owner, stop, status):
            if owner is None and status != "skipped_active_daemon":
                result = {"ok": False, "status": status, "pending_count": 0}
            else:
                options = {
                    "config": config,
                    "source_owner": owner,
                    "stop": stop if owner else None,
                    "tapd_client_factory": tapd_client_factory,
                    "tapd_observation_provider": tapd_observation_provider,
                    "tapd_identity_provider": tapd_identity_provider,
                }
                native = publish_ready_actions(args.db, limit=args.limit, provider="tapd", **options) if owner else {}
                remaining = max(0, args.limit - native.get("pending_count", 0))
                if remaining:
                    result = publish_ready_actions(
                        args.db,
                        limit=remaining,
                        provider="github",
                        recover_sources=False,
                        github_budget_check=github_publication_budget(args.db, config, stop=options["stop"]),
                        **options,
                    )
                    for key, count in native.items():
                        if key.endswith("_count") and type(count) is int:
                            result[key] = result.get(key, 0) + count
                    if not native.get("ok", True) and result.get("ok"):
                        result.update(ok=False, status=native["status"])
                    if native.get("failures"):
                        result["failures"] = [*native["failures"], *result.get("failures", [])]
                else:
                    result = native or {"ok": True, "status": "published", "pending_count": 0}
    else:
        result = publish_ready_actions(args.db, dry_run=args.dry_run, limit=args.limit, config=config)
    return emit(result, 0 if result.get("ok") else 3)


if __name__ == "__main__":
    raise SystemExit(main())
