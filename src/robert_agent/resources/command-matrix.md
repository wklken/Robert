# Command Matrix

Commands print one JSON object to stdout, except `python -m robert_agent.worker.heartbeat --output-mode tee`, which forwards child streams and uses the child exit status (124 on timeout). Exit code `0` means success, `2` means retryable failure, `3` means config or input failure, `4` means security refusal, and other non-zero exits mean unclassified failure.

Use the interpreter from the Robert installation for `python -m` commands.

| Command | Purpose |
| --- | --- |
| `python -m robert_agent.acceptance.readiness` | Check live GitHub collaboration readiness gates without writing to GitHub or launching workers. |
| `python -m robert_agent.acceptance.suite` | Aggregate safe acceptance checks and explicitly report the remaining production GitHub write canary gap. |
| `python -m robert_agent.acceptance.controlled_e2e` | Run a fully isolated issue-to-PR workflow through a real `runtime.cycle` run, real git worktree creation, mock worker result recording, audit, fake GitHub PR publication, and finalization. |
| `python -m robert_agent.acceptance.discovery` | Run read-only live GitHub discovery through `gh` without writing the control-plane database or dispatching workers. |
| `python -m robert_agent.acceptance.worker` | Run an isolated fixture-backed live worker dispatch, then audit and dry-run publication without writing GitHub content. |
| `python -m robert_agent.acceptance.worktree` | Run real git worktree preparation in an isolated local checkout without touching the operator repository or GitHub. |
| `python -m robert_agent.acceptance.write_canary` | Plan or, with explicit `--confirm-github-write`, publish one marker-protected canary comment to a specified GitHub issue or PR through the normal audited publisher path. |
| `python -m robert_agent.operations.initialize` | Create a missing runtime config from the packaged `config.example.yml` without overwriting an existing config. |
| `python -m robert_agent.runtime.cycle` | Execute one agent cycle from `workflow.yml`. |
| `python -m robert_agent.runtime.loop` | Execute bounded repeated `runtime.cycle` runs while durable local work remains, then stop with a structured reason. |
| `robert maintenance prune --output json` | Preview a bounded batch of expired diagnostic rows; `--apply` deletes eligible history while preserving task, audit, dedupe and recovery state. No automatic VACUUM. |
| `python -m robert_agent.runtime.scan` | Scan GitHub, run publication with `--publish`, or list and retry stored scan batches. |
| `python -m robert_agent.runtime.control` | List control children and acknowledge a verified stopped child. |
| `python -m robert_agent.runtime.daemon` | Run the foreground resident Robert daemon. It owns a daemon lease, drains local runnable work through `runtime.loop --skip-external`, and runs conservative rate-limit-aware live `runtime.cycle` polls. Use the native user-service commands for unattended operation. |
| `python -m robert_agent.readmodels.status` | Print compact read-only JSON for global status, recent/latest runs, a task, attempt, workstream, event/comment lookup, event search, source task view, or artifact tail so agents do not need ad hoc SQLite queries. |
| `python -m robert_agent.readmodels.chat` | Format the read-only dashboard payload as chat-ready Markdown inside a JSON envelope for OpenClaw slash commands or keyword triggers. |
| `python -m robert_agent.configuration.command` | Validate config, repo entries, skill index, and local command availability. |
| `python -m robert_agent.adapters.github.discovery` | Collect GitHub issue, PR, review, assignment, mention, and notification events. |
| `python -m robert_agent.security.authorization` | Apply trusted trigger and accepted context actor gates. |
| `python -m robert_agent.workflows.routes` | Convert authorized events into task routes and expected outputs. |
| `python -m robert_agent.work.streams` | Attach sources and events to serial workstreams. |
| `python -m robert_agent.adapters.git.workspace` | Prepare or reuse worktrees and branches. New branches assume an `upstream` remote and `upstream/<base_branch>` start point; existing PR updates reuse the discovered head branch; reviewer-assignment source reviews fetch `pull/<number>/head` into a local `review/pr-...` branch. |
| `python -m robert_agent.context.prompts` | Render worker prompt artifacts. |
| `python -m robert_agent.context.curation` | Propose, list, show, approve, and reject human-reviewed runtime knowledge candidates from project memory. |
| `python -m robert_agent.adapters.processes.worker` | Start or dry-run worker attempts with the configured worker command. |
| `python -m robert_agent.worker.snapshot` | Record a worker phase, status, summary, and next step through the internal worker runtime. |
| `python -m robert_agent.worker.heartbeat` | Run a long worker command while recording heartbeat snapshots and an attempt command journal; default JSON summary, opt-in `--output-mode tee` for raw stdout/stderr. |
| `python -m robert_agent.worker.result` | Record a worker's structured final result and planned GitHub actions. |
| `python -m robert_agent.execution.inspection` | Inspect active attempts, stale workers, timeouts, and cancel events. |
| `python -m robert_agent.delivery.application.audit` | Audit worker-planned GitHub actions against allowed actions before publication. |
| `python -m robert_agent.delivery.application.publish` | Publish audited GitHub actions and record publish status. |
| `python -m robert_agent.delivery.application.notifications` | Record and send local notifications. |
| `python -m robert_agent.acceptance.publish_dedupe` | Verify comment and PR idempotency marker dedupe with an isolated database and simulated GitHub lookup responses. |
| `python -m robert_agent.readmodels.summarize` | Summarize one cycle and recent workstreams. |
| `python -m robert_agent.interfaces.web.server` | Serve the read-only HTML dashboard from `dd.sqlite3`; `/data.json`, `/history`, and `--json` expose the raw status payload, and `/artifact.txt` previews registered text artifacts. |
