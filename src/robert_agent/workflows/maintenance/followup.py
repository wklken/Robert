"""Bounded, durable follow-up for engineering repairs with an audited delivery."""

from robert_agent.persistence import errors as db_errors
from robert_agent.persistence import transactions as db_transactions

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import supervision_checkpoints as db_read_supervision_checkpoints

from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from robert_agent.workflows.maintenance import repair
from robert_agent.supervision import rules as supervision_rules
from robert_agent.supervision import store as supervision_store
from robert_agent.delivery.lock import task_publication_lock


_RECHECK = timedelta(minutes=5)
_CLAIM_WINDOW = timedelta(minutes=7)


def _candidate(conn, *, repository, policy_digest, now):
    return db_records.first(
        db_read_supervision_checkpoints.find_for_candidate(
            conn, repository, policy_digest, now.isoformat(), now.isoformat()
        )
    )


def _claim(config, repo, policy, now):
    policy_digest = repair._digest(
        [policy, {key: repo[key] for key in ("full_name", "repo_root", "worktree_root", "default_base_branch")}]
    )
    with closing(repair._connect(config)) as conn, conn:
        db_transactions.begin(conn, immediate=True)
        if not supervision_store.has_schema(conn):
            return None
        candidate = _candidate(conn, repository=policy["repository"], policy_digest=policy_digest, now=now)
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
        db_transactions.begin(conn, immediate=True)
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
        return repair._proposal_status("disabled" if not policy else "maintenance_repository_unavailable")
    current = supervision_rules.timestamp(now) if now is not None else datetime.now(timezone.utc)
    if current is None:
        return repair._proposal_status("unavailable")
    try:
        with task_publication_lock(repair._path(config), "supervision-repair-followup") as acquired:
            if not acquired:
                return repair._proposal_status("busy")
            claim = _claim(config, repo, policy, current)
            if not claim:
                return repair._proposal_status("idle")
            key, state, path = claim
            raw = Path(path)
            checkout = raw.resolve()
            root = Path(repo["worktree_root"]).resolve()
            if not raw.is_absolute() or checkout == root or not checkout.is_relative_to(root):
                outcome = repair._proposal_status("verification_pending", reason_code="delivery_worktree_out_of_scope")
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
                return repair._proposal_status("repair_evidence_changed", incident_id=key[1])
            return {**outcome, "incident_id": key[1]}
    except (OSError, ValueError, db_errors.Error):
        return repair._proposal_status("unavailable")
