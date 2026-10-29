# Stage 2 caller and prompt evidence

Snapshot: branch `codex/worker-contract`, HEAD
`ff42f36d1fbe310077c9590af3c17cc5aa4303e0`, plus inherited stage 1/2 core
and this uncommitted caller unit. No deployment or real model run occurred.

Both [before](stage-2-before.md) and [after](stage-2-after.md) contain the same
14 unchanged inputs from [inputs.json](../2026-10-29-worker-prompts/inputs.json).
Before was captured using the inherited stage-1 renderer immediately before
caller adaptation. [render-stage-2.py](render-stage-2.py) records the generation
method; use the renderer corresponding to the intended snapshot and pass before
or after. Do not regenerate before with the changed renderer. The historical
original 14-case documents and stage-1 documents were preserved.

Whole-document UTF-8 sizes: before 106,677 bytes; after 106,580 bytes. These
include explanatory instructions and examples, not model/tool output or tokenizer
counts. This unit demonstrates valid compact examples, not measured latency.
All 14 examples parse and pass the current compact semantic shape validator after
replacing the review-input-kind selection placeholder with `request`. Full route
business golden/negative matrices remain for the fixtures migration unit.

Commands used `.venv/bin/python` from `/Volumes/ExtSSD/workspace/Robert`:

- Initial `-B -m unittest tests.test_compact_callers` reproduced missing classify
  kind, missing recommended-new-pr slug, ignored single-issue explicit target,
  and old program-owned prompt fields (3 failures, 12 errors).
- Cycle 1: compact caller/core and full native coordinator modules: 64 tests,
  63 passed. The remaining classification-to-vote test directly submits an old
  flat envelope and awaits fixture migration; no compatibility parser was added.
- Cycle 2: `tests.test_compact_callers tests.test_compact_result`: 18 passed.
- Cycle 3: those modules plus `tests.test_controlled_e2e_acceptance`, native review
  two-round file handoff, native vote audit/publication, and vote clarification:
  23 tests passed in 7.202 seconds. The control-path regression calls the real
  Workbench Cancel and Create APIs while run is blocked, then verifies the old
  canceled attempt is retained and the replacement gets a new compact attempt.
- `-m ruff check src tests`: passed.
- CI mypy paths (`cli`, `paths.py`, `route_config.py`, `skills.py`, `service.py`,
  `migrate.py`): 9 source files passed.
- `-m compileall -q src`: passed.
- `-m build --outdir /tmp/robert-worker-contract/stage2-callers-dist-final`:
  wheel and sdist passed.
- `git diff --check`: passed. Deleted renderer helpers and all public result
  producers were searched; no old source caller remained.

Logs are `/tmp/robert-worker-contract/stage2-callers-*`; content hashes are in
`stage2-callers-files.json` there. Full unittest is deliberately deferred until
old fixtures are migrated, as assigned; the focused pass is not a full-suite pass.

Known follow-up: cutover should ignore FileNotFoundError when concurrent normal
spool ingestion removes a file after glob. It currently fails closed with a
transient blocked diagnostic; evidence is not lost. This was identified after
cycle 3 and left to the next bounded repair. Submission HEAD capture/comparison,
full schema/business parity and route goldens remain separate follow-up work.
No automatic prepared migration or strong verification-source attestation is
claimed. Prepared cutover requires old-version drain or reconciled Cancel and a
new replacement task, as documented in the result contract.
