"""Existing review schema initialization and upgrades."""

from robert_agent.persistence.dialects.sqlite import execute_control
from robert_agent.persistence.dialects import sqlite as db_sqlite
from robert_agent.persistence.stores import review_runs as db_write_review_runs

SCHEMA = """
CREATE TABLE IF NOT EXISTS review_sessions (
  round_id TEXT PRIMARY KEY,
  repo TEXT NOT NULL,
  pr_number INTEGER NOT NULL,
  workstream_id TEXT NOT NULL,
  trigger_fingerprint TEXT NOT NULL,
  task_id TEXT UNIQUE REFERENCES tasks(task_id),
  status TEXT NOT NULL DEFAULT 'queued',
  request_json TEXT NOT NULL,
  manifest_json TEXT,
  report_path TEXT,
  report_sha256 TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(repo, pr_number, trigger_fingerprint)
);
CREATE TABLE IF NOT EXISTS review_runs (
  run_id TEXT PRIMARY KEY,
  round_id TEXT NOT NULL REFERENCES review_sessions(round_id),
  stage TEXT NOT NULL CHECK(stage IN ('prepare','review','synthesis')),
  worker_id TEXT NOT NULL,
  model TEXT,
  attempt_no INTEGER NOT NULL,
  owner_attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
  status TEXT NOT NULL,
  output_dir TEXT NOT NULL UNIQUE,
  prompt_sha256 TEXT NOT NULL,
  slot_reserved INTEGER NOT NULL DEFAULT 0,
  started_at TEXT,
  dispatch_json TEXT,
  artifact_json TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(round_id, stage, worker_id, attempt_no)
);
CREATE INDEX IF NOT EXISTS review_sessions_workstream ON review_sessions(workstream_id, created_at);
CREATE TABLE IF NOT EXISTS review_process_pool (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  max_concurrency INTEGER NOT NULL CHECK(max_concurrency>0)
);
INSERT OR IGNORE INTO review_process_pool VALUES (1,4);
"""


def initialize(conn):
    # execute separately: executescript would commit the caller's transaction.
    for statement in SCHEMA.split(";"):
        if statement.strip():
            execute_control(conn, statement)
    if "model" not in {row[1] for row in db_sqlite.table_info(conn, "review_runs")}:
        execute_control(conn, "ALTER TABLE review_runs ADD COLUMN model TEXT")
    columns = {row[1] for row in db_sqlite.table_info(conn, "review_runs")}
    if "slot_reserved" not in columns:
        execute_control(
            conn,
            "ALTER TABLE review_runs ADD COLUMN slot_reserved INTEGER NOT NULL DEFAULT 0",
        )
        # Existing children remain counted until their process groups are
        # proven to have exited, including ambiguous starts.
        db_write_review_runs.update_for_initialize(conn)
    if "started_at" not in columns:
        execute_control(conn, "ALTER TABLE review_runs ADD COLUMN started_at TEXT")
        db_write_review_runs.update_for_initialize_2(conn)

    # Build after legacy columns are upgraded, not in the initial schema loop.
    execute_control(
        conn,
        "CREATE INDEX IF NOT EXISTS review_runs_reserved ON review_runs(slot_reserved) WHERE slot_reserved=1",
    )
