# Stage 4 evidence

Stage 4 starts from the stage-3 renderer archived in `stage-4-renderer-before.py`.
Regenerate with `PYTHONPATH=src .venv/bin/python docs/reviews/2026-10-29-worker-contract-prompts/render-stage-4.py`.
Both snapshots use the exact same 14 stored inputs in
`../2026-10-29-worker-prompts/inputs.json`; input SHA256 is embedded in each.
The after renderer also contains the parent-owned correction that scopes source
issue selection guidance to routes allowing open_pr.

## Behavior and boundaries

- Runtime journals now record argv, resolved cwd, source=runtime_recorded,
  attested=false and output mode alongside existing identity/time/exit/log data.
  Recognized credential flags, assignments, auth headers and URL credentials are
  hidden in journal argv and dry-run command previews. Redaction is best effort,
  not a sanitizer for arbitrary script strings. Raw logs remain unsanitized local
  data; logs and atomically replaced journals are created with mode 0600.
- Default summary keeps the existing bounded JSON tails, counts and truncation
  flags. Explicit --output-mode tee forwards binary stdout/stderr separately and
  saves full logs, without adding result JSON to either stream. Tee uses child
  status as CLI exit status, 124 on timeout, 128+signal on signal termination.
- Tee polls saved regular logs instead of adding subprocess pipes or process
  groups. Bounded nonblocking forwarding keeps sink backpressure from delaying
  supervision. If a sink cannot drain by the command deadline or one second
  after completion, tee_incomplete in the terminal journal/result identifies
  output available only from full logs. Child-side buffering and global stream
  interleaving are not controlled. Use summary for large or unbounded output.
- --cwd explicitly sets and records the child working directory. Custom output
  directories move logs only; command journals remain discoverable in the task
  artifact directory for supervisor recovery. Commands hold no DB transaction.
- Existing verification schema requires command/status/purpose/required/source,
  executed exit_code or skipped_reason, journal path for runtime_recorded and an
  HTTPS URL for external_ci. These remain declarations, with attested=false;
  neither a label nor a path provides independently verified proof, freshness,
  coverage or environment correctness. No command_ref gate/tree hash was added.

## Verification

Cwd `/Volumes/ExtSSD/workspace/Robert`, branch `codex/worker-contract`, runtime
`.venv/bin/python`. No external service/model execution or writes were made.

- Before source edits: 6 new execution journal tests failed (3 failures and
  3 errors) on absent argv/cwd and tee interfaces; `stage4-red.log`.
- Final focused run: 27 tests passed, including all existing heartbeat tests,
  process-group/descendant timeout cleanup, start failure, unexpected snapshot
  failure, lock contention, terminal journal/recovery, prompt scope, and 7 new
  tests for argv/cwd/redaction/privacy, exact binary tee, live pre-exit streaming,
  200KB output per stream, timeout partial output, 2MB sink backpressure and
  command-side SQLite writer access. `stage4-focused-final.log`.
- Scoped Ruff passed; scoped compileall passed; git diff --check passed.
- Final resource build produced wheel and sdist; `stage4-build-final.log`.
- Parent-owned scoped review prompt regression passed after the tiny open_pr
  guidance correction: `/tmp/robert-worker-contract/parent-scoped-prompt.log`.
- Full unittest discovery is owned by parent on the combined final snapshot.
  The focused pass is not a full-suite claim.

Logs, source hashes, before source copies and stage-specific diffs are under
`/tmp/robert-worker-contract/stage4-*`. Three implementation/verification cycles
were used (about 9 minutes including initial inspection and documentation).

## Limits

No production log-read frequency, extra-tool-read count, real model latency A/B,
actual supervisor cancellation deployment or environment attestation was measured.
The new tests show local supervised command behavior; existing process-group and
recovery tests support preservation of those boundaries. Actual cancellation is
still owned by the existing supervisor. The stdout/stderr logs can contain
secrets despite metadata redaction and require local artifact handling.
