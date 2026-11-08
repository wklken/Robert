## runtime
Runtime tools (use only when needed):
- For control-plane state, use `${status_command}`. Use `${status_help}` for other queries.
- Avoid ad hoc SQLite queries; use them only for fields the status CLI cannot answer, with short output.

## snapshot
Record meaningful phase changes: `${snapshot_command} --phase analyze --status running --summary "Reading context"`.

## heartbeat
Wrap long commands for supervision: `${heartbeat_command} --phase verify -- <command...>`. Default summary returns bounded JSON tails and saved log paths. For short output use `--output-mode tee` before `--` to forward stdout/stderr without JSON; inspect the attempt's `*.command.json` journal for metadata. Use `--cwd <directory>` when needed. Journals record redacted argv, cwd and observed exit status; raw logs are local and may contain sensitive output. A runtime_recorded label or journal path does not attest verification or freshness.
