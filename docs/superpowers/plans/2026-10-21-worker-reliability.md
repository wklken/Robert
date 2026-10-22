# Worker reliability implementation plan

**Goal:** Resume interrupted model workers without mistaking stale command phases for live processes, and preserve command completion despite SQLite contention.

**Architecture:** The heartbeat wrapper writes an atomic command journal in the task artifact directory before attempting database snapshots. Recovery combines process liveness, journals, and existing worktree/log evidence; model transport failures use bounded delayed same-task resumes. Existing result auditing and GitHub publication idempotency remain unchanged.

**Tech stack:** Python standard library, SQLite, unittest; no runtime dependencies.

## Constraints

- Base: `origin/main` at `2a5bed0`; isolated branch `fix/worker-recovery-reliability`.
- Preserve original task, accepted events, branch, worktree, and publication boundaries.
- Never resume over a surviving command; never infer command exit success from missing processes.
- Do not modify the deployed package or existing worker model configuration.
- Keep all work in the main agent; user authorized commit, push, and one PR, not merge/deployment.

## Task 1: Durable command lifecycle

Files: `src/robert_agent/worker/heartbeat.py`, `tests/test_worker_protocol.py`.

- [x] Add tests exercising real subprocesses with locked start, heartbeat, and completion snapshot writes. Assert command exit status and saved output survive, and a journal records terminal state before the final database write.
- [x] Run focused tests and observe the new failures before implementation.
- [x] Add atomic journals with task/attempt identity, process identity, output paths, terminal status, and return code. Treat only SQLite busy/locked snapshots as deferred; retain other errors and avoid orphaning children on wrapper exceptions.
- [x] Run the focused tests, including failing commands and timeouts.

## Task 2: Evidence-based recovery

Files: `src/robert_agent/run_once.py`, `tests/test_run_once.py`.

- [x] Reproduce stale running snapshots after command completion and model exit. Assert same-task resume preserves worktree and exit evidence.
- [x] Replace the old test that equates a phase string with liveness with a surviving-process case. Include PID identity mismatch and multiple command records so one completed command cannot hide a live one.
- [x] Read command journals from registered artifact locations and the task artifact directory. Replay deferred completion snapshots idempotently in the supervisor transaction. Only verified live processes block resume; stale phases contribute progress, not liveness.
- [x] Run `python3 -B -m unittest tests.test_run_once tests.test_worker_protocol` (184 tests at the first checkpoint; later cases included in final full-suite gate).

## Task 3: Bounded transport-failure resumes and delivery

Files: `src/robert_agent/run_once.py`, `tests/test_run_once.py`, `src/robert_agent/resources/worker-protocol.md`.

- [x] Add tests for exhausted stream reconnects before the first phase, consecutive failures, delayed dispatch, retry exhaustion, and a completed structured result never being resumed.
- [x] Detect terminal transport failures from structured worker error events, not quoted command output. Prepare delayed same-task resumes from durable evidence; cap consecutive transport failures and report exhaustion explicitly.
- [x] Explain journal/deferred snapshot and resume semantics in the worker protocol.
- [ ] Run full unittest, Ruff, CI's scoped mypy, compileall, build, package audit, and `git diff --check`; record any unavailable gate.
- [ ] Review the complete staged diff using `fast-review-local`, fix findings, then commit/push and open one PR against `wklken/Robert:main`. Verify remote head and report #3391's current state separately.

Review remediation: `LOCAL-1` (attempt-scoped dispatch prompt) and `LOCAL-2`
(transport exit during the startup probe) were confirmed by failing behavior
tests and fixed; both regression tests now pass. The final full-suite gate
includes these tests. No unrelated source, model config, or dependency changes.

## Baseline

`python3 -B -m unittest discover -s tests`: 697 tests, OK (56.355 seconds), before changes.
