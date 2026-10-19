# Worker Fallback Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Repository policy prohibits subagents unless separately authorized.

**Goal:** Reduce Robert's default hard worker timeout to 45 minutes and dispatch a worker's fallback model after two consecutive `failed_timeout` attempts in the same workstream.

**Architecture:** Add an optional `fallback_model` to each normalized worker configuration. Immediately before dispatch, inspect the two most recent completed attempts in the current workstream; if both ended with `failed_timeout` (including recovered timeout records whose `original_status` is `failed_timeout`), replace the normally selected route/default model with the selected worker's fallback model. Keep worker selection, route overrides, reasoning effort, persistence, and retry creation unchanged.

**Tech Stack:** Python 3.10+, SQLite, PyYAML, JSON Schema, `unittest`

## Global Constraints

- Default `hard_timeout_minutes` is 45; an explicitly configured value still wins.
- Fallback activates only after two consecutive timeout failures in the same workstream.
- A completion, cancellation, startup failure, dispatch failure, or other non-timeout failure breaks the consecutive-timeout sequence.
- Fallback changes only the model; reasoning effort keeps the existing worker/route selection result.
- `fallback_model` is optional. Workers without it retain current dispatch behavior.
- No database migration or runtime dependency is added.
- Preserve trust, workstream, audit, redaction, and idempotency boundaries.

---

### Task 1: Normalize and document worker fallback configuration

**Files:**
- Modify: `src/robert_agent/validate_config.py`
- Modify: `src/robert_agent/resources/schemas/config.schema.json`
- Modify: `src/robert_agent/resources/config.example.yml`
- Modify: `src/robert_agent/live_worker_acceptance.py`
- Test: `tests/test_config_and_schema.py`
- Test: `tests/test_live_worker_acceptance.py`

**Interfaces:**
- Consumes: Existing mapping-style `workers.<name>` and legacy list-style worker configuration.
- Produces: Every normalized worker has `fallback_model: str | None`; public YAML accepts optional `workers.<name>.fallback_model`.

- [ ] **Step 1: Add failing configuration tests**

Add assertions showing that mapping-style workers normalize `fallback_model`, workers without it normalize to `None`, blank fallback values are rejected, the public schema exposes the field, and isolated live-worker configuration preserves it:

```python
self.assertEqual(result["workers"][0]["fallback_model"], "gpt-5.6-sol")
self.assertIsNone(result["workers"][1]["fallback_model"])
self.assertEqual(
    invalid["safe_error"],
    "invalid config: workers.default.fallback_model must not be empty",
)
self.assertEqual(
    worker_config["properties"]["fallback_model"],
    {"type": "string", "minLength": 1},
)
self.assertEqual(isolated["workers"][0]["fallback_model"], "gpt-5.6-sol")
```

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
python3 -B -m unittest \
  tests.test_config_and_schema.ConfigAndSchemaTests.test_config_accepts_named_workers_and_resolves_route_defaults \
  tests.test_config_and_schema.ConfigAndSchemaTests.test_config_rejects_blank_worker_fallback_model \
  tests.test_config_and_schema.ConfigAndSchemaTests.test_config_schema_documents_public_yaml_contract \
  tests.test_live_worker_acceptance.LiveWorkerAcceptanceTests.test_isolated_config_preserves_named_workers_and_route_selection
```

Expected: FAIL because `fallback_model` is unsupported or absent.

- [ ] **Step 3: Implement minimal configuration support**

In `_normalize_workers`, accept `fallback_model` in both worker shapes and normalize it with this contract:

```python
raw_fallback_model = raw_worker.get("fallback_model")
fallback_model = (
    None
    if raw_fallback_model is None
    else str(raw_fallback_model).strip()
)
if raw_fallback_model is not None and not fallback_model:
    raise ValueError(f"workers.{name}.fallback_model must not be empty")
```

Store `"fallback_model": fallback_model` beside `default_model`. The list-style error uses `workers[{index}].fallback_model`. Legacy implicit workers use `fallback_model: None`.

Add this schema property:

```json
"fallback_model": {"type": "string", "minLength": 1}
```

Preserve the field when `live_worker_acceptance._isolated_config()` copies normalized workers. Add an example fallback model to the packaged YAML configuration.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the command from Step 2.

Expected: PASS.

---

### Task 2: Select fallback model after two consecutive timeout failures

**Files:**
- Modify: `src/robert_agent/run_once.py`
- Test: `tests/test_run_once.py`

**Interfaces:**
- Consumes: `db_path`, current `task_info["workstream_id"]`, current `attempt_id`, selected worker's `fallback_model`, and the existing normally selected model.
- Produces: `_model_for_dispatch(conn, workstream_id, attempt_id, selected_worker, primary_model) -> str`.

- [ ] **Step 1: Add failing dispatch behavior tests**

Create focused `_dispatch_queued` tests with SQLite attempts in one workstream. Cover:

```python
# Two latest prior attempts both timed out: use fallback.
self.assertEqual(calls[0]["model"], "model-fallback")
self.assertEqual(calls[0]["reasoning_effort"], "high")

# Only one timeout: keep primary model.
self.assertEqual(calls[0]["model"], "route-model")

# A non-timeout failure between timeouts resets the sequence.
self.assertEqual(calls[0]["model"], "route-model")

# A completed attempt resets the sequence.
self.assertEqual(calls[0]["model"], "route-model")

# Recovered timeout records still count.
# failure_json = {"status": "needs_resume", "original_status": "failed_timeout"}
self.assertEqual(calls[0]["model"], "model-fallback")

# No configured fallback: keep primary model.
self.assertEqual(calls[0]["model"], "route-model")

# Automatic resume attempts in the same task use fallback on attempt 3.
self.assertEqual(calls[0]["model"], "model-fallback")
```

- [ ] **Step 2: Run focused dispatch tests and verify RED**

Run:

```bash
python3 -B -m unittest \
  tests.test_run_once.RunOnceDryRunTests.test_dispatch_uses_fallback_model_after_two_consecutive_timeouts \
  tests.test_run_once.RunOnceDryRunTests.test_dispatch_keeps_primary_model_before_second_consecutive_timeout \
  tests.test_run_once.RunOnceDryRunTests.test_dispatch_non_timeout_failure_resets_fallback_sequence \
  tests.test_run_once.RunOnceDryRunTests.test_dispatch_success_resets_fallback_sequence \
  tests.test_run_once.RunOnceDryRunTests.test_dispatch_counts_recovered_timeouts_for_fallback \
  tests.test_run_once.RunOnceDryRunTests.test_dispatch_without_fallback_model_keeps_primary_model \
  tests.test_run_once.RunOnceDryRunTests.test_dispatch_same_task_resume_uses_fallback_after_two_timeouts
```

Expected: FAIL because dispatch always uses the route/default model.

- [ ] **Step 3: Implement timeout-history lookup and model selection**

Add a small helper near `_dispatch_queued`:

```python
def _model_for_dispatch(
    conn,
    workstream_id,
    attempt_id,
    selected_worker,
    primary_model,
):
    fallback_model = selected_worker.get("fallback_model")
    if not fallback_model:
        return primary_model
    rows = conn.execute(
        """
        SELECT a.failure_json
        FROM attempts a
        JOIN tasks t ON t.task_id = a.task_id
        WHERE t.workstream_id = ?
          AND a.attempt_id != ?
          AND a.status IN ('completed', 'failed', 'canceled')
        ORDER BY a.started_at DESC, a.attempt_id DESC
        LIMIT 2
        """,
        (workstream_id, attempt_id),
    ).fetchall()
    if len(rows) != 2:
        return primary_model
    failures = [_json_object(row[0]) for row in rows]
    if all(
        failure.get("status") == "failed_timeout"
        or failure.get("original_status") == "failed_timeout"
        for failure in failures
    ):
        return fallback_model
    return primary_model
```

In `_dispatch_queued`, compute the existing route/default `primary_model`, open the database read-only through the existing SQLite connection pattern, and pass the helper result as `worker_kwargs["model"]`. Do not change `reasoning_effort`.

- [ ] **Step 4: Run focused dispatch tests and verify GREEN**

Run the command from Step 2.

Expected: PASS.

---

### Task 3: Change the global timeout default to 45 minutes

**Files:**
- Modify: `src/robert_agent/supervise.py`
- Modify: `src/robert_agent/validate_config.py`
- Modify: `src/robert_agent/live_worker_acceptance.py`
- Modify: `src/robert_agent/live_worktree_acceptance.py`
- Modify: `src/robert_agent/controlled_e2e_acceptance.py`
- Test: `tests/test_config_and_schema.py`
- Test: `tests/test_operational_commands.py`

**Interfaces:**
- Consumes: Existing optional `hard_timeout_minutes` configuration.
- Produces: An omitted value resolves to `45`; explicit positive values remain unchanged.

- [ ] **Step 1: Add failing default-timeout tests**

Remove `hard_timeout_minutes` from a generated config and assert:

```python
self.assertEqual(result["hard_timeout_minutes"], 45)
```

Add a supervision default test that classifies a 46-minute runtime without passing `hard_timeout_minutes`:

```python
timed_out = supervise.classify_attempt(
    heartbeat_at=now,
    started_at=now - timedelta(minutes=46),
    now=now,
)
self.assertEqual(timed_out["status"], "failed_timeout")
self.assertEqual(timed_out["timeout_seconds"], 45 * 60)
```

- [ ] **Step 2: Run focused timeout tests and verify RED**

Run:

```bash
python3 -B -m unittest \
  tests.test_config_and_schema.ConfigAndSchemaTests.test_config_defaults_hard_timeout_to_45_minutes \
  tests.test_operational_commands.OperationalCommandTests.test_supervise_defaults_hard_timeout_to_45_minutes
```

Expected: FAIL with the current 90-minute default.

- [ ] **Step 3: Replace every fallback default of 90 with 45**

Use `45` in:

```python
hard_timeout_minutes=45
parser.add_argument("--hard-timeout-minutes", type=int, default=45)
config.get("hard_timeout_minutes", 45)
source_config.get("hard_timeout_minutes", 45)
```

Do not change tests that deliberately pass `hard_timeout_minutes=90` to verify explicit override behavior.

- [ ] **Step 4: Run focused timeout tests and verify GREEN**

Run the command from Step 2.

Expected: PASS.

---

### Task 4: Document behavior and run repository gates

**Files:**
- Modify: `docs/guides.md`
- Modify: `docs/reference.md`
- Modify: `README.md`
- Modify: `README_EN.md`
- Test: `tests/test_docs.py`

**Interfaces:**
- Consumes: Final worker configuration and dispatch behavior from Tasks 1-3.
- Produces: User-facing YAML examples and exact fallback semantics.

- [ ] **Step 1: Update documentation examples**

Show:

```yaml
hard_timeout_minutes: 45
workers:
  default:
    adapter: codex
    command: codex
    model: gpt-6.1-sol
    fallback_model: gpt-5.6-sol
    effort: high
```

State that fallback begins on the third run after the two immediately preceding attempts in the same workstream both ended in `failed_timeout`; success and other failure types reset the sequence; effort is unchanged.

- [ ] **Step 2: Run documentation tests**

Run:

```bash
python3 -B -m unittest tests.test_docs
```

Expected: PASS.

- [ ] **Step 3: Run complete verification**

Run:

```bash
python3 -B -m unittest discover -s tests
python3 -m build
git diff --check
```

Expected: `unittest` reports all tests passing, build creates sdist/wheel successfully, and `git diff --check` exits 0.

- [ ] **Step 4: Review the exact task diff**

Stage only task-owned files and review the staged diff for fallback ordering, explicit-timeout compatibility, schema/documentation consistency, and unrelated changes. Resolve all blocking findings and rerun affected checks.

- [ ] **Step 5: Commit, push, and open the PR**

```bash
git commit -m "feat(worker): add fallback model after repeated timeouts"
git push -u origin feat/worker-fallback-model
gh pr create --base main --head feat/worker-fallback-model \
  --title "feat(worker): add fallback model after repeated timeouts" \
  --body-file /tmp/robert-worker-fallback-pr.md
```

The PR body must summarize the 45-minute default, fallback semantics, configuration example, full verification commands, and local review result.
