# DBOS Durable Workflow Migration Implementation Plan

> Historical design, superseded by the SQLite state-machine removal. Do not use
> this plan for current deployment. See [upgrade instructions](../../sqlite-state-machine-upgrade.md).


> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:executing-plans` to implement this plan task by task. Repository
> policy prohibits subagents unless the user explicitly authorizes delegation;
> `superpowers:subagent-driven-development` must not be used by default. Steps
> use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the GitHub human-request and PR remediation orchestration in
Robert's polling loop with DBOS durable workflows, while preserving the deployed
Robert domain data, supervising already-running Workers to a safe terminal state,
and preventing duplicate dispatch, audit, or GitHub publication during cutover.

**Architecture:** Keep `robert.sqlite3` as the source of truth for repositories,
events, workstreams, tasks, attempts, Worker results, audit decisions,
remediation episodes, GitHub actions, and operator evidence. Add a separate DBOS
system database for workflow/step checkpoints. A durable binding assigns every
eligible Task or remediation Episode to exactly one owner (`legacy` or `dbos`).
DBOS workflow bodies contain only deterministic orchestration; all SQLite,
GitHub, process, filesystem, clock, audit, and publication operations execute in
idempotent DBOS steps backed by Robert's domain records. A transactional Robert
outbox bridges Worker/GitHub facts into DBOS messages, and publication intents
close the crash window around external side effects.

**Tech Stack:** Python 3.10+, `dbos>=2.29,<3`, Robert's existing SQLite domain
database, PostgreSQL for the deployed DBOS system database, SQLite DBOS system
databases only in local/unit tests, existing `gh` CLI and Git worktree adapters,
`unittest`, supervisor/systemd/launchd, and the existing Robert Web UI.

DBOS API/configuration references verified for this plan:

- <https://docs.dbos.dev/python/reference/configuration>
- <https://docs.dbos.dev/python/integrating-dbos>
- <https://docs.dbos.dev/python/tutorials/database-connection>
- <https://docs.dbos.dev/python/tutorials/workflow-management>
- <https://docs.dbos.dev/python/tutorials/testing>

## Global Constraints

- Preserve `robert.sqlite3`; this migration does not move or duplicate Robert's
  domain tables into DBOS.
- Use a separate DBOS system database. The deployed configuration points
  `ROBERT_DBOS_SYSTEM_DATABASE_URL` at PostgreSQL. Do not put the URL or database
  credentials in Robert YAML, fixtures, logs, diagnostics, or Web responses.
- Keep `workflow_engine.mode: legacy` as the upgrade-safe default. Package
  installation and schema initialization alone must not change runtime behavior.
- Only one engine may advance a non-terminal Task or remediation Episode. Every
  legacy and DBOS query must enforce `workflow_bindings.owner_engine` before
  mutating state. Legacy mode lazily creates a legacy binding before the first
  post-upgrade mutation; it never treats a missing row as mutable ownership.
- Do not transfer ownership of a running/prepared Attempt, an audited but
  unpublished GitHub action, or an ambiguous publication. Existing in-flight
  subjects remain `legacy` until they reach a safe terminal boundary.
- The first DBOS release covers `human_request`, `ci_remediation`, and
  `merge_conflict_remediation` subjects originating from GitHub. Web-originated
  local Tasks remain on the existing path and receive no new behavior.
- An additive GitHub event becomes pending context and never interrupts the
  current Attempt. An invalidating fact (PR merged/closed, head/base changed,
  branch missing, or permission lost) blocks publication, terminates the Worker,
  and supersedes the stale snapshot.
- Unknown process identity, remote publication state, current PR snapshot, or
  migration ownership fails closed: persist evidence, mark Operator Attention,
  and require an explicit safe resume.
- If the original Worker is alive and its PID identity is proven, keep
  supervising it. If it is dead and worktree progress is consistent, permit at
  most one automatic recovery Attempt with compact recovery evidence. Otherwise
  require Operator Attention.
- A Task completes only after its promised action is confirmed. A Workstream
  remains open until its PR is merged/closed or Robert records explicit no-further-
  action. A remediation Episode resolves only after fresh configured checks are
  green or the merge conflict is gone.
- DBOS workflow function names and control flow are versioned. `v1` functions
  remain immutable while a `v1` binding is open; behavior changes that alter the
  replay path require new `v2` functions and workflow IDs.
- Do not query DBOS internal tables. Runtime management and Web read models use
  supported DBOS/DBOSClient APIs.
- Do not enable the DBOS admin server. Robert's existing Web server remains the
  sole operator surface.
- Every behavior change starts with a failing `unittest`. Run the focused test,
  then the affected module tests, then the repository gates.

## Verified Current Boundaries

Plan baseline captured on 2026-08-02:

- repository revision: `fedbfa2` (`main`, equal to `origin/main`);
- verification: `.venv/bin/python -B -m unittest discover -s tests` passed all
  628 tests before this plan was written;
- current DBOS Python release checked for the plan: `2.29.0`, supporting Python
  3.10+ and `system_database_url` for PostgreSQL or SQLite;
- the existing reliability documents and prior PR self-healing plan were already
  untracked and must not be overwritten or staged incidentally during execution.

| Concern | Current owner | Migration treatment |
| --- | --- | --- |
| Domain persistence | `src/robert_agent/resources/db/schema.sql`, `storage.py` | Retain and add ownership/outbox/publication tables |
| Per-cycle orchestration | `daemon.py` -> `loop_engine.py` -> `run_once.py` | Route eligible subjects through DBOS; retain a fenced legacy drain |
| Worker dispatch/process | `dispatch.py`, supervision helpers in `run_once.py` | Wrap in idempotent steps and strengthen process identity |
| Worker result ingress | `worker/result.py` | Insert domain result and DBOS outbox signal in one SQLite transaction |
| Audit | `audit_result.py`, `_audit_completed_results` | Keep policy boundary; invoke once from a DBOS step |
| GitHub publication | `publish.py`, `github_actions` | Keep publisher; add durable intent and three-state reconciliation |
| Remediation state | `remediation.py`, `pr_remediation_episodes` | Keep domain state; make one durable workflow own each Episode |
| Operator read model | `web.py`, `workbench.py`, `status.py` | Join Robert facts with supported DBOS workflow/step metadata |
| Diagnostic cycle history | `agent_runs`, `run_steps`, `run_repo_steps` | Keep as legacy diagnostics during migration; do not treat as checkpoints |

## Target State and Ownership

```text
GitHub discovery / Worker result / timer
                 |
                 v
        Robert SQLite transaction
   domain fact + workflow_outbox row
                 |
                 v
        DBOS signal relay (retryable)
                 |
                 v
        DBOS v1 workflow by subject
                 |
          idempotent DBOS steps
                 |
   Robert domain transitions / Worker / GitHub
```

Robert stores what happened and what may happen. DBOS stores where deterministic
execution is checkpointed. `workflow_bindings` is the only ownership decision;
DBOS status is not copied into Tasks as a second lifecycle.

Deterministic workflow IDs:

- active human Task: `robert:v1:task:<task_id>`;
- active remediation Episode: `robert:v1:remediation:<episode_id>`;
- shadow observation: `robert:shadow:v1:<subject_type>:<subject_id>`.

An eligible human-request subject is a non-terminal GitHub-originated Task. An
eligible remediation subject is a non-terminal `pr_remediation_episode`; its
child remediation Tasks are controlled through the Episode binding and do not
receive a second independent workflow. Web-originated local Tasks are outside
this migration and retain their existing execution path.

## Configuration Contract

The public configuration gains one optional block:

```yaml
workflow_engine:
  mode: legacy  # legacy | shadow | active
  system_database_url_env: ROBERT_DBOS_SYSTEM_DATABASE_URL
```

Rules:

- absent block normalizes to `legacy` and preserves current behavior;
- unknown fields and unknown modes fail validation;
- `legacy` does not read the environment variable or initialize DBOS;
- `shadow` and `active` require the named environment variable;
- the environment value must be a PostgreSQL URL in a deployed run;
- unit tests construct a runtime directly with `sqlite:///...` and never depend
  on a developer PostgreSQL instance;
- diagnostics return the URL's scheme and availability only, never the URL.

## Robert Schema Additions

Add migration version `3`, name `robert-dbos-workflow-v1`, with a fixed checksum.
It creates the following tables without rewriting existing Task/Attempt rows:

```sql
CREATE TABLE IF NOT EXISTS workflow_bindings (
  subject_type TEXT NOT NULL
    CHECK (subject_type IN ('task', 'remediation_episode')),
  subject_id TEXT NOT NULL,
  workflow_kind TEXT NOT NULL
    CHECK (workflow_kind IN ('human_request', 'pr_remediation')),
  owner_engine TEXT NOT NULL
    CHECK (owner_engine IN ('legacy', 'dbos')),
  definition_version TEXT NOT NULL DEFAULT 'v1',
  workflow_id TEXT UNIQUE,
  shadow_workflow_id TEXT UNIQUE,
  binding_status TEXT NOT NULL
    CHECK (binding_status IN ('active', 'terminal', 'attention')),
  recovery_attempts INTEGER NOT NULL DEFAULT 0
    CHECK (recovery_attempts BETWEEN 0 AND 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  terminal_at TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (subject_type, subject_id),
  CHECK ((owner_engine = 'legacy' AND workflow_id IS NULL)
      OR (owner_engine = 'dbos' AND workflow_id IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS workflow_outbox (
  signal_id TEXT PRIMARY KEY,
  workflow_id TEXT NOT NULL,
  topic TEXT NOT NULL,
  dedupe_key TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'delivered', 'attention')),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  not_before_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  delivered_at TEXT,
  last_error_json TEXT,
  UNIQUE (workflow_id, topic, dedupe_key)
);

CREATE TABLE IF NOT EXISTS workflow_signal_receipts (
  signal_id TEXT PRIMARY KEY,
  workflow_id TEXT NOT NULL,
  topic TEXT NOT NULL,
  consumed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_shadow_observations (
  observation_id TEXT PRIMARY KEY,
  subject_type TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  legacy_decision_json TEXT NOT NULL,
  shadow_decision_json TEXT NOT NULL,
  matched INTEGER NOT NULL CHECK (matched IN (0, 1)),
  mismatch_code TEXT,
  created_at TEXT NOT NULL
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
```

Create indexes for active bindings, pending outbox rows by `not_before_at`, and
shadow mismatches by creation time. Foreign keys cannot cover both subject types;
`workflow_store.bind_subject` must validate the referenced Task/Episode in the
same transaction.

---

### Task 1: Add the DBOS Dependency and Upgrade-Safe Configuration

**Files:**

- Modify: `pyproject.toml`
- Modify: `src/robert_agent/resources/schemas/config.schema.json`
- Modify: `src/robert_agent/resources/config.example.yml`
- Modify: `src/robert_agent/init_config.py`
- Modify: `src/robert_agent/validate_config.py`
- Modify: `tests/test_config_and_schema.py`
- Modify: `tests/test_init_and_doctor.py`
- Modify: `tests/test_distribution.py`

**Interfaces:**

- `_normalize_workflow_engine(config, environ=None) -> dict`
- validated result key `workflow_engine` with `mode`,
  `system_database_url_env`, `system_database_scheme`, and `configured`

- [ ] Add tests proving an absent block normalizes to `legacy`, invalid modes and
  unknown keys fail, and legacy mode does not require an environment variable.
- [ ] Add tests proving shadow/active fail closed when the named environment
  variable is absent, accept PostgreSQL, and redact the full URL from all result
  payloads.
- [ ] Run the focused tests and confirm they fail because workflow-engine
  normalization/validation is absent:
  `.venv/bin/python -B -m unittest tests.test_config_and_schema tests.test_init_and_doctor`.
- [ ] Add `dbos>=2.29,<3`. Document beside the dependency that the standard
  library/current dependencies do not provide durable replay, step checkpoints,
  or workflow messaging.
- [ ] Implement normalization and schema validation without importing or
  launching DBOS from `validate_config.py`.
- [ ] Add the block to generated/example config with mode `legacy`.
- [ ] Run `.venv/bin/python -B -m unittest tests.test_config_and_schema tests.test_init_and_doctor tests.test_distribution`.
- [ ] Commit: `feat(workflow): add upgrade-safe DBOS configuration`.

### Task 2: Add Workflow Ownership, Outbox, and Publication Storage

**Files:**

- Modify: `src/robert_agent/resources/db/schema.sql`
- Modify: `src/robert_agent/storage.py`
- Create: `src/robert_agent/workflow_store.py`
- Create: `tests/test_workflow_store.py`
- Modify: `tests/test_config_and_schema.py`
- Modify: `tests/test_migration.py`

**Interfaces:**

- `bind_subject(conn, subject_type, subject_id, workflow_kind, owner_engine, now) -> dict`
- `ensure_legacy_binding(conn, subject_type, subject_id, workflow_kind, now) -> dict`
- `get_binding(conn, subject_type, subject_id) -> dict | None`
- `assert_owner(conn, subject_type, subject_id, expected_engine) -> None`
- `close_binding(...)`, `mark_binding_attention(...)`
- `enqueue_signal(...)`, `list_due_signals(...)`, `mark_signal_delivered(...)`
- `consume_signal_once(...) -> bool`
- `prepare_publication_intent(...)`, `transition_publication_intent(...)`

- [ ] Write schema/idempotency tests for all five tables, constraints, indexes,
  and migration checksum behavior.
- [ ] Write behavior tests proving repeated binding/outbox/publication calls
  return the same row, conflicting owners fail, and a duplicate signal receipt
  returns `False` without applying a transition twice.
- [ ] Add tests that reject `task + pr_remediation` and
  `remediation_episode + human_request` binding combinations so remediation child
  Tasks cannot acquire a second workflow owner.
- [ ] Run `.venv/bin/python -B -m unittest tests.test_workflow_store tests.test_migration`; confirm failure before implementation.
- [ ] Add the schema and migration version 3. Do not alter existing lifecycle
  values or backfill bindings from `storage.init_database`.
- [ ] Implement the store helpers with caller-owned SQLite transactions so
  domain mutation and outbox insert can commit atomically.
- [ ] Run `.venv/bin/python -B -m unittest tests.test_workflow_store tests.test_migration tests.test_config_and_schema`.
- [ ] Commit: `feat(workflow): add durable ownership and signal storage`.

### Task 3: Establish Subject Snapshots and the Legacy Ownership Fence

**Files:**

- Create: `src/robert_agent/workflow_operations.py`
- Modify: `src/robert_agent/run_once.py`
- Modify: `tests/test_run_once.py`
- Create: `tests/test_workflow_operations.py`

**Interfaces:**

- `load_subject_snapshot(db_path, subject_type, subject_id) -> dict`
- `claim_legacy_subject(conn, subject_type, subject_id, *, now) -> dict | None`
- `require_subject_owner(conn, subject_type, subject_id, expected_owner_engine) -> dict`
- later tasks add `prepare_task`, `dispatch_task`, `supervise_attempt`,
  `audit_task_result`, `publish_task_result`, `finalize_task`, and
  `reconcile_remediation` only when their complete DBOS step contract is
  implemented; do not add proxy-only placeholders in this task.

- [ ] Add characterization tests for task/remediation snapshots, atomic legacy
  claims, a conflicting DBOS binding, and terminal/attention bindings.
- [ ] Run the new tests and prove they fail because the operations module does not
  exist.
- [ ] Add run-once tests proving a DBOS-owned prepared/running/completed Task is
  not dispatched, supervised, audited, or finalized by the legacy path, and an
  unbound eligible Task is bound legacy before its first such mutation.
- [ ] Add remediation tests proving a DBOS-owned Episode does not produce a
  legacy repair Task and an unbound eligible Episode is bound legacy before a
  legacy repair decision is materialized.
- [ ] Fence the existing batch mutation/external-call boundaries by lazily
  claiming unbound eligible subjects as legacy and skipping DBOS/attention/
  terminal bindings. Recheck ownership in the transaction immediately before
  Worker dispatch. Close a binding in the same transaction as its terminal
  Task/Episode transition.
- [ ] Preserve the existing trust, route, worktree, audit, redaction, publisher,
  output-shape, and repo-level diagnostic owners. Do not move the batch pipeline
  or introduce imports from `workflow_operations.py` back into `run_once.py`.
- [ ] Keep existing output shapes stable so `loop_engine.py`, daemon diagnostics,
  and current tests do not change behavior.
- [ ] Run `.venv/bin/python -B -m unittest tests.test_workflow_operations tests.test_run_once tests.test_workflow_matrix tests.test_pr_remediation_integration`.
- [ ] Commit: `feat(workflow): fence legacy subject ownership`.

### Task 4: Initialize DBOS Once Per Long-Lived Robert Process

**Files:**

- Create: `src/robert_agent/dbos_runtime.py`
- Create: `src/robert_agent/durable_workflows.py`
- Create: `tests/test_dbos_runtime.py`
- Modify: `src/robert_agent/daemon.py`
- Modify: `src/robert_agent/doctor.py`
- Modify: `src/robert_agent/cli/main.py`
- Modify: `tests/test_init_and_doctor.py`

**Interfaces:**

- `configure_runtime(config_result) -> DBOSRuntime`
- `launch_runtime(runtime) -> None`
- `destroy_runtime() -> None` for tests/process shutdown
- `start_bound_workflow(binding) -> str`
- registered `human_request_workflow_v1(task_id: str) -> dict`
- registered `pr_remediation_workflow_v1(episode_id: str) -> dict`

- [ ] Create a `unittest` DBOS fixture that uses one temporary
  `sqlite:///.../dbos-test.sqlite3`, calls `DBOS.reset_system_database()`, launches
  once, and destroys after each test.
- [ ] Add tests for one-time initialization, deterministic workflow IDs via
  `SetWorkflowID`, same-ID start idempotency, and admin-server disabled config.
- [ ] Run `.venv/bin/python -B -m unittest tests.test_dbos_runtime`; confirm failure.
- [ ] Configure DBOS with `name="robert"`, `system_database_url`,
  `run_admin_server=False`, and `use_listen_notify=False` only for SQLite tests.
- [ ] Register versioned workflow skeletons before `DBOS.launch()`. Skeletons may
  load/return a read-only subject snapshot but must not dispatch or publish yet.
- [ ] Launch DBOS in the daemon process for shadow/active mode; keep legacy mode
  free of DBOS initialization. Ensure SIGTERM destroys DBOS only after the daemon
  stops accepting work.
- [ ] Add a Doctor connectivity check that initializes a DBOS client/runtime only
  for shadow/active, reports `passed|failed`, and never includes the URL.
- [ ] Run `.venv/bin/python -B -m unittest tests.test_dbos_runtime tests.test_daemon tests.test_cli tests.test_init_and_doctor`.
- [ ] Commit: `feat(workflow): initialize the DBOS runtime`.

### Task 5: Deliver Transactional Signals from Robert to DBOS

**Files:**

- Create: `src/robert_agent/workflow_signals.py`
- Create: `tests/test_workflow_signals.py`
- Modify: `src/robert_agent/worker/result.py`
- Modify: `src/robert_agent/run_once.py`
- Modify: `src/robert_agent/daemon.py`
- Modify: `src/robert_agent/loop_engine.py`
- Modify: `src/robert_agent/workflow_store.py`

**Interfaces:**

- `record_subject_signal(conn, binding, topic, dedupe_key, payload, now) -> dict`
- `relay_due_signals(db_path, dbos_sender, *, limit, now) -> dict`
- DBOS topic `subject-events`
- message shape: `signal_id`, `topic`, `subject_type`, `subject_id`, `fact_id`

- [ ] Add a failing test proving Worker result insertion and outbox insertion are
  in one transaction; forced rollback must leave neither row.
- [ ] Add relay tests for success, transient DBOS send failure, crash-equivalent
  duplicate send before delivered marking, receipt dedupe, and retry backoff.
- [ ] Add tests proving a Worker result queues one signal only when its Task is
  DBOS-owned; legacy bindings do not produce an outbox row. GitHub/remediation
  fact integration is added at the event-classifier/Episode workflow boundaries
  in Tasks 8 and 10, where the authoritative subject is known.
- [ ] Run `.venv/bin/python -B -m unittest tests.test_workflow_signals tests.test_worker_protocol tests.test_discovery_authorization`; confirm failure.
- [ ] Insert the outbox row from `record_result` after validating the result and
  before its existing transaction commits. The message contains IDs only; the
  workflow reloads the authoritative row from Robert SQLite.
- [ ] Relay in the DBOS-initialized daemon process with
  `DBOS.send(workflow_id, message, topic="subject-events",
  idempotency_key=signal_id)`; do not attempt to use the daemon's in-process DBOS
  singleton from the `loop_engine.py` child process. Mark delivered only after
  send returns. Treat crash-equivalent duplicate sends as expected and consume
  by `signal_id` once inside an idempotent step.
- [ ] On a transient send failure, keep the row pending, increment
  `attempt_count`, persist only a redacted exception class, and set deterministic
  capped exponential `not_before_at`; ordinary transport failures must not
  become Operator Attention. Permanent local validation conflicts fail closed.
- [ ] Make due pending outbox rows count as runnable local work. The daemon
  relays them before deciding whether to launch a legacy local-drain child and
  before sleeping.
- [ ] Run `.venv/bin/python -B -m unittest tests.test_workflow_signals tests.test_worker_protocol tests.test_discovery_authorization tests.test_daemon tests.test_loop_engine`.
- [ ] Commit: `feat(workflow): bridge Robert facts to DBOS workflows`.

### Task 6: Make Worker Dispatch and Supervision Recoverable

**Files:**

- Create: `src/robert_agent/worker_identity.py`
- Modify: `src/robert_agent/dispatch.py`
- Modify: `src/robert_agent/workflow_operations.py`
- Modify: `src/robert_agent/run_once.py`
- Create: `tests/test_worker_identity.py`
- Create: `tests/test_worker_lifecycle_workflow.py`
- Modify: `tests/test_operational_commands.py`
- Modify: `tests/test_run_once.py`

**Interfaces:**

- dispatch metadata adds `pid`, `process_group_id`, `process_start_token`, and
  `command_fingerprint`
- `inspect_worker_identity(db_path, attempt_id) -> alive_same | dead | unknown`
- `terminate_worker(db_path, attempt_id) -> terminated | already_dead | attention`
- `classify_recovery(conn, subject_type, subject_id, attempt_id, evidence,
  *, now) -> continue | recover_once | attention`

- [ ] Add failing tests that reject PID reuse/mismatched start token, preserve a
  proven live Worker, terminate the recorded process group, and fail closed when
  identity cannot be read.
- [ ] Add workflow-operation tests proving a replay after dispatch creates no
  second Attempt while the original Worker identity is alive. Full DBOS
  crash/relaunch replay is exercised in Task 7 after the workflow loop exists.
- [ ] Add tests that a dead Worker with consistent Git progress creates exactly
  one recovery Attempt and that a second death becomes Operator Attention.
- [ ] Run `.venv/bin/python -B -m unittest tests.test_worker_lifecycle_workflow tests.test_operational_commands tests.test_run_once`; confirm failure.
- [ ] Record Linux `/proc/<pid>/stat` start time or the platform `ps` start token,
  process group, and hash of the launch command at dispatch. Never identify a
  process by PID alone.
- [ ] Replace single-PID SIGTERM with verified process-group SIGTERM, bounded
  confirmation, then SIGKILL only when the same identity remains alive. Persist
  every outcome in Attempt metadata/notifications.
- [ ] Centralize `/proc/<pid>/stat` or `ps` identity reads in
  `worker_identity.py`; `run_once.py` and DBOS-facing operations must call the
  same implementation. A PID without a matching start token, process group, and
  command fingerprint is `unknown`, never alive-same.
- [ ] Reuse the existing compact worktree/log recovery evidence and increment
  `workflow_bindings.recovery_attempts` atomically before creating the sole
  recovery Attempt. The second recovery request marks binding Attention and
  creates no Attempt.
- [ ] Keep durable wait/recv control flow in Task 7, which composes these proven
  identity and recovery operations rather than duplicating them.
- [ ] Run `.venv/bin/python -B -m unittest tests.test_worker_lifecycle_workflow tests.test_operational_commands tests.test_run_once tests.test_live_worker_acceptance`.
- [ ] Commit: `feat(workflow): make Worker supervision crash recoverable`.

### Task 7: Implement the Human-Request Workflow v1

**Files:**

- Modify: `src/robert_agent/durable_workflows.py`
- Modify: `src/robert_agent/workflow_operations.py`
- Create: `tests/test_human_request_workflow.py`
- Modify: `tests/test_workflow_matrix.py`
- Modify: `tests/test_board_control_integration.py`

**Workflow loop:**

1. Assert the Task's DBOS ownership and load its exact source/snapshot.
2. Prepare route, prompt, and worktree in a step.
3. Dispatch/supervise until a valid Worker result or attention state.
4. Audit the result in a step.
5. Publish/reconcile every promised action in a step.
6. Mark the Task complete only after confirmation.
7. Consume additive pending events and create the next Task; close the Workstream
   only when its terminal condition is independently observed.

Task 7 establishes the durable controller and a narrow versioned
`HumanRequestDriver` transition contract. Task 11 supplies the production
driver adapter to the existing route/dispatch/audit/publish implementation when
active ownership is enabled. This keeps the workflow body deterministic and
avoids importing `run_once.py` private helpers into DBOS definitions; active
routing is not enabled before that adapter and Task 9 publication reconciliation
are both complete.

- [ ] Add failing end-to-end unit tests for comment-only, new PR, existing PR
  update, waiting-for-user, additive follow-up, rejected result, and closed/merged
  invalidation paths.
- [ ] Assert exactly one Attempt per dispatch decision, one audit record per
  result, and no Task completion before required GitHub actions are published.
- [ ] Run `.venv/bin/python -B -m unittest tests.test_human_request_workflow`; confirm failure.
- [ ] Implement `human_request_workflow_v1` as deterministic control flow over
  step return values. Do not read time, SQLite, files, process state, or GitHub in
  the workflow body.
- [ ] Define a strict transition result (`transition_id`, `operation`,
  `wait_seconds`, `terminal`, redacted state summary) and reject malformed,
  repeated-without-progress, or out-of-order driver results. Tests in this unit
  use a deterministic fake driver; Task 11 must pass the same tests through the
  production adapter before `active` mode can bind a new subject.
- [ ] Take `wait_seconds` from the recorded `next_transition` step result, then
  call `DBOS.recv("subject-events", timeout_seconds=wait_seconds)` for durable
  waits; pass the received `signal_id` to a step that deduplicates and reloads
  the domain fact.
- [ ] Keep additive events as `task_events.relationship='pending'`; process them
  after the current Task reaches its promised-action boundary.
- [ ] Run `.venv/bin/python -B -m unittest tests.test_human_request_workflow tests.test_workflow_matrix tests.test_board_control_integration tests.test_run_once`.
- [ ] Commit: `feat(workflow): add durable human request execution`.

### Task 8: Implement PR Remediation Workflow v1

**Files:**

- Modify: `src/robert_agent/durable_workflows.py`
- Modify: `src/robert_agent/workflow_operations.py`
- Modify: `src/robert_agent/remediation.py`
- Create: `tests/test_remediation_workflow.py`
- Modify: `tests/test_remediation.py`
- Modify: `tests/test_pr_remediation_integration.py`

**Workflow loop:**

1. Bind one workflow to one Episode snapshot `(PR, head_sha, base_sha)`.
2. Enforce attempt/time/token/cost budgets before dispatch.
3. Run the Worker lifecycle and audit/publish sequence.
4. Enter `waiting_for_signal` after a confirmed push.
5. Wait for fresh PR health; resolve only on configured green checks or a
   conflict-free result head.
6. Supersede on head/base movement and exhaust on non-recoverable/unknown state.

As with Task 7, Task 8 establishes the versioned deterministic controller and
`RemediationDriver` contract. Task 11 provides the exact-Episode production
adapter over existing remediation transitions and Worker/publisher operations;
active Episode routing remains disabled until that adapter and Task 9 are
accepted.

- [ ] Add failing tests for CI repair success, still-failing CI, conflict repair,
  stale snapshot, budget exhaustion, missing usage, missing failure evidence,
  and concurrent human pending context.
- [ ] Add a test proving a confirmed push does not resolve the Episode before a
  fresh health observation.
- [ ] Run `.venv/bin/python -B -m unittest tests.test_remediation_workflow tests.test_remediation`; confirm failure.
- [ ] Implement `pr_remediation_workflow_v1` using existing episode transitions
  and attestation/publish guards; do not copy remediation state into DBOS custom
  tables.
- [ ] Make budget, snapshot, attempt, audit, publication, and fresh-health
  observations explicit versioned driver transitions. Workflow control flow may
  branch only on those recorded results; it must not read the Episode, clock,
  usage, process, worktree, or GitHub directly.
- [ ] Resolve/exhaust/supersede the Episode and binding in one Robert transaction.
- [ ] Run `.venv/bin/python -B -m unittest tests.test_remediation_workflow tests.test_remediation tests.test_pr_remediation_integration tests.test_run_once`.
- [ ] Commit: `feat(workflow): add durable PR remediation execution`.

### Task 9: Close the External Publication Crash Window

**Files:**

- Create: `src/robert_agent/publication_reconcile.py`
- Modify: `src/robert_agent/publish.py`
- Modify: `src/robert_agent/workflow_operations.py`
- Create: `tests/test_publication_reconcile.py`
- Modify: `tests/test_operational_commands.py`
- Modify: `tests/test_publish_dedupe_acceptance.py`

**Three-state contract:**

- remote confirms effect: backfill `github_actions.publish_status='published'`
  and persist external evidence;
- remote confirms absence and current action scope remains valid: change intent
  to `confirmed_absent` and safely retry;
- remote is unknown/conflicting or query fails: mark intent/binding Attention and
  do not retry automatically.

- [ ] Add failing tests for a simulated crash after comment creation, PR creation,
  new-branch push, and existing-PR push but before local published marking.
- [ ] Add tests for confirmed absent, conflicting marker/owner/head, remote query
  failure, and stale action scope.
- [ ] Run `.venv/bin/python -B -m unittest tests.test_publication_reconcile tests.test_publish_dedupe_acceptance`; confirm failure.
- [ ] Create the publication intent before the external call and set it to
  `executing` in a committed transaction. Derive idempotency key and request
  fingerprint from the audited action scope, not Worker text.
- [ ] On every step retry, reconcile an `executing` intent before any write. Reuse
  existing hidden comment markers, PR head/owner checks, and push SHA/lease guards.
- [ ] Persist redacted remote evidence and create one
  `workflow_attention_required` notification for unknown/conflicting state.
- [ ] Run `.venv/bin/python -B -m unittest tests.test_publication_reconcile tests.test_operational_commands tests.test_publish_dedupe_acceptance tests.test_production_write_canary`.
- [ ] Commit: `feat(publish): reconcile ambiguous external side effects`.

### Task 10: Classify Additive and Invalidating Events at the Workflow Boundary

**Files:**

- Create: `src/robert_agent/workflow_events.py`
- Modify: `src/robert_agent/run_once.py`
- Modify: `src/robert_agent/workflow_operations.py`
- Modify: `src/robert_agent/durable_workflows.py`
- Create: `tests/test_workflow_events.py`
- Modify: `tests/test_discovery_authorization.py`
- Modify: `tests/test_github_event_matrix.py`

**Interfaces:**

- `classify_subject_fact(fact) -> additive | invalidating | terminal | irrelevant`
- invalidation evidence includes exact old/new head/base, source state, permission
  decision, and fact ID

- [ ] Add a table-driven failing test for comments/review feedback (additive), PR
  close/merge, head/base change, branch disappearance, permission revocation
  (invalidating), and unrelated events.
- [ ] Add a race test where invalidation arrives between audit and publish; the
  publisher must recheck the binding/snapshot and perform no write.
- [ ] Run `.venv/bin/python -B -m unittest tests.test_workflow_events tests.test_github_event_matrix`; confirm failure.
- [ ] Centralize classification and call it from both legacy and DBOS paths.
- [ ] For invalidation, atomically record evidence, mark binding Attention or
  terminal as appropriate, and signal the workflow. Terminate only a proven
  matching Worker identity.
- [ ] Run `.venv/bin/python -B -m unittest tests.test_workflow_events tests.test_discovery_authorization tests.test_github_event_matrix tests.test_workflow_matrix tests.test_run_once`.
- [ ] Commit: `feat(workflow): enforce additive and invalidating event semantics`.

### Task 11: Add Shadow Comparison and Active Routing to the Daemon

**Files:**

- Modify: `src/robert_agent/daemon.py`
- Modify: `src/robert_agent/loop_engine.py`
- Modify: `src/robert_agent/run_once.py`
- Modify: `src/robert_agent/workflow_store.py`
- Create: `src/robert_agent/workflow_router.py`
- Create: `tests/test_workflow_router.py`
- Modify: `tests/test_daemon.py`
- Modify: `tests/test_loop_engine.py`

**Mode behavior:**

- `legacy`: current owner and execution path only;
- `shadow`: legacy owns side effects; a read-only shadow workflow records the next
  transition comparison for every eligible new subject;
- `active`: new eligible GitHub Tasks/Episodes bind to DBOS; existing legacy
  bindings drain on the fenced path; Web-originated local Tasks remain legacy.

- [ ] Add failing tests for every mode, DBOS start failure, duplicate daemon
  cycles, legacy drain, local Web Task exclusion, and shadow mismatch recording.
- [ ] Run `.venv/bin/python -B -m unittest tests.test_workflow_router tests.test_daemon tests.test_loop_engine`; confirm failure.
- [ ] Bind a subject in the same transaction that creates/activates it. Never
  infer ownership from configuration after a binding exists.
- [ ] In shadow mode, run only read-only snapshot/decision steps and store
  normalized legacy/shadow decisions. Any attempted dispatch/audit/publish from a
  shadow ID must raise and mark the observation mismatched.
- [ ] In active mode, start the deterministic DBOS workflow after its binding
  commits. A crash between commit/start is repaired by a daemon reconciliation
  pass that starts missing bound workflow IDs idempotently.
- [ ] Implement and configure the production `HumanRequestDriver` adapter over
  the existing preparation, dispatch, supervision, audit, publish, finalize,
  waiting-for-user, and pending-event logic. It must accept an expected DBOS
  owner and an exact Task id; it must not temporarily relabel a binding as
  legacy or run unrelated subjects.
- [ ] Keep live GitHub discovery in the daemon initially; replace only subject
  progression, not event collection/rate-limit policy.
- [ ] Run `.venv/bin/python -B -m unittest tests.test_workflow_router tests.test_daemon tests.test_loop_engine tests.test_run_once tests.test_workflow_matrix`.
- [ ] Commit: `feat(workflow): route eligible subjects through DBOS`.

### Task 12: Integrate DBOS Execution State into `web.py`, Status, and Diagnostics

**Files:**

- Create: `src/robert_agent/workflow_read_model.py`
- Modify: `src/robert_agent/web.py`
- Modify: `src/robert_agent/workbench.py`
- Modify: `src/robert_agent/status.py`
- Modify: `src/robert_agent/diagnostics.py`
- Modify: `src/robert_agent/web_assets/workbench.js`
- Modify: `src/robert_agent/web_assets/workbench.css`
- Create: `tests/test_workflow_read_model.py`
- Modify: `tests/test_operational_commands.py`
- Modify: `tests/test_workbench.py`

**Interfaces:**

- `WorkflowReader.list_recent(limit) -> list[dict]`
- `WorkflowReader.get_detail(workflow_id) -> dict | None`
- `build_dashboard_data(db_path, history_limit=20, workflow_reader=None)`
- endpoints `GET /api/workflows` and `GET /api/workflows/<workflow_id>`

- [ ] Add tests with a fake reader proving dashboard rows join binding/subject
  data with DBOS status/steps and that reader failure yields
  `workflow_engine.status='unavailable'` without breaking the domain dashboard.
- [ ] Add security tests proving no database URL, step input secrets, raw Worker
  prompt, or DBOS internal table data is returned.
- [ ] Run `.venv/bin/python -B -m unittest tests.test_workflow_read_model tests.test_operational_commands tests.test_workbench`; confirm failure.
- [ ] Implement the reader using supported `DBOS.list_workflows`,
  `DBOS.list_workflow_steps`, or `DBOSClient` methods. Query only workflow IDs
  present in Robert bindings.
- [ ] Show owner engine, definition version, workflow status, current/last step,
  recovery count, pending signal count, and shadow mismatch/attention evidence.
- [ ] Keep `--db` read-only Web mode functional with workflow details marked
  unavailable; config-backed Web mode constructs the reader.
- [ ] Extend status/diagnostics with counts and redacted connectivity only.
- [ ] Run `.venv/bin/python -B -m unittest tests.test_workflow_read_model tests.test_operational_commands tests.test_workbench tests.test_web_security tests.test_diagnostics`.
- [ ] Commit: `feat(web): expose durable workflow execution state`.

### Task 13: Build the Deployed-State Migration and Rollback CLI

**Files:**

- Create: `src/robert_agent/workflow_migration.py`
- Modify: `src/robert_agent/cli/main.py`
- Create: `tests/test_workflow_migration.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_migration.py`
- Create: `docs/guides/dbos-migration.md`

**Commands:**

```text
robert workflow migrate plan --config <path> --output <manifest.json>
robert workflow migrate apply --config <path> --manifest <manifest.json>
robert workflow migrate verify --config <path> --manifest <manifest.json>
robert workflow rollback --config <path> --manifest <manifest.json>
robert workflow legacy-status --config <path>
```

**Manifest:** schema version, Robert database path hash, DBOS database scheme,
source revision, generated time, subject IDs/current lifecycles, assigned owner,
reason code, current Attempt/action IDs, and a SHA-256 checksum. It contains no
credentials, prompt text, result handoff, or GitHub token.

- [ ] Add failing planner tests for terminal subjects, running/prepared Attempts,
  waiting-for-user Tasks, audited-unpublished actions, ambiguous publication
  intents, remediation waiting-for-signal, and new idle subjects.
- [ ] Assert all non-terminal deployed subjects bind `legacy`; the migrator never
  adopts an in-flight subject. A running/prepared Attempt is additionally a
  `drain_required` apply blocker because pre-upgrade PID-only metadata cannot be
  upgraded into a proven process identity safely. Waiting-for-user and other
  quiescent legacy subjects may remain open. New subjects after active cutover
  bind DBOS.
- [ ] Add apply tests for manifest checksum/database mismatch, changed subject
  state, repeated apply, DBOS connectivity failure, and transaction rollback.
- [ ] Define rollback as `active -> shadow`, not `active -> legacy`: shadow keeps
  the DBOS runtime available so already-open DBOS bindings can drain, while new
  subjects bind legacy. Add tests requiring the rollback config to be shadow,
  proving already-open DBOS bindings remain DBOS-owned until terminal or
  explicit operator cancellation, and rejecting direct legacy mode while a
  non-terminal DBOS binding exists. Never hand an open DBOS subject to legacy
  automatically.
- [ ] Run `.venv/bin/python -B -m unittest tests.test_workflow_migration tests.test_cli tests.test_migration`; confirm failure.
- [ ] Implement read-only `plan`, transactional/idempotent `apply`, independent
  `verify`, and fenced `rollback`. `legacy-status` reports
  `safe_to_disable_dbos=true` only when no non-terminal DBOS binding or pending
  DBOS outbox row remains; only then may the operator change shadow to legacy.
- [ ] Write the exact operator runbook below, including an intake pause and
  running/prepared Worker drain gate before replacing the binary, backup,
  stop/start, verification gates, and restore commands appropriate to Robert
  rather than generic DBOS examples. Do not synthesize identity metadata for a
  Worker launched by the old binary.
- [ ] Run `.venv/bin/python -B -m unittest tests.test_workflow_migration tests.test_cli tests.test_migration`.
- [ ] Commit: `feat(workflow): add deployed DBOS migration tooling`.

### Task 14: Prove Fault Recovery and Workflow Closure End to End

**Files:**

- Create: `tests/test_dbos_fault_injection.py`
- Modify: `tests/test_controlled_e2e_acceptance.py`
- Modify: `tests/test_acceptance_suite.py`
- Modify: `src/robert_agent/resources/acceptance-matrix.md`
- Modify: `docs/reliability-acceptance.md`
- Modify: `docs/reliability-acceptance.zh-CN.md`
- Modify: `docs/reference.md`

**Fault matrix:**

- crash before/after binding commit and before workflow start;
- crash before/after Worker dispatch returns;
- daemon restart with original Worker alive;
- dead Worker with/without consistent progress and second recovery failure;
- duplicate/out-of-order Worker result signal;
- additive event during running Attempt;
- invalidating event before dispatch, during Worker run, after audit, and before
  publication;
- crash after each GitHub external effect but before local confirmation;
- PostgreSQL unavailable, Robert SQLite busy, GitHub query failure, and DBOS
  message delivery failure;
- remediation push followed by failing, green, moved-head, and missing checks.

- [ ] Write the matrix as table-driven `unittest` cases using `subTest`, fake
  clock/process, a SQLite DBOS runtime, fake GitHub runner, and explicit restart
  boundaries.
- [ ] Assert invariants after every fault: one owner, at most one live Attempt per
  Task, at most one automatic recovery, no unaudited publication, no duplicate
  external effect, no false Task/Episode closure, and durable attention evidence.
- [ ] Run the focused suite and confirm each fault test fails before its relevant
  implementation task is complete.
- [ ] Add the DBOS scenarios to the acceptance runner without making PostgreSQL or
  network access a unit-test prerequisite.
- [ ] Update reliability docs to distinguish domain persistence, DBOS execution
  persistence, signal delivery, publication reconciliation, Task completion,
  Workstream closure, and Episode closure.
- [ ] Run `.venv/bin/python -B -m unittest tests.test_dbos_fault_injection tests.test_controlled_e2e_acceptance tests.test_acceptance_suite`.
- [ ] Commit: `test(workflow): verify DBOS recovery and closure invariants`.

### Task 15: Run Full Gates and Publish the Cutover Runbook

**Files:**

- Modify: `README.md`
- Modify: `docs/human-install.md`
- Modify: `docs/agent-install.md`
- Modify: `docs/development.md`
- Modify: `docs/guides.md`
- Modify: `src/robert_agent/resources/command-matrix.md`

- [ ] Document the dependency reason, PostgreSQL requirement for deployed DBOS,
  environment variable, legacy/shadow/active modes, Web integration, backup,
  migration, rollback, and no-mid-flight-adoption rule.
- [ ] Run `.venv/bin/python -B -m unittest discover -s tests`.
- [ ] Run `.venv/bin/python -m build` because dependency/package resources changed.
- [ ] Run `.venv/bin/python -m ruff check src tests`.
- [ ] Run `git diff --check`.
- [ ] Inspect `git status --short` and stage only files from this plan; preserve
  pre-existing untracked documentation.
- [ ] Perform the required refactor audit: list moved/renamed/extracted/deleted
  symbols, run production/test call-site searches, remove proxy-only/dead code,
  check layer/import rules, and state the result in the implementation handoff.
- [ ] Commit: `docs(workflow): publish DBOS migration and operations guide`.

## Deployed Cutover Runbook Required by Task 13

The implementation runbook must use these gates and order:

1. Provision PostgreSQL and set `ROBERT_DBOS_SYSTEM_DATABASE_URL` in the Robert
   service/container environment. Keep `robert.sqlite3`, artifacts, worktrees,
   and logs on the existing persistent volume.
2. Stop Robert discovery/daemon cleanly. Do not kill detached Workers.
3. Record `robert status --output json`, running process identities, active
   Attempts, pending/audited GitHub actions, open remediation Episodes, and the
   current package/revision.
4. Back up Robert SQLite with the SQLite online backup API or `.backup`; take a
   PostgreSQL backup after DBOS schema initialization; copy the generated
   migration manifest and verify all checksums. Copying only the SQLite WAL main
   file is not an acceptable backup.
5. Upgrade the package. Run `robert config validate`, `robert doctor`, and
   `robert workflow migrate plan`. Review that every non-terminal existing
   subject is `legacy` and that no credentials appear in the manifest.
6. Run `robert workflow migrate apply` and `verify` while the daemon is stopped.
   Start with `workflow_engine.mode: shadow`.
7. Restart Robert. Confirm the original Workers retain matching start tokens and
   are supervised by their legacy bindings. Any unknown identity goes to
   Operator Attention; it is not redispatched.
8. Hold shadow mode for at least 24 continuous hours and 20 eligible transition
   decisions, including controlled human-request, CI-remediation, and conflict-
   remediation cases. Require zero side effects from shadow IDs, zero ownership
   violations, zero unexplained decision mismatches, no outbox row pending beyond
   its retry schedule, and all pre-existing Workers/ambiguous actions at a safe
   terminal or attention state.
9. Stop the daemon, change mode to `active`, validate/doctor, and restart. New
   eligible GitHub Tasks/Episodes now bind DBOS; existing legacy subjects drain.
10. Verify one controlled human-request workflow and one controlled remediation
    workflow through confirmed action/health closure. Confirm the Web UI displays
    Robert domain state and DBOS steps without querying DBOS internal tables.
11. If new-subject routing must be rolled back, stop the daemon and run
    `robert workflow rollback`. This changes the default for future subjects only.
    Open DBOS bindings stay DBOS-owned and may be allowed to finish or explicitly
    canceled after operator inspection.
12. Restore Robert SQLite or the DBOS PostgreSQL backup only when the service is
    stopped and only with an operator reconciliation of workflows/external
    effects created after the backup. Never restore one database independently
    and restart blindly, because that can forget ownership or publication
    evidence while the other database or GitHub still records the effect.

## Release and Legacy Removal Gate

Do not delete `run_once.py`, `loop_engine.py`, `agent_runs`, `run_steps`, or
`run_repo_steps` in the migration release. They are required to drain deployed
legacy subjects and to retain current diagnostics. A later, separately reviewed
cleanup may remove only the GitHub/remediation legacy progression branches after:

- `robert workflow legacy-status` reports zero non-terminal GitHub/remediation
  bindings for seven consecutive days;
- no rollback to legacy is planned;
- Web-originated local Tasks have an explicit retained execution path;
- full fault-injection and acceptance gates pass without legacy progression;
- the symbol/call-site/dead-code audit proves the removed branches have no
  production consumers.

This compatibility window is a migration safety mechanism, not a second source
of truth: `workflow_bindings` continues to give every subject exactly one owner.
