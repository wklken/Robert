# Command Matrix

All commands print one JSON object to stdout. Exit code `0` means success, `2` means retryable failure, `3` means config or input failure, `4` means security refusal, and other non-zero exits mean unclassified failure.

| Command | Purpose |
| --- | --- |
| `acceptance.py` | Check live GitHub collaboration readiness gates without writing to GitHub or launching workers. |
| `acceptance_suite.py` | Aggregate safe acceptance checks and explicitly report the remaining production GitHub write canary gap. |
| `controlled_e2e_acceptance.py` | Run a fully isolated issue-to-PR workflow through real `run_once`, real git worktree creation, mock worker result recording, audit, fake GitHub PR publication, and finalization. |
| `live_discovery_acceptance.py` | Run read-only live GitHub discovery through `gh` without writing the control-plane database or dispatching workers. |
| `live_worker_acceptance.py` | Run an isolated fixture-backed live worker dispatch, then audit and dry-run publication without writing GitHub content. |
| `live_worktree_acceptance.py` | Run real git worktree preparation in an isolated local checkout without touching the operator repository or GitHub. |
| `production_write_canary.py` | Plan or, with explicit `--confirm-github-write`, publish one marker-protected canary comment to a specified GitHub issue or PR through the normal audited publisher path. |
| `init_config.py` | Create a missing runtime config from `references/config.example.yml` without overwriting an existing config. |
| `run_once.py` | Execute one agent cycle from `workflow.yml`. |
| `loop_engine.py` | Execute bounded repeated `run_once.py` cycles while durable local work remains, then stop with a structured reason. |
| `daemon.py` | Run the foreground resident Robert daemon. It owns a daemon lease, drains local runnable work through `loop_engine.py --skip-external`, and runs conservative rate-limit-aware live `run_once.py` polls. Use the native user-service commands for unattended operation. |
| `status.py` | Print compact read-only JSON for global status, recent/latest runs, a task, attempt, workstream, event/comment lookup, event search, source task view, or artifact tail so agents do not need ad hoc SQLite queries. |
| `chat_status.py` | Format the read-only dashboard payload as chat-ready Markdown inside a JSON envelope for OpenClaw slash commands or keyword triggers. |
| `validate_config.py` | Validate config, repo entries, skill index, and local command availability. |
| `discover.py` | Collect GitHub issue, PR, review, assignment, mention, and notification events. |
| `authorize.py` | Apply trusted trigger and accepted context actor gates. |
| `route.py` | Convert authorized events into task routes and expected outputs. |
| `workstream.py` | Attach sources and events to serial workstreams. |
| `worktree.py` | Prepare or reuse worktrees and branches. New branches assume an `upstream` remote and `upstream/<base_branch>` start point; existing PR updates reuse the discovered head branch; reviewer-assignment source reviews fetch `pull/<number>/head` into a local `review/pr-...` branch. |
| `render_prompt.py` | Render worker prompt artifacts. |
| `memory_curator.py` | Propose, list, show, approve, and reject human-reviewed runtime knowledge candidates from project memory. |
| `dispatch.py` | Start or dry-run worker attempts with the configured worker command. |
| `worker_snapshot.py` | Record a worker phase, status, summary, and next step through the internal worker runtime. |
| `worker_heartbeat.py` | Run a long worker command while recording heartbeat snapshots. |
| `worker_result.py` | Record a worker's structured final result and planned GitHub actions. |
| `supervise.py` | Inspect active attempts, stale workers, timeouts, and cancel events. |
| `audit_result.py` | Audit worker-planned GitHub actions against allowed actions before publication. |
| `publish.py` | Publish audited GitHub actions and record publish status. |
| `notify.py` | Record and send local notifications. |
| `publish_dedupe_acceptance.py` | Verify comment and PR idempotency marker dedupe with an isolated database and simulated GitHub lookup responses. |
| `summarize.py` | Summarize one cycle and recent workstreams. |
| `web.py` | Serve the read-only HTML dashboard from `dd.sqlite3`; `/data.json`, `/history`, and `--json` expose the raw status payload, and `/artifact.txt` previews registered text artifacts. |
