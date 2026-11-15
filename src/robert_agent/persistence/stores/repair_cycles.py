"""Durable repair budget mutations. The caller owns each writer transaction."""

from uuid import uuid4

from sqlalchemy import func, literal, select, update
from sqlalchemy.dialects.sqlite import insert

from robert_agent.foundation.contracts.work import RemediationStatus
from robert_agent.persistence import statements, transactions
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.stores.pr_remediation_episodes import record_task_started


def ensure_episode_cycle(conn, episode_id):
    cycle, episode = (tables[name] for name in ("repair_cycles", "pr_remediation_episodes"))
    return statements.write(
        conn,
        insert(cycle)
        .from_select(
            ["cycle_id", "scope", "episode_id", "started_at", "status", "created_at", "updated_at"],
            select(
                episode.c.episode_id,
                literal("github"),
                episode.c.episode_id,
                episode.c.first_seen_at,
                literal("active"),
                episode.c.first_seen_at,
                episode.c.updated_at,
            ).where(episode.c.episode_id == episode_id),
        )
        .on_conflict_do_nothing(index_elements=[cycle.c.episode_id]),
    )


def record_episode_claim(conn, *, episode_id, task_id, now):
    if not conn.in_transaction:
        transactions.begin_immediate(conn)
    checkpoint = "record_repair_claim"
    transactions.savepoint(conn, checkpoint)
    try:
        ensure_episode_cycle(conn, episode_id)
        cycle, claim, task, episode, member = (
            tables[name]
            for name in (
                "repair_cycles",
                "repair_cycle_claims",
                "tasks",
                "pr_remediation_episodes",
                "source_round_tasks",
            )
        )
        native_claim = claim.alias("prior_native_repair_claim")
        # The state transition distinguishes a genuinely reopened start even
        # when the same task and timestamp are reused.
        inserted = statements.write(
            conn,
            insert(claim).from_select(
                ["cycle_id", "request_key", "episode_id", "task_id", "created_at"],
                select(
                    cycle.c.cycle_id,
                    literal("legacy-start:" + uuid4().hex),
                    cycle.c.episode_id,
                    task.c.task_id,
                    literal(now),
                )
                .select_from(
                    cycle.join(episode, episode.c.episode_id == cycle.c.episode_id).join(
                        task, task.c.workstream_id == episode.c.workstream_id
                    )
                )
                .where(
                    cycle.c.episode_id == episode_id,
                    task.c.task_id == task_id,
                    cycle.c.scope == "github",
                    cycle.c.status == "active",
                    episode.c.status == RemediationStatus.OPEN.value,
                    ~select(member.c.task_id).where(member.c.task_id == task_id).exists(),
                    ~select(native_claim.c.task_id)
                    .where(native_claim.c.task_id == task_id, native_claim.c.round_id.is_not(None))
                    .exists(),
                ),
            ),
        )
        changed = inserted == 1 and record_task_started(conn, task_id, now, episode_id) == 1
    except BaseException:
        transactions.finish_isolation(conn, checkpoint, rollback=True)
        raise
    transactions.finish_isolation(conn, checkpoint, rollback=not changed)
    return bool(changed)


def close_episode_cycle(conn, *, episode_id, reason, now):
    cycle = tables["repair_cycles"]
    return statements.write(
        conn,
        update(cycle)
        .where(cycle.c.episode_id == episode_id, cycle.c.status == "active")
        .values(status="closed", close_reason=reason, updated_at=now),
    )


def insert_native_cycle(conn, *, cycle_id, source_id, pr_source_id, base_ref, policy_json, now):
    return statements.write(
        conn,
        insert(tables["repair_cycles"]).values(
            cycle_id=cycle_id,
            scope="tapd",
            source_id=source_id,
            pr_source_id=pr_source_id,
            authorized_base_ref=base_ref,
            policy_version=1,
            policy_json=policy_json,
            status="active",
            created_at=now,
            updated_at=now,
        ),
    )


def tighten_native_policy(conn, *, cycle_id, policy_cap_json, now):
    cycle = tables["repair_cycles"]
    return statements.write(
        conn,
        update(cycle)
        .where(cycle.c.cycle_id == cycle_id, cycle.c.status.in_(("active", "paused")))
        .values(policy_cap_json=policy_cap_json, updated_at=now),
    )


def observe_native_cycle(conn, *, cycle_id, status, reason, observed_at, observation_digest, now):
    cycle = tables["repair_cycles"]
    return statements.write(
        conn,
        update(cycle)
        .where(cycle.c.cycle_id == cycle_id, cycle.c.scope == "tapd", cycle.c.status.in_(("active", "paused")))
        .values(
            status=status,
            close_reason=reason,
            observed_at=observed_at,
            observation_digest=observation_digest,
            updated_at=now,
        ),
    )


def record_native_claim(conn, *, cycle_id, request_id, episode_id, task_id, round_id, now):
    cycle, claim = (tables[name] for name in ("repair_cycles", "repair_cycle_claims"))
    count = select(func.count()).where(claim.c.cycle_id == cycle.c.cycle_id).correlate(cycle).scalar_subquery()
    legacy_claim = select(claim.c.task_id).where(claim.c.task_id == task_id, claim.c.round_id.is_(None)).exists()
    inserted = statements.write(
        conn,
        insert(claim)
        .from_select(
            ["cycle_id", "request_key", "episode_id", "task_id", "round_id", "created_at"],
            select(
                cycle.c.cycle_id,
                literal(request_id),
                literal(episode_id),
                literal(task_id),
                literal(round_id),
                literal(now),
            ).where(
                cycle.c.cycle_id == cycle_id,
                cycle.c.scope == "tapd",
                cycle.c.status == "active",
                count < 3,
                ~legacy_claim,
            ),
        )
        .on_conflict_do_nothing(),
    )
    if inserted:
        statements.write(
            conn,
            update(cycle)
            .where(cycle.c.cycle_id == cycle_id)
            .values(started_at=func.coalesce(cycle.c.started_at, now), updated_at=now),
        )
    return bool(inserted)


def record_native_usage(conn, *, cycle_id, attempt_id, tokens, cost_usd, observed_at):
    claim, attempt, usage = (tables[name] for name in ("repair_cycle_claims", "attempts", "repair_cycle_usage"))
    values = (
        select(attempt.c.attempt_id, claim.c.cycle_id, literal(tokens), literal(cost_usd), literal(observed_at))
        .select_from(attempt.join(claim, claim.c.task_id == attempt.c.task_id))
        .where(
            attempt.c.attempt_id == attempt_id,
            claim.c.cycle_id == cycle_id,
            claim.c.round_id.is_not(None),
            attempt.c.status.in_(("completed", "failed", "canceled")),
        )
    )
    statement = insert(usage).from_select(["attempt_id", "cycle_id", "tokens", "cost_usd", "observed_at"], values)
    return bool(
        statements.write(
            conn,
            statement.on_conflict_do_update(
                index_elements=[usage.c.attempt_id],
                set_={
                    "tokens": func.coalesce(usage.c.tokens, statement.excluded.tokens),
                    "cost_usd": func.coalesce(usage.c.cost_usd, statement.excluded.cost_usd),
                    "observed_at": statement.excluded.observed_at,
                },
                where=(usage.c.cycle_id == statement.excluded.cycle_id)
                & (
                    (usage.c.tokens.is_(None))
                    | (usage.c.tokens == statement.excluded.tokens)
                    | statement.excluded.tokens.is_(None)
                )
                & (
                    (usage.c.cost_usd.is_(None))
                    | (usage.c.cost_usd == statement.excluded.cost_usd)
                    | statement.excluded.cost_usd.is_(None)
                ),
            ),
        )
    )
