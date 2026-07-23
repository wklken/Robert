PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  checksum TEXT NOT NULL,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS repos (
  repo_id TEXT PRIMARY KEY,
  full_name TEXT NOT NULL UNIQUE,
  github_account TEXT NOT NULL,
  default_base_branch TEXT NOT NULL,
  repo_root TEXT NOT NULL,
  worktree_root TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS actors (
  actor_id TEXT PRIMARY KEY,
  login TEXT NOT NULL UNIQUE,
  actor_kind TEXT NOT NULL DEFAULT 'user',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS actor_permissions (
  permission_id TEXT PRIMARY KEY,
  repo_id TEXT NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
  actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE CASCADE,
  trust_level TEXT NOT NULL CHECK (trust_level IN ('trusted_trigger', 'accepted_context', 'ignored')),
  source TEXT NOT NULL,
  checked_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(repo_id, actor_id, source)
);

CREATE TABLE IF NOT EXISTS github_sources (
  source_id TEXT PRIMARY KEY,
  repo_id TEXT NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
  source_key TEXT NOT NULL UNIQUE,
  source_type TEXT NOT NULL CHECK (source_type IN ('issue', 'pull_request')),
  number INTEGER NOT NULL,
  html_url TEXT,
  title TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'open',
  author_login TEXT,
  source_updated_at TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS github_events (
  event_id TEXT PRIMARY KEY,
  repo_id TEXT NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
  source_id TEXT NOT NULL REFERENCES github_sources(source_id) ON DELETE CASCADE,
  event_fingerprint TEXT NOT NULL UNIQUE,
  event_type TEXT NOT NULL,
  actor_login TEXT,
  author_association TEXT,
  authorization_status TEXT NOT NULL DEFAULT 'pending',
  event_at TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS workstreams (
  workstream_id TEXT PRIMARY KEY,
  repo_id TEXT NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
  primary_source_id TEXT REFERENCES github_sources(source_id) ON DELETE SET NULL,
  origin_workstream_id TEXT REFERENCES workstreams(workstream_id) ON DELETE SET NULL,
  lifecycle TEXT NOT NULL DEFAULT 'active' CHECK (lifecycle IN ('active', 'completed', 'waiting_for_user', 'failed', 'canceled')),
  active_task_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS workstream_sources (
  workstream_id TEXT NOT NULL REFERENCES workstreams(workstream_id) ON DELETE CASCADE,
  source_id TEXT NOT NULL REFERENCES github_sources(source_id) ON DELETE CASCADE,
  relationship TEXT NOT NULL CHECK (relationship IN ('primary', 'derived_pr', 'origin_issue', 'related')),
  created_at TEXT NOT NULL,
  PRIMARY KEY(workstream_id, source_id)
);

CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY,
  workstream_id TEXT NOT NULL REFERENCES workstreams(workstream_id) ON DELETE CASCADE,
  lifecycle TEXT NOT NULL CHECK (lifecycle IN ('detected', 'authorized', 'classified', 'queued', 'running', 'completed', 'waiting_for_user', 'failed', 'canceled', 'ignored')),
  parent_task_id TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
  priority TEXT NOT NULL DEFAULT 'P2',
  routing_mode TEXT NOT NULL DEFAULT 'auto' CHECK (routing_mode IN ('auto', 'manual')),
  requested_worker TEXT,
  route_id TEXT,
  expected_output TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  CHECK ((routing_mode = 'auto' AND requested_worker IS NULL)
      OR (routing_mode = 'manual' AND requested_worker IS NOT NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_workstreams_one_active_task
ON tasks(workstream_id)
WHERE lifecycle IN ('detected', 'authorized', 'classified', 'queued', 'running');

CREATE TABLE IF NOT EXISTS work_items (
  work_item_id TEXT PRIMARY KEY,
  repo_id TEXT NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  priority TEXT NOT NULL DEFAULT 'P2' CHECK (priority IN ('P0', 'P1', 'P2', 'P3')),
  origin_type TEXT NOT NULL CHECK (origin_type IN ('web', 'github')),
  origin_source_id TEXT REFERENCES github_sources(source_id) ON DELETE SET NULL,
  routing_mode TEXT NOT NULL DEFAULT 'auto' CHECK (routing_mode IN ('auto', 'manual')),
  requested_worker TEXT,
  workstream_id TEXT REFERENCES workstreams(workstream_id) ON DELETE SET NULL,
  creation_idempotency_key TEXT NOT NULL UNIQUE,
  created_by TEXT NOT NULL,
  activated_at TEXT,
  completed_at TEXT,
  canceled_at TEXT,
  version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  CHECK ((routing_mode = 'auto' AND requested_worker IS NULL)
      OR (routing_mode = 'manual' AND requested_worker IS NOT NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_work_items_origin_source
ON work_items(origin_source_id) WHERE origin_source_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_work_items_workstream
ON work_items(workstream_id) WHERE workstream_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_work_items_board_order
ON work_items(updated_at DESC, work_item_id DESC);

CREATE INDEX IF NOT EXISTS idx_work_items_repo_updated
ON work_items(repo_id, updated_at DESC, work_item_id DESC);

CREATE TABLE IF NOT EXISTS work_item_events (
  event_id TEXT PRIMARY KEY,
  work_item_id TEXT NOT NULL REFERENCES work_items(work_item_id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  actor_kind TEXT NOT NULL,
  actor_identity TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  resolves_event_id TEXT REFERENCES work_item_events(event_id) ON DELETE SET NULL,
  idempotency_key TEXT NOT NULL,
  created_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_work_item_events_idempotency
ON work_item_events(work_item_id, idempotency_key);

CREATE INDEX IF NOT EXISTS idx_work_item_events_timeline
ON work_item_events(work_item_id, created_at DESC, event_id DESC);

CREATE INDEX IF NOT EXISTS idx_work_item_events_resolves
ON work_item_events(resolves_event_id) WHERE resolves_event_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS task_events (
  task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  event_id TEXT NOT NULL REFERENCES github_events(event_id) ON DELETE CASCADE,
  relationship TEXT NOT NULL CHECK (relationship IN ('trigger', 'pending', 'consumed', 'context')),
  created_at TEXT NOT NULL,
  PRIMARY KEY(task_id, event_id)
);

CREATE TABLE IF NOT EXISTS route_decisions (
  route_decision_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  route_id TEXT NOT NULL,
  expected_output TEXT NOT NULL,
  allowed_github_actions_json TEXT NOT NULL DEFAULT '[]',
  required_skills_json TEXT NOT NULL DEFAULT '[]',
  recommended_skills_json TEXT NOT NULL DEFAULT '[]',
  confidence TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attempts (
  attempt_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
  status TEXT NOT NULL CHECK (status IN ('prepared', 'running', 'completed', 'failed', 'stale', 'canceled')),
  worktree_path TEXT,
  branch_name TEXT,
  started_at TEXT,
  heartbeat_at TEXT,
  finished_at TEXT,
  failure_json TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(task_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS worker_phases (
  phase_id TEXT PRIMARY KEY,
  attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id) ON DELETE CASCADE,
  phase TEXT NOT NULL CHECK (phase IN ('prepare', 'analyze', 'plan', 'execute', 'verify', 'publish', 'handoff')),
  status TEXT NOT NULL,
  summary TEXT NOT NULL,
  next_step TEXT,
  created_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS worker_results (
  result_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id) ON DELETE CASCADE,
  output_type TEXT NOT NULL,
  consumed_event_fingerprints_json TEXT NOT NULL DEFAULT '[]',
  verification_json TEXT NOT NULL DEFAULT '[]',
  handoff TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(task_id, attempt_id)
);

CREATE TABLE IF NOT EXISTS github_actions (
  action_id TEXT PRIMARY KEY,
  result_id TEXT REFERENCES worker_results(result_id) ON DELETE SET NULL,
  task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  action_type TEXT NOT NULL,
  target_url TEXT,
  external_id TEXT,
  audit_status TEXT NOT NULL DEFAULT 'pending' CHECK (audit_status IN ('pending', 'accepted', 'policy_violation', 'failed')),
  publish_status TEXT NOT NULL DEFAULT 'not_published' CHECK (publish_status IN ('not_published', 'published', 'skipped')),
  created_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS artifacts (
  artifact_id TEXT PRIMARY KEY,
  task_id TEXT REFERENCES tasks(task_id) ON DELETE CASCADE,
  attempt_id TEXT REFERENCES attempts(attempt_id) ON DELETE SET NULL,
  artifact_type TEXT NOT NULL,
  path TEXT NOT NULL,
  sha256 TEXT,
  bytes INTEGER,
  created_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS notifications (
  notification_id TEXT PRIMARY KEY,
  task_id TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
  notification_type TEXT NOT NULL,
  channel TEXT NOT NULL DEFAULT 'local',
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS wakeups (
  wakeup_id TEXT PRIMARY KEY,
  repo_id TEXT NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
  reason TEXT NOT NULL CHECK (reason IN (
    'worker_result_ready',
    'queued_capacity_wait',
    'publish_retry_ready',
    'stale_attempt_check',
    'manual_operator_request'
  )),
  dedupe_key TEXT NOT NULL,
  work_item_id TEXT REFERENCES work_items(work_item_id) ON DELETE CASCADE,
  task_id TEXT REFERENCES tasks(task_id) ON DELETE CASCADE,
  attempt_id TEXT REFERENCES attempts(attempt_id) ON DELETE SET NULL,
  result_id TEXT REFERENCES worker_results(result_id) ON DELETE CASCADE,
  source_run_id TEXT REFERENCES agent_runs(run_id) ON DELETE SET NULL,
  consumed_run_id TEXT REFERENCES agent_runs(run_id) ON DELETE SET NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'consumed', 'expired', 'canceled')),
  not_before_at TEXT NOT NULL,
  expires_at TEXT,
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(repo_id, reason, dedupe_key)
);

CREATE INDEX IF NOT EXISTS idx_wakeups_status_due
ON wakeups(repo_id, status, not_before_at, created_at);

CREATE INDEX IF NOT EXISTS idx_wakeups_result
ON wakeups(result_id);

CREATE TABLE IF NOT EXISTS agent_runs (
  run_id TEXT PRIMARY KEY,
  status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed', 'skipped')),
  started_at TEXT NOT NULL,
  finished_at TEXT,
  config_path TEXT,
  dry_run INTEGER NOT NULL DEFAULT 0 CHECK (dry_run IN (0, 1)),
  summary_json TEXT NOT NULL DEFAULT '{}',
  error_json TEXT
);

CREATE TABLE IF NOT EXISTS run_steps (
  step_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
  step_key TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'skipped')),
  started_at TEXT,
  finished_at TEXT,
  output_json TEXT,
  error_json TEXT,
  UNIQUE(run_id, step_key)
);

CREATE TABLE IF NOT EXISTS run_repo_steps (
  step_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
  repo_id TEXT NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
  step_key TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'skipped')),
  started_at TEXT,
  finished_at TEXT,
  output_json TEXT,
  error_json TEXT,
  UNIQUE(run_id, repo_id, step_key)
);

CREATE TABLE IF NOT EXISTS leases (
  lease_id TEXT PRIMARY KEY,
  resource_type TEXT NOT NULL,
  resource_key TEXT NOT NULL,
  owner_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active', 'released', 'expired', 'stolen')),
  acquired_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL,
  released_at TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_leases_one_active_resource
ON leases(resource_type, resource_key)
WHERE status = 'active';

CREATE TABLE IF NOT EXISTS daemon_runs (
  daemon_run_id TEXT PRIMARY KEY,
  status TEXT NOT NULL CHECK (status IN ('running', 'stopped', 'failed', 'replaced')),
  owner_id TEXT NOT NULL,
  config_path TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  summary_json TEXT NOT NULL DEFAULT '{}',
  error_json TEXT
);

CREATE TABLE IF NOT EXISTS daemon_events (
  daemon_event_id TEXT PRIMARY KEY,
  daemon_run_id TEXT NOT NULL REFERENCES daemon_runs(daemon_run_id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_daemon_events_run_created
ON daemon_events(daemon_run_id, created_at);

CREATE INDEX IF NOT EXISTS idx_daemon_events_type_created
ON daemon_events(event_type, created_at);

CREATE TABLE IF NOT EXISTS audit_events (
  audit_id TEXT PRIMARY KEY,
  repo_id TEXT REFERENCES repos(repo_id) ON DELETE SET NULL,
  workstream_id TEXT REFERENCES workstreams(workstream_id) ON DELETE SET NULL,
  task_id TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
  event_type TEXT NOT NULL,
  created_at TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS project_memory_entries (
  memory_id TEXT PRIMARY KEY,
  repo_id TEXT NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
  memory_thread_key TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'context',
  title TEXT NOT NULL,
  short_summary TEXT NOT NULL,
  long_summary TEXT NOT NULL DEFAULT '',
  confidence TEXT NOT NULL DEFAULT 'medium' CHECK (confidence IN ('high', 'medium', 'low')),
  source_task_id TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
  source_result_id TEXT REFERENCES worker_results(result_id) ON DELETE SET NULL,
  current_revision_id TEXT,
  revision_count INTEGER NOT NULL DEFAULT 0 CHECK (revision_count >= 0),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_used_at TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(repo_id, memory_thread_key, kind, title)
);

CREATE INDEX IF NOT EXISTS idx_project_memory_entries_repo_updated
ON project_memory_entries(repo_id, updated_at);

CREATE TABLE IF NOT EXISTS project_memory_revisions (
  revision_id TEXT PRIMARY KEY,
  memory_id TEXT NOT NULL REFERENCES project_memory_entries(memory_id) ON DELETE CASCADE,
  repo_id TEXT NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
  result_id TEXT REFERENCES worker_results(result_id) ON DELETE SET NULL,
  task_id TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
  attempt_id TEXT REFERENCES attempts(attempt_id) ON DELETE SET NULL,
  operation TEXT NOT NULL DEFAULT 'upsert',
  title TEXT NOT NULL,
  short_summary TEXT NOT NULL,
  long_summary TEXT NOT NULL DEFAULT '',
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_project_memory_revisions_memory
ON project_memory_revisions(memory_id, created_at);

CREATE TABLE IF NOT EXISTS project_memory_terms (
  memory_id TEXT NOT NULL REFERENCES project_memory_entries(memory_id) ON DELETE CASCADE,
  repo_id TEXT NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
  term_type TEXT NOT NULL,
  term_value TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(memory_id, term_type, term_value)
);

CREATE INDEX IF NOT EXISTS idx_project_memory_terms_lookup
ON project_memory_terms(repo_id, term_type, term_value);

CREATE TABLE IF NOT EXISTS knowledge_candidates (
  candidate_id TEXT PRIMARY KEY,
  repo_id TEXT NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  summary TEXT NOT NULL,
  prompt_text TEXT NOT NULL,
  candidate_type TEXT NOT NULL DEFAULT 'rule' CHECK (candidate_type IN ('rule', 'anti_pattern', 'route_hint', 'verification_hint')),
  source_memory_ids_json TEXT NOT NULL DEFAULT '[]',
  evidence_json TEXT NOT NULL DEFAULT '[]',
  confidence TEXT NOT NULL DEFAULT 'medium' CHECK (confidence IN ('high', 'medium', 'low')),
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'superseded')),
  created_at TEXT NOT NULL,
  reviewed_at TEXT,
  reviewer TEXT,
  review_note TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(repo_id, title, source_memory_ids_json)
);

CREATE INDEX IF NOT EXISTS idx_knowledge_candidates_status
ON knowledge_candidates(repo_id, status, created_at);

CREATE TABLE IF NOT EXISTS runtime_knowledge (
  knowledge_id TEXT PRIMARY KEY,
  candidate_id TEXT REFERENCES knowledge_candidates(candidate_id) ON DELETE SET NULL,
  repo_id TEXT NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
  scope_type TEXT NOT NULL CHECK (scope_type IN ('global', 'route', 'path', 'symbol', 'workstream')),
  scope_value TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL,
  prompt_text TEXT NOT NULL,
  retrieval_boost_json TEXT NOT NULL DEFAULT '{}',
  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
  approved_by TEXT NOT NULL,
  approved_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_runtime_knowledge_lookup
ON runtime_knowledge(repo_id, active, scope_type, scope_value);

INSERT OR IGNORE INTO schema_migrations(version, name, checksum, applied_at)
VALUES (1, 'robert-initial-schema', 'stage-3-schema', datetime('now'));
