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
CREATE TABLE IF NOT EXISTS execution_fact_receipts (
  subject_type TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  fact_id TEXT NOT NULL,
  evidence_json TEXT,
  PRIMARY KEY(subject_type,subject_id,fact_id)
);
CREATE TABLE IF NOT EXISTS publication_intents (
  publication_id TEXT PRIMARY KEY,
  action_id TEXT NOT NULL UNIQUE
    REFERENCES github_actions(action_id) ON DELETE CASCADE,
  idempotency_key TEXT NOT NULL UNIQUE,
  request_fingerprint TEXT NOT NULL,
  status TEXT NOT NULL
    CHECK (status IN (
      'prepared', 'executing', 'published', 'confirmed_absent',
      'failed', 'attention'
    )),
  started_at TEXT,
  finished_at TEXT,
  remote_evidence_json TEXT NOT NULL DEFAULT '{}',
  error_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""


def initialize(conn):
    # execute each statement, preserving a caller-owned transaction.
    for statement in SCHEMA.split(";"):
        if statement.strip():
            execute_control(conn, statement)
