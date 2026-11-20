# AGENTS.md

These rules apply to this directory and its descendants. Follow the
[repository dependency and state rules](../../../AGENTS.md) here.

- Build read-only status, board, workbench, dashboard and history projections.
  Reuse stable work, execution and delivery query capabilities. Keep command
  handlers, concrete workflows and HTTP out of the import graph.
- Project persisted facts without repairing or advancing them. Preserve stable
  ordering, pagination and redaction. Keep artifact previews limited to
  registered artifacts. Preserve size limits and symlink-safe access under the
  runtime data directory.
- Agent conversation history may read local Codex/tcodex session roots through
  `adapters/processes/session_history.py`. Bind task history to the initial
  prompt identities; bind model runs to the owning task/attempt, recorded thread
  ID and prompt hash. Keep reads bounded and refuse symlink paths. Expose only
  public conversation records, never runtime configuration or raw reasoning.
- Test projections from small persisted fixtures, including empty data, ordering,
  active ownership, sensitive fields and missing artifacts.
