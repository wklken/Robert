"""Bounded, durable follow-up for engineering repairs with an audited delivery."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from uuid import uuid4

from robert_agent.workflows.maintenance import repair
from robert_agent.supervision import rules as supervision_rules
from robert_agent.supervision import store as supervision_store
from robert_agent.delivery.lock import task_publication_lock


_RECHECK = timedelta(minutes=5)
_CLAIM_WINDOW = timedelta(minutes=7)


def _candidate(conn, *, repository, policy_digest, now):
    return conn.execute(
        """SELECT link.scope_id AS incident_id, result.result_id, attempt.worktree_path,
        json_extract(result.metadata_json,'$.audit.action_scope.result_head_sha') AS revision
        FROM supervision_checkpoints link
        JOIN work_items item ON item.work_item_id=json_extract(link.metadata_json,'$.work_item_id')
        JOIN repos repo ON repo.repo_id=item.repo_id
        JOIN worker_results result ON result.result_id=(
            SELECT wr.result_id FROM tasks t JOIN worker_results wr ON wr.task_id=t.task_id
            JOIN attempts a ON a.attempt_id=wr.attempt_id
            WHERE t.workstream_id=item.workstream_id AND wr.output_type='new_pr' AND a.status='completed'
              AND json_extract(wr.metadata_json,'$.audit.status')='accepted'
              AND (json_type(a.metadata_json,'$.dispatch.pid') IS NOT 'integer'
                   OR json_extract(a.metadata_json,'$.process_stopped_at') IS NOT NULL)
            ORDER BY wr.created_at DESC,wr.result_id DESC LIMIT 1)
        JOIN attempts attempt ON attempt.attempt_id=result.attempt_id
        LEFT JOIN supervision_checkpoints followup ON followup.scope_type='repair'
            AND followup.scope_id=link.scope_id AND followup.rule_id='followup'
        WHERE link.scope_type='repair' AND link.rule_id='engineering'
          AND item.created_by='system:supervisor' AND item.canceled_at IS NULL
          AND repo.enabled=1 AND lower(repo.full_name)=?
          AND COALESCE(attempt.worktree_path,'')!=''
          AND (COALESCE(json_extract(followup.metadata_json,'$.status'),'')!='complete'
               OR json_extract(followup.metadata_json,'$.result_id') IS NOT result.result_id
               OR json_extract(followup.metadata_json,'$.revision') IS NOT json_extract(result.metadata_json,'$.audit.action_scope.result_head_sha')
               OR json_extract(followup.metadata_json,'$.policy_digest') IS NOT ?)
          AND (json_extract(followup.metadata_json,'$.next_check_at') IS NULL
               OR julianday(json_extract(followup.metadata_json,'$.next_check_at'))<=julianday(?))
          AND (json_extract(followup.metadata_json,'$.claim_expires_at') IS NULL
               OR julianday(json_extract(followup.metadata_json,'$.claim_expires_at'))<=julianday(?))
        ORDER BY COALESCE(json_extract(followup.metadata_json,'$.next_check_at'),link.updated_at),link.scope_id LIMIT 1""",
        (repository, policy_digest, now.isoformat(), now.isoformat()),
    ).fetchone()


def _claim(config, repo, policy, now):
    policy_digest = repair._digest(
        [
            policy,
            {
                key: repo[key]
                for key in (
                    "full_name",
                    "repo_root",
                    "worktree_root",
                    "default_base_branch",
                )
            },
        ]
    )
    with closing(repair._connect(config)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if not supervision_store.has_schema(conn):
            return None
        candidate = _candidate(
            conn, repository=policy["repository"], policy_digest=policy_digest, now=now
        )
        if not candidate:
            return None
        incident_id = candidate["incident_id"]
        key = ("repair", incident_id, "followup")
        state = {
            "status": "checking",
            "claim_id": uuid4().hex,
            "result_id": candidate["result_id"],
            "revision": candidate["revision"],
            "policy_digest": policy_digest,
            "started_at": now.isoformat(),
            "next_check_at": (now + _RECHECK).isoformat(),
            "claim_expires_at": (now + _CLAIM_WINDOW).isoformat(),
        }
        repair._store(conn, key, state, now)
    return key, state, candidate["worktree_path"]


def _finish(config, key, claimed, status, now):
    with closing(repair._connect(config)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if repair._state(conn, key).get("claim_id") != claimed["claim_id"]:
            return False
        complete = status == "verified"
        repair._store(
            conn,
            key,
            {
                "status": "complete" if complete else "pending",
                "result_id": claimed["result_id"],
                "revision": claimed["revision"],
                "policy_digest": claimed["policy_digest"],
                "last_status": status,
                "finished_at": now.isoformat(),
                "next_check_at": None if complete else (now + _RECHECK).isoformat(),
            },
            now,
        )
        return True


def poll(config, *, now=None, runtime=None):
    """Check at most one due delivery. Run from the independent maintenance child."""
    policy = repair._policy(config)
    repo = repair._repo(config, policy) if policy else None
    if not repo:
        return repair._proposal_status(
            "disabled" if not policy else "maintenance_repository_unavailable"
        )
    current = (
        supervision_rules.timestamp(now)
        if now is not None
        else datetime.now(timezone.utc)
    )
    if current is None:
        return repair._proposal_status("unavailable")
    try:
        with task_publication_lock(
            repair._path(config), "supervision-repair-followup"
        ) as acquired:
            if not acquired:
                return repair._proposal_status("busy")
            claim = _claim(config, repo, policy, current)
            if not claim:
                return repair._proposal_status("idle")
            key, state, path = claim
            raw = Path(path)
            checkout = raw.resolve()
            root = Path(repo["worktree_root"]).resolve()
            if (
                not raw.is_absolute()
                or checkout == root
                or not checkout.is_relative_to(root)
            ):
                outcome = repair._proposal_status(
                    "verification_pending", reason_code="delivery_worktree_out_of_scope"
                )
            else:
                outcome = repair.verify(
                    config,
                    key[1],
                    checkout=checkout,
                    runtime=runtime,
                    now=now,
                    expected_delivery=(state["result_id"], state["revision"]),
                )
            finished = datetime.now(timezone.utc) if now is None else current
            if not _finish(config, key, state, outcome["status"], finished):
                return repair._proposal_status(
                    "repair_evidence_changed", incident_id=key[1]
                )
            return {**outcome, "incident_id": key[1]}
    except (OSError, ValueError, sqlite3.Error):
        return repair._proposal_status("unavailable")
