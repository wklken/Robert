# PR CI and Merge-Conflict Self-Healing Implementation Plan

> **For agentic workers:** Execute this plan with
> `superpowers:executing-plans`, task by task. Repository policy prohibits
> subagents unless the user explicitly authorizes delegation.

**Goal:** Let Robert detect CI failures and merge conflicts on PRs it created,
create at most one bounded remediation task for the current PR snapshot, push a
safe repair, and stop or escalate instead of looping indefinitely.

**Architecture:** Extend the existing PR workstream rather than adding a second
orchestrator. A read-only PR health collector records normalized GitHub system
events and durable remediation episodes. The existing one-active-task rule,
`update-existing-pr` route, worker protocol, result audit, and publisher remain
the execution path. Every task and publish action is bound to an exact
`(PR, head_sha, base_sha)` snapshot; stale snapshots are superseded before
dispatch or publication.

**Tech Stack:** Python 3.10+, SQLite, GitHub REST API through the existing `gh`
CLI runner pattern, Git worktrees, `unittest`, JSON Schema.

## Scope After PR #13

PR #13 already provides the following boundaries. This implementation reuses
them and does not replace them:

- audited `action_scope` for repository, source, base branch, branch, worktree,
  and remote;
- publication-time revalidation of the audited scope;
- reconciliation that terminates workstreams for merged or closed PRs;
- exact refresh of read-only PR review worktrees;
- a process-wide `max_publish_actions` budget.

The remaining gaps are:

- no normalized CI/check events or failure evidence;
- no durable episode that aggregates failures for one PR snapshot;
- no merge-conflict detection or conflict-specific task contract;
- existing-PR write worktrees can be reused without proving that they match the
  current remote head;
- pending child tasks are created without rechecking the PR head and base;
- publication does not compare the audited PR snapshot with current GitHub
  state;
- `update_existing_pr` audits assume a human review point and cannot distinguish
  a system remediation task.

## Product Decisions

These decisions are part of the MVP contract:

1. **Opt-in only.** Both CI and conflict remediation default to disabled per
   repository.
2. **Robert-owned PRs only.** A PR is eligible only when its workstream has an
   `origin_workstream_id` and its author matches the repository's configured
   GitHub account. Explicitly adopting arbitrary PRs is a later feature.
3. **Polling first.** The existing run loop reads GitHub REST state. Webhooks can
   later feed the same normalized event and episode interfaces.
4. **One snapshot, one episode.** The identity is
   `(workstream_id, episode_kind, head_sha, base_sha)`.
5. **Conflict wins over CI.** When both are present for the same snapshot,
   Robert resolves the merge conflict first. CI is evaluated after the new head
   is visible.
6. **Merge, never rebase.** Conflict remediation merges the exact base snapshot
   into the PR branch. It does not force-push or rewrite PR history.
7. **Only deterministic CI failures auto-dispatch.** `failure` is eligible.
   `cancelled`, `timed_out`, `stale`, `startup_failure`, and
   `action_required` go to the Operator Inbox. Incomplete checks wait.
8. **No log guessing.** Robert dispatches a CI worker only when it can obtain a
   useful redacted failure excerpt. Raw long logs are neither persisted nor
   inserted into the prompt.
9. **No automatic rerun API in the MVP.** A remediation task must produce a
   code/config change and a new commit. Robert does not spend attempts merely
   rerunning an unchanged workflow.
10. **Ordinary non-force push only.** Git rejects a concurrent non-fast-forward
    update. Robert also checks PR head/base immediately before the push so that
    a base movement cannot silently publish stale conflict work.

## State Model

Each CI or conflict episode uses:

```text
open
  -> queued
  -> remediating
  -> waiting_for_signal
  -> resolved

open|queued|remediating|waiting_for_signal
  -> superseded   # head/base changed
  -> canceled     # PR closed/merged or feature disabled
  -> exhausted    # retry/time/token/cost/evidence policy stopped automation
```

`waiting_for_signal` means a repair was pushed and Robert is waiting for GitHub
to expose the resulting head and checks. It is not a worker-running state.

## Durable Contracts

### Repository configuration

Add this optional block to each public repository:

```yaml
pr_automation:
  conflict:
    enabled: false
    max_attempts: 2
    max_wall_minutes: 60
  ci:
    enabled: false
    check_allowlist: []
    max_attempts: 2
    max_wall_minutes: 120
    max_total_tokens: 200000
    max_cost_usd: 10.0
    max_failure_summary_chars: 12000
```

Validation rules:

- unknown keys are rejected;
- all limits are positive integers except `max_cost_usd`, which is a positive
  number;
- `check_allowlist` contains unique, non-empty strings;
- enabling CI requires a non-empty allowlist;
- missing `pr_automation` preserves current behavior exactly.

### Database changes

Add `actor_kind TEXT NOT NULL DEFAULT 'github_user'` to `github_events`.

Add
`task_kind TEXT NOT NULL DEFAULT 'human_request' CHECK (task_kind IN
('human_request', 'ci_remediation', 'merge_conflict_remediation'))`
to `tasks`.

Create:

```sql
CREATE TABLE IF NOT EXISTS pr_remediation_episodes (
  episode_id TEXT PRIMARY KEY,
  workstream_id TEXT NOT NULL
    REFERENCES workstreams(workstream_id) ON DELETE CASCADE,
  episode_kind TEXT NOT NULL
    CHECK (episode_kind IN ('ci', 'merge_conflict')),
  subject_key TEXT NOT NULL,
  observed_head_sha TEXT NOT NULL,
  observed_base_sha TEXT NOT NULL,
  result_head_sha TEXT,
  status TEXT NOT NULL
    CHECK (status IN (
      'open', 'queued', 'remediating', 'waiting_for_signal',
      'resolved', 'exhausted', 'superseded', 'canceled'
    )),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  failure_signature TEXT,
  first_seen_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  terminal_at TEXT,
  last_task_id TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(workstream_id, episode_kind, subject_key)
);

CREATE TABLE IF NOT EXISTS pr_ci_observations (
  observation_id TEXT PRIMARY KEY,
  episode_id TEXT NOT NULL
    REFERENCES pr_remediation_episodes(episode_id) ON DELETE CASCADE,
  source_kind TEXT NOT NULL
    CHECK (source_kind IN ('workflow_run', 'check_run')),
  external_id TEXT NOT NULL,
  attempt_no INTEGER NOT NULL DEFAULT 1 CHECK (attempt_no >= 1),
  check_name TEXT NOT NULL,
  status TEXT NOT NULL,
  conclusion TEXT,
  details_url TEXT,
  completed_at TEXT,
  failure_signature TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(source_kind, external_id, attempt_no)
);
```

`subject_key` is `"{head_sha}:{base_sha}"`. Storing both SHAs prevents a repair
computed against an old base from being reused after the base branch moves.

### Normalized GitHub system events

Add these internal event types:

- `ci_run_completed`;
- `ci_check_completed`;
- `pr_merge_conflict_detected`.

They use `actor_kind=github_system` and
`authorization_status=authorized_system_trigger`. This status may only be
created by the authenticated PR health collector after all eligibility checks;
fixture discovery and human event authorization must reject attempts to claim
it.

CI event payloads contain:

```json
{
  "workstream_id": "owner/repo#pull_request#13",
  "head_sha": "40-hex-sha",
  "base_sha": "40-hex-sha",
  "check_name": "unit",
  "external_id": "12345",
  "attempt_no": 1,
  "status": "completed",
  "conclusion": "failure",
  "details_url": "https://github.com/...",
  "completed_at": "RFC3339 timestamp",
  "failure_summary": "redacted and truncated text",
  "failure_signature": "sha256 hex"
}
```

The conflict event contains the same workstream/head/base identity plus the PR
number, head branch, base branch, and `mergeable=false`.

### Worker result evidence

Add an optional `remediation_evidence` object to the worker result:

```json
{
  "kind": "ci",
  "episode_id": "episode-id",
  "observed_head_sha": "40-hex-sha",
  "observed_base_sha": "40-hex-sha",
  "failure_signature": "sha256 hex",
  "resolution_summary": "What changed and why it addresses the evidence"
}
```

For conflicts, `kind` is `merge_conflict` and `failure_signature` is omitted.
The host, not the worker, attests the actual Git graph and current PR snapshot.

---

### Task 1: Add Opt-In Configuration and Durable Episode Storage

**Files:**

- Modify: `src/robert_agent/resources/schemas/config.schema.json`
- Modify: `src/robert_agent/resources/config.example.yml`
- Modify: `src/robert_agent/validate_config.py`
- Modify: `src/robert_agent/resources/db/schema.sql`
- Modify: `src/robert_agent/storage.py`
- Modify: `tests/test_config_and_schema.py`
- Modify: `tests/test_migration.py`
- Modify: `tests/test_init_and_doctor.py`

**Interfaces:**

- `validate_config.load_and_validate_config(...)` returns normalized
  `repo["pr_automation"]`.
- `storage.init_database(...)` upgrades existing databases without rebuilding
  them.
- Fresh and migrated databases expose identical columns, checks, indexes, and
  defaults.

- [ ] **Step 1: Write failing configuration tests**

Cover default-disabled behavior, a valid enabled configuration, CI enabled
without an allowlist, duplicate/empty check names, zero/negative limits, and
unknown fields.

- [ ] **Step 2: Write failing schema/migration tests**

Create one database from the old schema fixture, run `init_database`, and assert:

```python
self.assertEqual(_column_default(conn, "github_events", "actor_kind"), "'github_user'")
self.assertEqual(_column_default(conn, "tasks", "task_kind"), "'human_request'")
self.assertTrue(_table_exists(conn, "pr_remediation_episodes"))
self.assertTrue(_table_exists(conn, "pr_ci_observations"))
```

Also insert duplicate episode and observation identities and assert the unique
constraints reject them.

- [ ] **Step 3: Run the focused tests and observe failure**

```bash
.venv/bin/python -B -m unittest \
  tests.test_config_and_schema \
  tests.test_migration \
  tests.test_init_and_doctor
```

Expected: the new assertions fail because the configuration block, columns, and
tables do not exist.

- [ ] **Step 4: Implement the public configuration**

Keep all defaults in one normalization helper in `validate_config.py`; the JSON
Schema validates shape and types, while Python enforces cross-field rules such
as the non-empty allowlist when CI is enabled.

- [ ] **Step 5: Implement fresh schema and idempotent migration**

Add one `_ensure_column` migration for each new column and one migration
function that executes the two `CREATE TABLE IF NOT EXISTS` statements and
indexes. Register it in `init_database` after existing migrations. Do not
rewrite or drop existing task/event tables.

- [ ] **Step 6: Rerun the focused tests**

Expected: all Task 1 tests pass.

### Task 2: Collect an Exact PR Snapshot and Normalized CI Evidence

**Files:**

- Create: `src/robert_agent/pr_health.py`
- Create: `tests/test_pr_health.py`
- Modify: `src/robert_agent/redaction.py`
- Modify: `tests/test_redaction.py`

**Interfaces:**

```python
def fetch_pr_snapshot(repo_full_name, number, runner=subprocess.run):
    """Return normalized current PR state, or None when GitHub read fails."""

def collect_ci_observations(
    repo_full_name,
    head_sha,
    *,
    check_allowlist,
    max_summary_chars,
    runner=subprocess.run,
):
    """Return normalized workflow/check observations for exactly head_sha."""

def summarize_ci(observations, check_allowlist):
    """Return waiting, green, failing, or manual with aggregated failures."""

def failure_signature(failures):
    """Return a stable SHA-256 signature over normalized failure evidence."""
```

`fetch_pr_snapshot` returns:

```python
{
    "state": "open",
    "merged": False,
    "mergeable": True,  # False or None are preserved
    "head_sha": "...",
    "head_ref": "codex/example",
    "head_repo_full_name": "owner/repo",
    "base_sha": "...",
    "base_ref": "main",
    "author_login": "robert-account",
    "html_url": "https://github.com/owner/repo/pull/13",
    "updated_at": "...",
}
```

- [ ] **Step 1: Write snapshot normalization tests**

Cover open/merged/closed PRs, `mergeable` values `true`, `false`, and `null`,
fork head repositories, missing nested fields, and failed `gh api` calls.

- [ ] **Step 2: Write CI observation tests**

Use runner fixtures for:

- workflow runs filtered by the exact `head_sha`;
- workflow `run_attempt`;
- failed Actions jobs and downloaded job logs;
- external check runs with output title/summary/text and annotations;
- exclusion of duplicate GitHub Actions check runs from the external-check
  path;
- allowlist filtering;
- completed success/neutral/skipped checks;
- incomplete checks;
- failure versus manual-only conclusions;
- redaction and truncation before returning observations.

- [ ] **Step 3: Write failure-signature tests**

Assert that changing timestamps, ANSI color sequences, and volatile absolute
workspace prefixes does not change the signature, while a changed failure
message does.

- [ ] **Step 4: Run and observe failure**

```bash
.venv/bin/python -B -m unittest \
  tests.test_pr_health \
  tests.test_redaction
```

- [ ] **Step 5: Implement the collector**

Use `discover._try_run_json` for JSON reads so live and fixture-backed runners
retain the same injection boundary. Query:

```text
GET repos/{repo}/pulls/{number}
GET repos/{repo}/actions/runs?head_sha={head_sha}
GET repos/{repo}/actions/runs/{run_id}/jobs
GET repos/{repo}/actions/jobs/{job_id}/logs
GET repos/{repo}/commits/{head_sha}/check-runs
```

Only request job logs for failed allowlisted workflows. Redact each excerpt
with the existing redaction module before truncation. Return a `manual` summary
if evidence is empty, unavailable, or blocked by secret detection.

- [ ] **Step 6: Rerun focused tests**

Expected: all collector and redaction tests pass.

### Task 3: Implement the Episode State Machine and Budgets

**Files:**

- Create: `src/robert_agent/remediation.py`
- Create: `tests/test_remediation.py`
- Modify: `src/robert_agent/usage.py`
- Modify: `tests/test_workflow_matrix.py`

**Interfaces:**

```python
def upsert_health_episodes(
    conn,
    *,
    repo,
    workstream,
    snapshot,
    ci_summary,
    now,
):
    """Persist observations, supersede stale episodes, and return transitions."""

def next_system_decisions(conn, *, repo, now):
    """Return eligible episode triggers ordered conflict-before-CI."""

def revalidate_decision(conn, *, decision, snapshot, repo):
    """Return eligible, superseded, canceled, or exhausted before dispatch."""

def record_task_started(conn, *, episode_id, task_id, now):
    """Atomically increment attempt_count and bind the task."""

def record_publish_result(
    conn,
    *,
    episode_id,
    publish_status,
    result_head_sha=None,
    now,
):
    """Move a published repair to waiting_for_signal or record failure."""
```

- [ ] **Step 1: Write table-driven transition tests**

Cover:

- first conflict observation creates `open`;
- first failing CI summary creates one `open` episode;
- multiple failed checks on the same snapshot remain one episode;
- duplicate polling is idempotent;
- changed head or base supersedes all non-terminal old episodes;
- merged/closed PR cancels open episodes;
- conflict decision sorts before CI;
- a PR with an active task keeps the decision pending;
- successful configured checks resolve CI;
- mergeable `true` at `result_head_sha` resolves conflict;
- incomplete checks preserve `waiting_for_signal`;
- repeated identical CI failure signature after a repair exhausts early.

- [ ] **Step 2: Write budget tests**

For each episode, aggregate usage from its bound tasks/attempts using
`usage.extract_attempt_usage`. Assert exhaustion on:

- `max_attempts`;
- `max_wall_minutes`;
- `max_total_tokens`;
- `max_cost_usd`.

When usage is unavailable, attempt and wall-clock limits still enforce. Missing
usage must not be interpreted as zero cost with unlimited retries.

- [ ] **Step 3: Run and observe failure**

```bash
.venv/bin/python -B -m unittest \
  tests.test_remediation \
  tests.test_workflow_matrix
```

- [ ] **Step 4: Implement episode transitions as explicit functions**

Keep all SQL writes and transition guards in `remediation.py`. Do not scatter
status updates across the collector, prompt renderer, and publisher. Each
transition writes an `audit_events` record with old status, new status, reason,
episode ID, and observed SHAs.

- [ ] **Step 5: Rerun focused tests**

Expected: all state and budget tests pass.

### Task 4: Integrate GitHub-System Decisions into the Existing Run Loop

**Files:**

- Modify: `src/robert_agent/run_once.py`
- Modify: `src/robert_agent/authorize.py`
- Modify: `src/robert_agent/route.py`
- Modify: `src/robert_agent/resources/routes.yml`
- Modify: `tests/test_run_once.py`
- Modify: `tests/test_discovery_authorization.py`
- Modify: `tests/test_routing.py`
- Modify: `tests/test_github_event_matrix.py`

**Interfaces:**

- Existing human discovery continues through `authorize_event`.
- `_reconcile_remote_source_states` reuses `fetch_pr_snapshot` for PRs.
- An internal `system_decisions` collection is appended only after authenticated
  state collection and episode eligibility checks.
- Both remediation kinds route to `route_id=update-existing-pr` and
  `expected_output=update_existing_pr`.

- [ ] **Step 1: Write authorization-boundary tests**

Assert that fixture or discovered payloads claiming
`actor_kind=github_system` or
`authorization_status=authorized_system_trigger` are not trusted. Assert that
only the internal collector path creates the system status.

- [ ] **Step 2: Write scheduling tests**

Cover:

- feature disabled produces no health API reads, system events, or tasks;
- non-Robert-owned PR produces no task;
- stale head/base decision is superseded before `_create_task_attempt_and_prompt`;
- one active human task causes the episode event to enter pending;
- task completion creates at most one child remediation task;
- conflict and CI present together dispatch only conflict;
- duplicate loop iterations do not create duplicate tasks;
- old-SHA completed CI events never create a task;
- PR close/merge cancels episodes before dispatch.

- [ ] **Step 3: Run and observe failure**

```bash
.venv/bin/python -B -m unittest \
  tests.test_discovery_authorization \
  tests.test_github_event_matrix \
  tests.test_routing \
  tests.test_run_once
```

- [ ] **Step 4: Extend PR reconciliation**

For each open PR workstream:

1. read the exact PR snapshot;
2. apply Robert-owned and opt-in eligibility;
3. collect allowlisted CI only when CI automation is enabled;
4. insert normalized system events with deterministic fingerprints;
5. upsert/supersede episodes;
6. request at most one eligible system decision.

Use event fingerprints based on stable external IDs and attempts:

```text
github-system:workflow_run:{run_id}:{run_attempt}
github-system:check_run:{check_run_id}:{attempt_no}
github-system:merge_conflict:{pr_number}:{head_sha}:{base_sha}
```

- [ ] **Step 5: Add task kind and required skills**

Create remediation tasks with:

```python
route_id = "update-existing-pr"
expected_output = "update_existing_pr"
task_kind = "ci_remediation"  # or merge_conflict_remediation
```

Required skills:

- CI: `fast-test-fix`, `fast-preflight`;
- conflict: `fast-conflict-fix`, `fast-preflight`.

If a configured worker cannot satisfy the required skills, leave the episode
un-dispatched and create an Operator Inbox reason rather than weakening the
contract.

- [ ] **Step 6: Revalidate pending system decisions**

Before `_create_child_task_for_pending_events` selects a system event, fetch the
current PR snapshot and call `remediation.revalidate_decision`. Human pending
events retain their existing behavior.

- [ ] **Step 7: Rerun focused tests**

Expected: authorization and scheduling tests pass without changing human event
routing.

### Task 5: Prepare an Exact, Clean Existing-PR Write Worktree

**Files:**

- Modify: `src/robert_agent/worktree.py`
- Modify: `src/robert_agent/run_once.py`
- Modify: `tests/test_live_worktree_acceptance.py`
- Modify: `tests/test_run_once.py`

**Interfaces:**

```python
def prepare_existing_pr_worktree(
    repo_root,
    worktree_root,
    *,
    source_number,
    head_branch,
    head_sha,
    base_branch,
    base_sha,
    dry_run=True,
):
    """Create or safely refresh the write worktree to the exact PR snapshot."""
```

The return value includes `head_ref`, `base_ref`, `observed_head_sha`,
`observed_base_sha`, `worktree_path`, `branch_name`, commands, and a safe
blocked reason.

- [ ] **Step 1: Write failing worktree tests**

Create local bare remotes and assert:

- exact PR head is fetched to `refs/robert/prs/pr-{number}-head`;
- exact base is fetched to `refs/robert/prs/pr-{number}-base`;
- a new worktree starts exactly at `head_sha`;
- a clean reused worktree is reset exactly to `head_sha`;
- a dirty reused worktree returns `blocked_dirty_worktree` and is never reset;
- a fetched SHA mismatch returns `stale_snapshot`;
- conflict setup exposes the exact base ref but does not run the merge itself;
- no command contains `--force`, rebase, or destructive cleanup of a dirty tree.

- [ ] **Step 2: Run and observe failure**

```bash
.venv/bin/python -B -m unittest \
  tests.test_live_worktree_acceptance \
  tests.test_run_once
```

- [ ] **Step 3: Implement exact-ref preparation**

Fetch:

```text
+pull/{number}/head:refs/robert/prs/pr-{number}-head
+refs/heads/{base_branch}:refs/robert/prs/pr-{number}-base
```

Then verify with `git rev-parse` that both local refs equal the observed SHAs.
For a reused worktree, first run `git status --porcelain`; reset only when the
output is empty. A dirty worktree is user/operator-owned state and must not be
discarded.

- [ ] **Step 4: Use the new function only for automated remediation**

Keep the current human `update-existing-pr` worktree path unchanged in this
task. This avoids silently changing established human-driven update semantics.

- [ ] **Step 5: Rerun focused tests**

Expected: exact snapshot, dirty-tree protection, and current human worktree
tests pass.

### Task 6: Make Prompt, Result, and Audit Contracts Task-Kind Aware

**Files:**

- Modify: `src/robert_agent/render_prompt.py`
- Modify: `src/robert_agent/worker/result.py`
- Modify: `src/robert_agent/resources/schemas/result.schema.json`
- Modify: `src/robert_agent/audit_result.py`
- Modify: `src/robert_agent/run_once.py`
- Modify: `tests/test_worker_protocol.py`
- Modify: `tests/test_run_once.py`
- Modify: `tests/test_acceptance_suite.py`

**Interfaces:**

```python
def attest_remediation_worktree(
    *,
    task_kind,
    action_scope,
    remediation_evidence,
    run_command=subprocess.run,
):
    """Return a host-generated accepted/failed Git graph attestation."""
```

Add this function to `remediation.py`. It runs after worker result loading and
before `audit_result`.

- [ ] **Step 1: Write prompt tests**

Assert that CI prompts include the aggregated redacted evidence, check names,
details URLs, exact SHAs, budgets, required skills, and instruction to reproduce
the failure locally before editing. Conflict prompts include exact refs and:

```text
git merge --no-edit refs/robert/prs/pr-{number}-base
```

They instruct the worker to resolve all unmerged paths, run repository
verification, commit the merge, and never rebase or force-push.

- [ ] **Step 2: Write result-schema tests**

Assert required field combinations:

- human `update_existing_pr` still requires `review_point_evaluation`;
- CI remediation requires matching `kind`, episode ID, both SHAs, failure
  signature, and non-empty resolution summary;
- conflict remediation requires matching `kind`, episode ID, both SHAs, and
  resolution summary;
- a worker cannot change the trusted episode ID or observed SHAs.

- [ ] **Step 3: Write host-attestation tests**

For both remediation kinds:

- current branch matches the scoped branch;
- `observed_head_sha` is an ancestor of worktree `HEAD`;
- worktree has no unmerged paths.

For conflict remediation additionally assert
`observed_base_sha` is an ancestor of `HEAD`. A textual worker claim without
these Git facts is rejected.

- [ ] **Step 4: Run and observe failure**

```bash
.venv/bin/python -B -m unittest \
  tests.test_worker_protocol \
  tests.test_run_once \
  tests.test_acceptance_suite
```

- [ ] **Step 5: Implement task-kind prompt guidance**

Add `task_kind` and trusted episode context to `render_prompt`. Suppress
human-review-point guidance only for remediation task kinds. Do not change the
human prompt contract.

- [ ] **Step 6: Implement result parsing and audit**

Persist `remediation_evidence` in `worker_results.metadata_json`. Pass
`task_kind`, trusted episode context, and the host attestation into
`audit_result`. Keep `audit_result.py` pure: it compares data and policy but
does not execute Git or GitHub commands.

- [ ] **Step 7: Rerun focused tests**

Expected: all new remediation contracts and existing human review contracts
pass.

### Task 7: Add Publication-Time PR Snapshot Compare-and-Swap

**Files:**

- Modify: `src/robert_agent/run_once.py`
- Modify: `src/robert_agent/publish.py`
- Modify: `tests/test_publish_dedupe_acceptance.py`
- Modify: `tests/test_production_write_canary.py`
- Modify: `tests/test_run_once.py`

**Interfaces:**

Extend the trusted remediation action scope with:

```python
{
    "task_kind": "ci_remediation",
    "episode_id": "...",
    "pr_number": 13,
    "observed_head_sha": "...",
    "observed_base_sha": "...",
}
```

```python
def validate_remediation_publish_snapshot(
    action_scope,
    *,
    runner=subprocess.run,
):
    """Return current, superseded, canceled, or unavailable."""
```

- [ ] **Step 1: Write failing publication tests**

Cover:

- open PR with matching head/base permits an ordinary push;
- changed head skips the action as `superseded_remote_head`;
- changed base skips it as `superseded_remote_base`;
- closed or merged PR skips it as `remote_pr_terminal`;
- GitHub read failure blocks publication rather than assuming current;
- local `HEAD` is recorded as `result_head_sha` after a successful push;
- a following comment action is skipped when its preceding remediation push was
  superseded or failed;
- ordinary human `push_existing_pr` retains its current behavior;
- no force-push command is generated.

- [ ] **Step 2: Run and observe failure**

```bash
.venv/bin/python -B -m unittest \
  tests.test_publish_dedupe_acceptance \
  tests.test_production_write_canary \
  tests.test_run_once
```

- [ ] **Step 3: Implement pre-push snapshot validation**

After the existing `action_scope_violation` check and immediately before
`git push`, read the PR snapshot. Only a byte-for-byte match of both SHAs may
publish. If the snapshot is stale, mark the GitHub action `skipped`, mark the
episode `superseded`, and do not turn this into a retry attempt.

- [ ] **Step 4: Record the pushed result**

After a successful ordinary push, run `git rev-parse HEAD`, persist it as
`result_head_sha`, and move the episode to `waiting_for_signal`. The next loop
must observe that SHA from GitHub before deciding resolved, failing again, or
still waiting.

- [ ] **Step 5: Rerun focused tests**

Expected: publication race tests and all existing dedupe/canary tests pass.

### Task 8: Close the Merge-Conflict Loop End to End

**Files:**

- Modify: `tests/test_controlled_e2e_acceptance.py`
- Modify: `tests/test_live_discovery_acceptance.py`
- Modify: `src/robert_agent/controlled_e2e_acceptance.py`
- Modify: `src/robert_agent/live_discovery_acceptance.py`

**Acceptance scenario:**

1. A Robert-created open PR has `mergeable=false` at head `H1`, base `B1`.
2. One `pr_merge_conflict_detected` event and one conflict episode are created.
3. One `merge_conflict_remediation` task starts in an exact clean worktree.
4. The worker merges `B1`, resolves conflicts, verifies, and produces result
   `H2`.
5. Audit proves both `H1` and `B1` are ancestors of `H2`, with no unmerged
   paths.
6. Publication rechecks GitHub still reports `H1/B1`, then pushes `H2`.
7. A later poll reports `head=H2` and `mergeable=true`.
8. The episode becomes `resolved`; no second worker starts.

- [ ] **Step 1: Add the controlled fixture-backed acceptance scenario**

Include duplicate delivery, a `mergeable=null` polling interval, and a base
movement before publication. Assert null waits and base movement supersedes.

- [ ] **Step 2: Add a local Git acceptance scenario**

Use temporary bare remotes and branches with a real textual conflict. The test
executes the merge/resolution graph and proves the attestation checks the graph,
not merely result JSON.

- [ ] **Step 3: Run the conflict acceptance tests**

```bash
.venv/bin/python -B -m unittest \
  tests.test_controlled_e2e_acceptance \
  tests.test_live_discovery_acceptance \
  tests.test_live_worktree_acceptance
```

Expected: one task and one non-force push per eligible snapshot; stale or
duplicate observations produce no extra task.

### Task 9: Close the CI Failure Loop End to End

**Files:**

- Modify: `tests/test_controlled_e2e_acceptance.py`
- Modify: `tests/test_live_discovery_acceptance.py`
- Modify: `tests/test_loop_engine.py`
- Modify: `src/robert_agent/controlled_e2e_acceptance.py`
- Modify: `src/robert_agent/live_discovery_acceptance.py`

**Acceptance scenario:**

1. A Robert-created PR at `H1/B1` has two allowlisted failed checks.
2. Duplicate workflow/check deliveries produce one CI episode.
3. Redacted evidence from both checks is aggregated into one
   `ci_remediation` task.
4. The worker reproduces, repairs, verifies, commits `H2`, and returns matching
   remediation evidence.
5. Audit and publication snapshot checks pass; Robert pushes `H2`.
6. The episode waits while configured checks on `H2` are incomplete.
7. All configured checks on `H2` conclude `success`, `neutral`, or `skipped`.
8. The episode becomes `resolved`; green CI starts no worker.

- [ ] **Step 1: Add failing aggregation/idempotency acceptance tests**

Include two failed checks, duplicate poll delivery, one disallowed failing
check, and a stale `H0` completion arriving after `H1`.

- [ ] **Step 2: Add failure-policy acceptance tests**

Assert:

- missing or fully redacted evidence creates Operator Inbox attention and no
  worker;
- `cancelled` and `timed_out` create attention and no worker;
- repeated same signature after one pushed repair exhausts early;
- changed signature may retry while budgets remain;
- max attempts, wall time, tokens, and cost each stop further dispatch;
- green configured checks close the episode without worker activity.

- [ ] **Step 3: Add loop-budget regression coverage**

Assert episode budgets compose with, and never bypass, the existing
`max_publish_actions` process budget.

- [ ] **Step 4: Run the CI acceptance tests**

```bash
.venv/bin/python -B -m unittest \
  tests.test_controlled_e2e_acceptance \
  tests.test_live_discovery_acceptance \
  tests.test_loop_engine
```

Expected: one aggregated task per snapshot, no stale-SHA task, no green-CI
worker, and explicit exhaustion records.

### Task 10: Surface Operator Attention and Document the Contract

**Files:**

- Modify: `src/robert_agent/workbench.py`
- Modify: `tests/test_workbench.py`
- Modify: `src/robert_agent/resources/github-events.md`
- Modify: `src/robert_agent/resources/workstream-model.md`
- Modify: `src/robert_agent/resources/result-contract.md`
- Modify: `src/robert_agent/resources/worker-prompt.md`
- Modify: `src/robert_agent/resources/failure-modes.md`
- Modify: `src/robert_agent/resources/acceptance-matrix.md`
- Modify: `README.md`
- Modify: `README_EN.md`

**Interfaces:**

Add notification types:

- `pr_remediation_needs_evidence`;
- `pr_remediation_manual_conclusion`;
- `pr_remediation_dirty_worktree`;
- `pr_remediation_exhausted`;
- `pr_remediation_snapshot_unavailable`.

- [ ] **Step 1: Write Operator Inbox tests**

Extend the workbench read model to inspect remediation notifications and map
them to `needs_attention` without falsely marking the whole PR workstream
failed. Assert the reason includes episode kind, PR number, head/base SHAs,
attempt count, budget usage, and details URLs, but no raw secrets/logs.

- [ ] **Step 2: Implement attention mapping**

Pass notifications into `_derive_operator_state`. The newest unresolved
remediation notification takes precedence over a completed task state. A later
`resolved` or `superseded` episode notification clears that episode's attention.

- [ ] **Step 3: Update normative documentation**

Document:

- normalized system events and why they are not trusted-human events;
- episode identity and all terminal states;
- conflict priority and merge-only rule;
- evidence redaction/truncation;
- exact worktree and publication snapshot checks;
- retry/time/token/cost budgets;
- green, stale, duplicate, close, and merge behavior;
- opt-in configuration and Robert-owned-PR restriction.

- [ ] **Step 4: Run focused workbench and docs tests**

```bash
.venv/bin/python -B -m unittest \
  tests.test_workbench \
  tests.test_docs \
  tests.test_public_surface
```

### Task 11: Final Regression, Boundary, and Scope Audit

**Files:**

- Review all files changed by Tasks 1-10.
- Do not make unrelated cleanup changes.

- [ ] **Step 1: Run the full repository gate**

```bash
source .venv/bin/activate
python -B -m unittest discover -s tests
python -m ruff check src tests
python -m mypy \
  src/robert_agent/cli \
  src/robert_agent/paths.py \
  src/robert_agent/route_config.py \
  src/robert_agent/skills.py \
  src/robert_agent/service.py \
  src/robert_agent/migrate.py
python -m compileall -q src
python -m build
python -m pip_audit
git diff --check
```

No check may be silently skipped. Record any environment block separately from
product failures.

- [ ] **Step 2: Audit the trust and side-effect boundaries**

Manually verify with targeted searches and tests:

- raw GitHub payloads cannot self-assert `github_system`;
- only Robert-owned, opted-in PRs are eligible;
- every remediation event/task/action names exact head and base SHAs;
- child-task creation and publication revalidate those SHAs;
- worker text cannot override trusted episode or action scope;
- CI text is redacted before persistence/prompting;
- no force-push, automatic rebase, or implicit dirty-tree reset exists;
- terminal/exhausted episodes cannot dispatch;
- human comment/review/update flows retain their existing behavior.

- [ ] **Step 3: Run a changed-symbol scan**

For every added or modified public/helper function, run `rg` across `src` and
`tests` to identify all call sites. Remove only dead code introduced by this
change. Do not delete pre-existing compatibility surfaces.

- [ ] **Step 4: Inspect the final diff**

Confirm every changed line maps to one of:

- opt-in policy;
- health collection;
- episode persistence/state;
- exact worktree preparation;
- task-kind result/audit contract;
- publication compare-and-swap;
- operator visibility;
- tests or normative documentation.

Commit, push, and PR creation remain separate delivery actions and require
explicit authorization.

## Completion Criteria

The implementation is complete only when all of these are demonstrated:

- repeated polling or duplicate delivery creates no duplicate event,
  observation, episode, task, or push;
- old head/base observations never dispatch or publish;
- multiple failed checks on one snapshot produce one aggregated task;
- a pushed repair waits for GitHub to expose the new head and current checks;
- green CI closes an episode without starting a worker;
- merge conflicts use an exact base merge, with no rebase or force-push;
- current head/base and a clean Git graph are host-attested before publication;
- PR merge/close cancels every non-terminal remediation episode;
- missing evidence, dirty worktree, manual conclusions, and exhausted budgets
  produce explicit Operator Inbox attention;
- human-triggered Robert workflows remain regression-green.

## Explicit Non-Goals

- Automatically adopting PRs Robert did not create.
- GitHub webhook server deployment; polling and webhook delivery share a future
  normalization boundary.
- Automatically repairing external CI systems whose logs cannot be read.
- Retrying unchanged workflow runs through the GitHub rerun API.
- Force-pushing, rebasing contributor branches, or resolving dirty user
  worktrees.
- Creating a separate CI-fix route, worker protocol, or publisher.
