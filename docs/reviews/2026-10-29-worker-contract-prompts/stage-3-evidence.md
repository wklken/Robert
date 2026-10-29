# Stage 3 evidence

Stage 3 starts from the stage-2 renderer captured in `stage-3-renderer-before.py`.
That file is an evidence fixture, not a runtime compatibility path. Regenerate
both snapshots with the command documented in `render-stage-3.py`. Both use the
same fourteen entries in `../2026-10-29-worker-prompts/inputs.json`, including the
same supplied runtime paths. The real attempt-specific path layout is verified
separately by `tests/test_attempt_context.py`.

## Behavior

- GitHub archives are `<attempt_id>.github-context.md` and `.json`; resume writes
  new files and preserves the previous attempt's actual inputs.
- Prepared refresh uses the same task publication lock as dispatch. It rechecks
  status and owner after acquiring the lock and after Git preparation. External
  Git preparation holds no SQLite writer lock. Started attempts cannot refresh.
- Refresh updates existing artifact metadata. Each file is replaced atomically;
  caught file/DB failures restore the previous input files and roll back artifact
  changes, so a retry can replace the still-prepared inputs. This does not retain
  every preparation version or add a revision/manifest system.
- Accepted raw events are deterministically inlined, with the current refresh
  event first. The raw JSON budget is 4,000 characters per event and 12,000 in
  total. Every omitted event has an explicit Markdown anchor and required reading
  instruction. Requirements are not inferred from titles or metadata. Markdown
  is the default archive view; JSON is on demand. Web request/operator context
  keeps its existing inline representation.
- Consumed defaults remain the rendered input set, preserve order and allow a
  handled subset. Adding a later event does not mutate the rendered prompt.
- Update examples start with a comment; guidance calls for push only when actual
  code changes require publication. Publication schema instructions are scoped
  to the allowed actions.

## Verification

Cwd: `/Volumes/ExtSSD/workspace/Robert`; runtime: `.venv/bin/python`.
All runs are local with temporary DB/files and mocked external Git preparation.

- Before source edits, `python -B -m unittest tests.test_attempt_context`:
  9 tests, 6 expected behavior failures (snapshot naming, lock coordination,
  duplicate artifact history, inline short/long input and push example). An
  earlier setup-only failure was corrected before collecting that baseline.
- Final `python -B -m unittest tests.test_attempt_context tests.test_prompt_scope tests.test_review_input_workflow`:
  31 tests passed. New tests cover resume preservation, repeated refresh,
  both lock directions, no writer lock during Git, owner revocation, started
  status, Git failure, partial file replacement, DB failure, retries, inline
  requirements/operator reply, omitted raw content anchors and input consumption.
- `python -m ruff check src/robert_agent/run_once.py src/robert_agent/render_prompt.py tests/test_attempt_context.py`: passed.
- CI's scoped mypy command: passed, 9 source files.
- `python -m compileall -q src`: passed.
- `python -m build`: built wheel and sdist successfully.
- `git diff --check`: passed.
- Full discovery is deferred to the parent while it migrates old protocol
  fixtures; this focused pass is not a full-suite claim.

Logs: `/tmp/robert-worker-contract/stage3-{red,green-1,green-2,green-3,ruff,mypy,compile,build}.log`.
Stage-specific source diffs against the inherited dirty stage-2 state are
`/tmp/robert-worker-contract/stage3-{run_once,render_prompt}.diff`.
Three implementation/verification cycles were used.

## Limits

No real model, deployment, GitHub writes, tool-round measurement or latency A/B
was run. Byte counts alone are not latency evidence. Tests inject recoverable
exceptions; abrupt process death/power loss between individual file replacements
and the SQLite commit is not a verified cross-resource atomic transaction.
Only already-accepted event bodies and fields are assembled; no new remote source
fetch or LLM summary is introduced.
