"""Existing execution control schema; preserve caller-owned transactions."""

from robert_agent.persistence.dialects.sqlite import execute_control

SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_subjects (
  subject_type TEXT NOT NULL CHECK (subject_type IN ('task','remediation_episode')),
  subject_id TEXT NOT NULL,
  workflow_kind TEXT NOT NULL CHECK (workflow_kind IN ('human_request','pr_remediation')),
  binding_status TEXT NOT NULL CHECK (binding_status IN ('active','terminal','attention')),
  recovery_attempts INTEGER NOT NULL DEFAULT 0 CHECK (recovery_attempts BETWEEN 0 AND 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  terminal_at TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY(subject_type,subject_id)
);
CREATE TABLE IF NOT EXISTS execution_pending_facts (
  source_signal_id TEXT PRIMARY KEY,
  subject_type TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  topic TEXT NOT NULL,
  dedupe_key TEXT NOT NULL,
  fact_id TEXT NOT NULL,
  disposition TEXT NOT NULL,
  repo_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  not_before_at TEXT NOT NULL,
  consumed_at TEXT,
  resolution_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS execution_fact_receipts (
  subject_type TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  fact_id TEXT NOT NULL,
  evidence_json TEXT,
  PRIMARY KEY(subject_type,subject_id,fact_id)
);
CREATE TABLE IF NOT EXISTS execution_migrations (
  migration_id TEXT PRIMARY KEY,
  source_fingerprint TEXT NOT NULL UNIQUE,
  applied_at TEXT NOT NULL,
  snapshot_json TEXT NOT NULL,
  mappings_json TEXT NOT NULL
);
"""


def initialize(conn):
    # execute each statement, preserving a caller-owned transaction.
    for statement in SCHEMA.split(";"):
        if statement.strip():
            execute_control(conn, statement)
