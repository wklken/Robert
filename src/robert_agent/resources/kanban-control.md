# Repository Task Board and Local Control API

The task board is the operator-facing control surface for repository-scoped
work. It projects local assignments and trusted GitHub issue/PR work into one
canonical six-column view while the existing scheduler, worker, audit, and
publication paths remain authoritative.

## Start the board

Writable mode validates the normal runtime config, initializes/migrates its
SQLite database, synchronizes configured repositories, and allows task
commands for configured repositories and named workers:

```bash
robert web run \
  --config ~/.config/robert/config.yml \
  --writable \
  --host 127.0.0.1 \
  --port 8765 \
  --operator "$USER"
```

Open `http://127.0.0.1:8765/board`. The GitHub workbench is at `/`, and the
diagnostic dashboard remains at `/operations`.

Read-only mode is available when the operator must inspect an existing
database without creating work or changing Knowledge state:

```bash
robert web run \
  --db ~/.local/share/robert/robert.sqlite3 \
  --host 127.0.0.1 \
  --port 8765
```

In read-only mode, board/detail APIs still work, `/api/session` reports why
writes are disabled, and every HTTP write returns `503`.

## Projected columns

The column is a read model, not a mutable status field. Each non-canceled item
appears in exactly one column according to durable SQLite state:

| Column | Meaning |
| --- | --- |
| Backlog | A web-created requirement has not been activated and can still be edited. |
| TODO | An activated task is detected, classified, queued, or otherwise ready for execution. |
| Doing | A worker attempt is running or accepted GitHub publication is in progress. |
| Waiting for you | An unresolved Agent question, completion decision, startup failure, or publication failure requires operator action. |
| Review | An associated PR is open, or PR-specific attention such as an unmerged close must be handled. |
| Done | Completion has been accepted or an associated PR was merged. |

Canceled items remain in history and are excluded from the default six-column
response. Structured state wins over timestamps and text heuristics. Resolving
an attention event prevents an old failure or question from returning the card
to Waiting.

## Task commands

All mutations are versioned, transactional, and idempotent. Clients must send
the detail response's `version` as `expected_version` and a new
`X-Idempotency-Key` for each operator intent.

| Command | Normal use |
| --- | --- |
| `edit` | Change title, requirement, priority, or routing while in Backlog. |
| `start` | Activate a Backlog item and enqueue its first task. |
| `approve` | Resolve an operator decision; completion acceptance can finish the item. |
| `reply` | Answer an Agent question and create a serial child task. |
| `request_changes` | Resolve review attention or request another pass on an open PR. |
| `retry` | Resolve a retryable startup, execution, or publication failure. |
| `cancel` | Cancel non-running work; a live attempt must finish or be supervised first. |
| `reopen` | Reopen a completed item as a new serial child task. |

`409` means the item changed since the caller read it. The response includes a
safe current detail projection so the UI can refresh without guessing. Invalid
filters and command bodies return `422`.

## Routing and capacity

New work can use `routing_mode: auto` or `routing_mode: manual`. Auto routing
uses the normal route-to-worker mapping. Manual routing requires a worker name
from the validated config and is authoritative: if that worker is unavailable,
the task stays prepared and becomes actionable instead of falling back to a
different worker.

Only attempts with `status = running` consume capacity. The global
`max_concurrency` bounds the process, and `repos[].max_concurrency` may impose a
lower limit for one repository. Prepared, waiting, stale, and historical rows
do not consume a slot, although supervision still inspects stale/running
attempts. Independent workstreams may run concurrently and receive distinct
worktrees; a single workstream remains serial.

## Waiting and continuation

A worker `waiting_for_user` result must contain a structured
`operator_question`. The accepted result records an unresolved attention event
and projects the item into Waiting for you. An operator `reply`, `approve`,
`request_changes`, or `retry` resolves that exact event and creates a child task
when execution must continue.

The child stays on the same work item and serial workstream. Worktree planning
uses the same local source reference, so follow-up work reuses the same branch
and worktree when available. Multiple question/reply cycles append events and
child tasks; they do not create duplicate cards.

## GitHub association and completion

A trusted GitHub issue/PR intake creates or reuses a stable work item. Repeated
events for the same root source are deduplicated. A PR derived from an issue
stays attached to the issue's item; an independent PR receives its own item.

Publishing an audited `open_pr` action records the PR link and projects the
item into Review. Review changes create another task on the same workstream.
A remote merged PR records `pr_merged` and completes the item. A PR closed
without merge records unresolved `unmerged_pr_closed` attention and remains in
Review until the operator decides what to do.

Analysis-only and validated no-op local results may complete without a PR. A
code-producing GitHub route still follows its route verification and required
publication contracts.

## Local security boundary

The server defaults to `127.0.0.1` and is intended for local operator use. It
does not enable CORS. HTTP writes require all of the following:

- an exact `Host` matching the configured bind origin;
- an exact same-origin `Origin`;
- the process-scoped token returned by `/api/session` in
  `X-Robert-CSRF-Token`;
- the expected content type;
- a request body no larger than 64 KiB;
- an idempotency key for work-item mutations.

The API allowlists repositories and workers from the validated config, returns
safe errors without raw exception text, and writes only SQLite/wakeup state.
The scheduler remains responsible for worktree creation, worker launch, audit,
and GitHub publication. Binding to a non-loopback address is outside this local
trust model and requires an operator-provided authenticated reverse proxy.

## Recovery and diagnostics

The board retains its last successful data during refresh failures. Startup,
execution, and publication failures remain unresolved attention until an
operator command resolves them. Restarting `web.py` is safe because command
idempotency and item versions live in SQLite; the CSRF token intentionally
changes with the server process.

Use these focused checks before raw SQL:

```bash
robert status --config ~/.config/robert/config.yml

robert web run \
  --db ~/.local/share/robert/robert.sqlite3 --json

curl --fail http://127.0.0.1:8765/healthz
curl --fail http://127.0.0.1:8765/api/board
```

The canonical board endpoints are `/api/board`, `/api/work-items/<id>`, and
`/api/work-items/<id>/events`. Use `/operations`, `/data.json`, Knowledge, and
registered artifact views for lower-level run, audit, publication, and worker
evidence.
