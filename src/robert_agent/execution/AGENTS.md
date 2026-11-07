# AGENTS.md

These rules apply to this directory and its descendants. Follow the
[repository dependency and state rules](../../../AGENTS.md) here.

- Keep task and attempt ownership, leases, process evidence, capacity, progress
  and command journals in the stable modules. Dispatch, monitor and replay
  coordination belong under `application/`.
- Keep `stages.py` independent of concrete review and vote workflows. Coordinators
  supply `StageStore`, `StageWorkspace` and `StageArtifacts`. Worker attempt
  capacity and the SQLite model slot pool measure different resources. Pass
  capabilities explicitly. Do not use delayed imports, dynamic lookup or
  re-exports to evade dependency rules.
- Recovery callers explicitly provide `resume_attempt`. The paused monitor
  passes `allow_recovery=False` and `resume_attempt=None`. Preserve recovery budgets
  across attempts and revalidate the owner and observed state before mutation.
- Keep process identity, process-group exit and result arrival distinct. Release
  capacity only when existing ownership rules establish that it is safe.
- Cover ownership and progress at the execution boundary. Use real controlled
  children when a change affects process groups, cancellation or slot release.
