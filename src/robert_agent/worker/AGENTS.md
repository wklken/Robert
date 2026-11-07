# AGENTS.md

These rules apply to this directory and its descendants. Follow the
[repository dependency and state rules](../../../AGENTS.md) here.

- Keep result, snapshot and heartbeat entrypoints focused on command-line
  translation. Progress and command execution live in `execution/`.
  Durable result ingestion lives in `delivery/application/submission.py`.
- Preserve invocation and output contracts, including command exit status and
  result validation errors. Lower layers call their owning capabilities
  directly. Keep these canonical entrypoints in this package.
- Test argument translation, response and exit behavior at the CLI boundary.
  Test persistence, heartbeat and replay in the owning domains.
