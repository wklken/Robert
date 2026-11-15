"""Observe and bind one exact repository's receipt-linked PR disposition."""

from contextlib import closing
from dataclasses import asdict, dataclass
import subprocess

from robert_agent.adapters.github import pull_requests
from robert_agent.foundation import values
from robert_agent.foundation.commands import ControlDeadlineExceeded
from robert_agent.persistence import database, transactions
from robert_agent.persistence.queries import source_pr_selection as read, source_sessions as rounds
from robert_agent.persistence.rounds.source_evidence import context_digest
from robert_agent.persistence.stores import source_pr_selection as write
from robert_agent.work import source_pr_selection as policy
from robert_agent.work.application import admission


@dataclass(frozen=True, slots=True)
class PRSelection:
    status: str
    reason: str
    target: policy.ExistingPRTarget | None = None
    fingerprint: str | None = None


def observe(db_path, config, claim, *, round_id, repo_id, revision, task_id=None, stop=None, mixed_proof=None):
    """Observe canonical history outside the writer and commit only current scope."""

    def authorized(conn, now):
        return admission.native_round_authority(
            conn,
            config,
            claim,
            round_id=round_id,
            revision=revision,
            repo_id=repo_id,
            capability="development",
            now=now,
            task_id=task_id,
            mixed_proof=mixed_proof,
        )

    if stop is not None and stop.is_set():
        return PRSelection("held", "source_pr_observation_stopped")
    with closing(database.connect(db_path)) as conn:
        round_row = rounds.active_round(conn, claim.source_id)
        if round_row is None or round_row["round_id"] != round_id or round_row["capability"] != "development":
            return PRSelection("held", "source_pr_scope_changed")
        history = read.creation_history(conn, claim.source_id, repo_id)
    with closing(database.connect(db_path, foreign_keys=True)) as conn, conn:
        transactions.begin_immediate(conn)
        initial = authorized(conn, values.utc_now())
    if initial is None:
        return PRSelection("held", "source_pr_scope_changed")
    _, initial_members, repo = initial
    try:
        snapshots = pull_requests.read_native_prs(
            repo["full_name"], (entry.number for entry in history or ()), stop=stop
        )
        observations = policy.normalize_observations(history, snapshots, repo["github_account"])
    except ControlDeadlineExceeded:
        return PRSelection("held", "source_pr_observation_stopped")
    except (OSError, ValueError, subprocess.SubprocessError):
        observations = []
    status, reason, target = policy.select_target(history, observations, repo["default_base_branch"])
    proof = {
        "source_id": claim.source_id,
        "round_id": round_id,
        "repo_id": repo_id,
        "repository": repo["full_name"],
        "publisher": repo["github_account"],
        "default_base_branch": repo["default_base_branch"],
        "status": status,
        "reason": reason,
        "target": asdict(target) if target else None,
        "observations": observations,
        "expected_actions": None if history is None else [entry.action_id for entry in history],
    }
    fingerprint = context_digest(proof)
    with closing(database.connect(db_path, foreign_keys=True)) as conn, conn:
        transactions.begin_immediate(conn)
        now = values.utc_now()
        current = authorized(conn, now)
        if (
            current is None
            or current[1] != initial_members
            or current[2] != repo
            or read.creation_history(conn, claim.source_id, repo_id) != history
            or (stop is not None and stop.is_set())
            or not write.save(
                conn,
                claim,
                round_id=round_id,
                repo_id=repo_id,
                now=now,
                decision={**proof, "fingerprint": fingerprint},
            )
        ):
            return PRSelection("held", "source_pr_scope_changed")
    return PRSelection(status, reason, target, fingerprint)
