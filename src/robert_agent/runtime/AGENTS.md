# AGENTS.md

These rules apply to this directory and its descendants. Follow the
[repository dependency and state rules](../../../AGENTS.md) here.

- Own scheduling, run and child lifetimes, deadlines and mode selection.
  Delegate business SQL, lifecycle transitions, authorization and publication
  to their owners. `application/repository_cycle.py` composes the per-repository
  pipeline.
- Preserve process and lease ownership, bounded waits and cleanup. Keep scan,
  local advancement and publication independently schedulable. The paused lane
  may observe existing attempts and ingest durable results without starting
  new recovery.
- Launch owned subprocesses from their canonical modules or trusted absolute
  script paths as appropriate to the launch boundary. Update callers directly
  when a path moves. The package root has no compatibility scripts.
- Test lane scheduling, pause, timeout and child cleanup with controlled process
  and clock inputs. Keep domain policy assertions with their owning use cases.
