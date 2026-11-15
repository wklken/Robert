"""Move historical episode counters into repair cycle ownership in place."""

import hashlib

from robert_agent.persistence.schema import native_sources


VERSION = 9
NAME = "robert-repair-cycles-v1"
CHECKSUM = hashlib.sha256(NAME.encode("utf-8")).hexdigest()


def has_legacy_counters(conn):
    return any(row[1] == "attempt_count" for row in conn.execute("PRAGMA table_info(pr_remediation_episodes)"))


def migrate(conn):
    # The column owns its CHECK and no production index depends on it. SQLite
    # removes both in place while preserving episode IDs and all child FKs.
    historical = conn.execute("SELECT count(*) FROM pr_remediation_episodes").fetchone()[0]
    conn.execute(
        """
        INSERT INTO repair_cycles(cycle_id,scope,episode_id,started_at,status,close_reason,
                                  legacy_attempt_count,created_at,updated_at)
        SELECT episode_id,'github',episode_id,first_seen_at,
               CASE WHEN status IN ('resolved','exhausted','superseded','canceled') THEN 'closed' ELSE 'active' END,
               CASE WHEN status IN ('resolved','exhausted','superseded','canceled') THEN status END,
               attempt_count,first_seen_at,updated_at
        FROM pr_remediation_episodes
        """
    )
    copied = conn.execute("SELECT count(*) FROM repair_cycles WHERE scope='github'").fetchone()[0]
    if copied != historical:
        raise RuntimeError("Repair cycle migration changed the historical episode count")
    conn.execute("ALTER TABLE pr_remediation_episodes DROP COLUMN attempt_count")


def has_global_claim_task_key(conn):
    """Detect the prerelease unscoped task key, including its SQLite autoindex."""
    return any(
        unique
        and not partial
        and [row[0] for row in conn.execute("SELECT name FROM pragma_index_info(?)", (name,))] == ["task_id"]
        for _, name, unique, _, partial in conn.execute("PRAGMA index_list(repair_cycle_claims)")
    )


def allow_legacy_restarts(conn, schema):
    if conn.execute("PRAGMA foreign_keys").fetchone()[0]:
        raise RuntimeError("Repair claim key upgrade requires a dedicated rebuild connection")
    native_sources.check_quiescent(conn)
    native_sources.replace_table_preserving_rows(conn, "repair_cycle_claims", schema)
    for name, field in (
        ("idx_repair_cycle_claims_native_task", "task_id"),
        ("idx_repair_cycle_claims_native_request", "request_key"),
    ):
        conn.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON repair_cycle_claims({field}) WHERE round_id IS NOT NULL"
        )
    if has_global_claim_task_key(conn):
        raise RuntimeError("Unscoped repair claim task uniqueness remains after upgrade")
