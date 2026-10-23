# Agent Harness Reliability: Industry Practices and Robert Acceptance Criteria

## Executive Summary

Robert should evolve as a model-agnostic control plane rather than a collection
of prompts or a Markdown-driven process. Model-specific reasoning belongs in
replaceable workers; the control plane owns durable state, permissions, budgets,
verification, recovery, and publication.

The first comparison is positive: Robert already has durable SQLite state,
isolated worktrees, worker adapters, action audit, and publication
deduplication. The principal maturity gaps are confirmed process-group
termination, checkpoint-level replay, and verification evidence that the
control plane can reproduce independently.

## Industry Concept Model

| Concept | Primary purpose | Does not provide by itself |
| --- | --- | --- |
| Prompt engineering | Communicate task intent, context, and constraints | Reliable execution, recovery, authorization, or acceptance |
| Agent loop | Observe, act, verify, and repair within one task | Cross-task state, bounded recovery, or durable approval |
| Graph or workflow engine | Model explicit states, transitions, branches, pauses, and resumes | Correct implementation or trustworthy verification |
| Harness engineering | Combine context, tools, environment, feedback, state, and policy | A single required framework or model |

A graph or state machine should manage work, not prescribe the agent's internal
reasoning. Any worker may use its own prompt, loop, tools, and decomposition if
it returns the standard Robert protocol result.

## Eight Essential Properties of a Reliable Workflow

1. **Durable state is the source of truth.** Tasks, attempts, approvals, and
   external actions survive restart.
2. **Nodes are small and replayable.** Each control-plane node has one job and
   a recorded completion boundary.
3. **Side effects are idempotent.** Branches, comments, pushes, notifications,
   and publication use durable keys and confirmation evidence.
4. **Failures are classified.** Transient failures retry with bounds;
   recoverable worker failures resume with evidence; human decisions pause;
   unknown failures stop with diagnostics.
5. **Human review is a durable interrupt.** Approval, edit, and rejection are
   explicit state transitions that resume a paused workflow safely.
6. **Workers are replaceable; the protocol is not.** Every worker returns a
   structured result, verification evidence, and next-step information.
7. **Verification beats narration.** Executable checks and reproducible
   evidence decide whether a result may pass.
8. **Budgets and cancellation are control-plane enforced.** Time, cost,
   retries, and process termination are hard limits, not prompt instructions.

Markdown is useful for rationale, decisions, and navigation, but it is not a
reliable primary enforcement mechanism.

## First Robert Comparison

| Property | Current evidence | Assessment |
| --- | --- | --- |
| Durable state | SQLite records workstreams, tasks, attempts, results, wakeups, actions, leases, and runs | Present |
| Replayable nodes | Run and step records exist, but the main orchestration remains concentrated in `run_once.py`; worker phases are diagnostic records | Partial |
| Idempotent effects | Work-item events, wakeups, attempt numbering, and publication actions have durable uniqueness or deduplication controls | Present; fault-injection proof remains |
| Failure classification | Startup failure, process exit, stale attempt, timeout, recovery, and no-progress paths exist | Partial |
| Durable human interrupt | `waiting_for_user`, operator questions, replies, approval, retry, and cancellation exist | Present |
| Replaceable workers | Worker adapters and a structured result contract support multiple local agents | Present |
| Independent verification | Route policies and audit contracts exist, but verification is often worker-supplied | Partial |
| Hard budgets and cancellation | Loop and worker budgets plus stale/timeout supervision exist | Partial; confirmed termination is missing |
| Observability | Snapshots, logs, artifacts, notifications, status, and a board exist | Partial; a unified attempt timeline and reliability metrics are missing |

The immediate concern is cancellation: dispatch creates a separate process
session, but timeout supervision currently signals the recorded worker PID.
Stable unattended operation requires proven process-group termination and
confirmation, not only a SQLite status update.

## Scope and Non-goals

This document defines the next reliability boundary for Robert's existing
control plane. It does not add requirement planning, replace Robert with a
general-purpose graph framework, require a particular model, or treat Markdown
completion as acceptance evidence.

## Reliability Principles

1. The control plane, not worker prose, owns state transitions and publication.
2. Every external side effect has a durable idempotency key and confirmation.
3. A retry resumes from durable evidence or starts a new bounded attempt; it
   never silently continues an unknown process.
4. Verification that gates publication is independently reproducible from
   stored evidence.
5. A cancellation request or budget breach terminates the entire worker process
   group before an attempt becomes terminal.

## Acceptance Area 1: Process-Group Cancellation and Timeouts

### Required behavior

- Record process-group identity, start time, and immutable launch metadata.
- Use one termination path for timeout, operator cancellation, daemon
  replacement, and shutdown.
- Request graceful group termination, wait for a bounded grace period, then
  force-kill remaining processes.
- Confirm that no group member remains before marking an attempt `failed` or
  `canceled`; otherwise require operator attention.
- Preserve late results as evidence, but never let a canceled or superseded
  attempt advance a task or publish an action.

### Acceptance tests

1. A timed-out fixture worker with a child process leaves neither process
   running and records two-stage termination evidence.
2. Canceling an active work item terminates its group; a late result is not
   accepted.
3. A daemon restart reconciles an active original group before any replacement
   dispatch.
4. An unobservable process becomes operator attention rather than a falsely
   terminal attempt.

## Acceptance Area 2: Checkpointed Recovery and Bounded Resume

### Required behavior

- Persist control-plane checkpoints for workspace preparation, worker dispatch,
  result recording, result audit, and action publication.
- Each checkpoint records input identity, output identity, completion time, and
  idempotency key; free-form phase text is diagnostic only.
- Restart replays unfinished steps only; it never redispatches or republishes a
  completed step solely because daemon memory was lost.
- A resume has a new attempt identity, parent reference, compact recovery
  artifact, and explicit remaining budgets.
- Missing usage evidence disables automatic continuation rather than counting
  as zero.

### Acceptance tests

1. Restart after every checkpoint and assert no duplicate dispatch or publish.
2. A worker exit after creating a diff creates one bounded resume attempt using
   stored recovery evidence rather than a full previous transcript.
3. Repeated recoverable failure stops when its configured budget is exhausted.
4. An ambiguous worktree or stale remote head blocks automatic resume and
   records the conflicting evidence.

## Acceptance Area 3: Independently Reproducible Verification Evidence

### Required behavior

- Gating verification contains a command or named verifier, working directory
  or artifact identity, exit status, bounded output evidence, and the covered
  source revision.
- The verifier runs in a route-controlled environment; worker prose alone
  cannot satisfy the policy.
- Each accepted action links to its verification records and exact action scope.
- Missing, stale, malformed, or revision-mismatched evidence blocks publication.
- Re-running stored verification produces the same conclusion or an explicit
  environment-drift result.

### Acceptance tests

1. Prose-only verification is rejected for a route requiring executable checks.
2. Evidence from another commit or worktree is rejected without publication.
3. Valid controlled verification survives restart and is reused without
   re-running the external action.
4. Changed verifier environment or output is reported as drift, not accepted as
   fresh evidence.

## Release Gate and Order

Before unattended operation is called stable, fixture-backed integration tests
must cover the criteria above and a fault-injection matrix for process death,
database restart, duplicate wakeup delivery, late result delivery, publication
ambiguity, and operator interruption. Each test asserts both durable state and
the absence of unintended external actions.

Implement in this order:

1. Process-group cancellation and confirmed termination.
2. Checkpoint model and restart/replay tests.
3. Controlled verification records and audit enforcement.
4. Fault-injection matrix, operational metrics, and release qualification.

## References

- [OpenAI: Harness engineering](https://openai.com/index/harness-engineering/)
- [Anthropic: Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)
- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- [LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
