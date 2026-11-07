# AGENTS.md

These rules apply to this directory and its descendants. Follow the
[repository dependency and state rules](../../../AGENTS.md) here.

- Own GitHub, Git, subprocess, SQLite, notification and service mechanisms.
  Depend on external protocols and stable utilities. Callers own domain policy
  and compose use cases.
- Keep SQL storage details in their owning adapter. Preserve schema and
  transaction contracts, including caller-owned commits and explicit durable
  checkpoints. `sqlite/model_pool.py` owns shared review and vote slot accounting.
- Keep process registry access in `processes/worker.py` through public handle
  operations. Preserve identity checks, bounded commands and the existing
  environment allowlists. Keep credentials out of logs and fixture data.
- Test protocol shape and failure translation at the adapter. Use controlled
  fixtures for network responses and real local processes or repositories
  when testing OS or Git behavior.
