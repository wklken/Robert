# AGENTS.md

## Scope

These rules apply to the entire Robert repository.

## Required behavior

- Read the target modules and tests before changing them.
- Keep changes limited to the active task.
- Add a failing behavior test before bug fixes.
- Preserve the trust, workstream, audit, redaction, and idempotency boundaries.
- Do not add runtime dependencies without documenting why the standard library
  and current dependencies are insufficient.
- Do not store GitHub or OpenClaw credentials in configuration or fixtures.
- Run `python3 -B -m unittest discover -s tests` for code changes.
- Run `python3 -m build` for packaging or resource changes.
- Run `git diff --check` before committing.
- Documentation-only changes do not require the Python test suite.

## Source structure and ownership

The current layout is the first stage of the source architecture migration.
Use [the architecture map](docs/architecture.md) for the implemented structure
and [the migration plan](docs/superpowers/plans/2026-11-06-source-architecture-refactor.md)
for remaining work. Planned directories are not existing APIs. Keep each change
within a reviewable migration unit; do not move the whole repository to match a
future tree in one patch.

All paths below are relative to `src/robert_agent/`.

| Location | Owns | Placement rule |
| --- | --- | --- |
| `configuration/` | Loading, schema application, validation, route and supervision configuration | Normalize and validate at the boundary; do not start work or import runtime orchestration. |
| `work/policies.py`, `work/errors.py` | Work admission predicates and shared work errors | Keep policy decisions independent of database, process, and presentation code. |
| `work/queries.py`, `work/repositories.py` | Workstream/event queries and repository persistence | Use the caller's connection; queries do not change state. |
| `work/admission.py`, `work/application/lifecycle.py` | Work admission and lifecycle use cases | Coordinate policy and state transitions here, preserving authority and transaction boundaries. |
| `execution/ownership.py` | Task/episode ownership, parent-child admission, attention and closure | Ownership operations may mutate state; do not treat them as read-only predicates. |
| `execution/leases.py`, `execution/notifications.py` | Repository leases and durable notification records | Keep resource ownership and notification persistence separate from workflow decisions. |
| `execution/stages.py` | Shared model-stage execution mechanics and collaborator protocols | Receive workflow-specific store, workspace, and artifact collaborators explicitly. |
| `delivery/policies.py` | Pure submission/question and review-evaluation policy | Share policy with result validation without importing publication or workflow implementations. |
| `workflows/routing.py` | Effective workflow selection and native-request eligibility | Compose normalized configuration and policy; do not duplicate configuration parsing. |
| `adapters/processes/`, `adapters/git/`, `adapters/github/` | Process observations/termination, Git remotes, GitHub reads | Translate external state; leave admission and lifecycle decisions to their owners. |
| `security/text.py` | Redacted text suitable for exposure | Reuse the existing redaction implementation rather than adding a separate sanitizer. |
| `cli/`, worker entrypoints, daemon and board modules | User/process entrypoints and runtime or view composition | Call the owning capability instead of becoming a reusable source of business helpers. |
| `resources/` | Packaged schemas, route policy, prompts and other runtime data | Load through the package resource API; verify installed-wheel behavior after changes. |

Existing root modules still implement substantial application behavior.
`run_once.py` remains the main orchestrator; `worker_supervision.py` and
`loop_engine.py` still consume some of its monitoring/readiness helpers.
`work_items.py`, `execution_store.py`, `execution_operations.py`, publication,
review/vote, supervision and presentation modules have not all been relocated.
These are explicit migration seams, not permission to grow a new general-purpose
helper collection in the package root.

## Dependency and module rules

- Organize code around responsibility and the invariants it owns. Use small
  Python modules, functions, dataclasses or protocols where they clarify a real
  contract. DDD does not require an ORM, service/repository class for every
  function, generic event bus, plugin registry, or a directory for each layer.
- Keep pure policies stable and independent of I/O. Application use cases
  coordinate decisions and persistence. Adapters own external protocols.
  Entry points and views compose these capabilities. Do not make a lower-level
  capability import its caller to obtain a helper.
- `configuration/`, `work/`, `execution/`, `delivery/`, `adapters/`, and
  `security/` must not import `run_once`, `daemon`, or `board_execution`.
  Existing migrated root consumers have the same restriction in
  [the architecture guard](tests/test_architecture_boundaries.py).
- Configuration may depend on its own modules, package resources, and worker
  adapter capabilities. It must not depend on workflow execution. Keep
  `work.policies` and `delivery.policies` free of application dependencies.
- Keep `execution.stages` independent of concrete review/vote coordinators,
  stores and workspace implementations. Supply its protocols at the workflow
  boundary. Preserve the correspondence between stage stores and model-pool
  run tables; ordinary workers and model subprocess capacity are distinct.
- Import reusable behavior from its owner. Do not borrow private helpers from
  orchestration or board code, or add forwarding wrappers solely to preserve
  obsolete internal paths. Preserve wrappers that implement a public CLI,
  persisted entrypoint, lifecycle, instrumentation, or compatibility contract.
- The explicit Python import graph must stay acyclic, including imports inside
  functions. Delaying an import or using dynamic lookup to evade the guard does
  not resolve a dependency inversion. Dynamic entrypoints also need smoke tests;
  an acyclic static graph alone does not validate them.
- When a new dependency is necessary, document its ownership and update the
  narrow module contract in the architecture guard. Do not add package-wide
  exceptions to make a failing boundary test pass. Extract shared policy or
  inject a collaborator when callers would otherwise depend on each other.

## State, authority and transaction boundaries

- Keep task, workstream, input, lineage, attempt, round and recovery identities
  distinct. Preserve cross-attempt recovery budgets and parent-child ownership.
  Do not derive authority merely from a process being alive or a result existing.
- Preserve claims, leases, fencing and idempotency checks before side effects.
  Result arrival does not establish process exit or permit releasing resources
  still owned by a live worker. Unknown process identity must remain visible
  through the existing attention/recovery path.
- Persistence helpers using a supplied SQLite connection do not commit it or
  silently open an independent transaction. The application use case owns the
  transaction that makes a transition durable, including required receipts and
  audit records. Name and test mutating ownership operations accordingly.
- Keep network calls, Git operations and process waits outside long write
  transactions. When applying observations, recheck the current task/attempt,
  owner and relevant state under the write boundary before accepting the result.
- Publication and recovery must preserve current trust, action-scope,
  reauthorization, redaction and audit checks. Configuration extensibility must
  not widen what an untrusted input or worker result is authorized to do.
- UI/read models may format durable facts but must not become the source of
  execution or lifecycle policy. Keep status queries read-only.

## Configuration and extension rules

- Configuration version 2 is the supported contract. Keep loading, schema
  application, normalization and validation in `configuration/`; do not restore
  legacy aliases or add a second parser in CLI, workflow or runtime code.
- `resources/routes.yml` owns fixed route policy. Python route representations
  derive from it. Operators can select supported workers and skills; configuration
  must not widen action permissions, verification, output or workspace policy.
- Keep defaults in their existing owning schema/normalization module and preserve
  explicit units, including seconds for timeout and interval fields. New options
  need a demonstrated operational requirement, validation and documented meaning.
  Do not expose internal implementation choices as configuration by default.
- Add a workflow by composing the existing policies and execution collaborators.
  Add an external integration in its adapter boundary. Add a pure decision to
  the capability that owns the invariant. Avoid speculative registries and
  generic abstractions for hypothetical future workflows.
- Package initializers should stay lightweight. Importing a configuration or
  policy module must not start workers, connect to production state or perform
  remote operations.

## Migration and verification rules

- Before moving or deleting a symbol, inventory production callers, tests,
  exports, CLI registration, persisted prompt commands and dynamic entrypoints.
  Update callers together. Static search alone does not prove a public symbol
  or an executable module is unused.
- Preserve `robert_agent.cli.main:main` and the root worker result, snapshot,
  heartbeat and status entrypoints used by generated/persisted commands unless
  an explicit migration handles their consumers. Keep `validate_config`'s
  executable module boundary while its implementation lives in configuration.
- For a structural change, run the focused architecture boundary tests and the
  full code-change gates. Test observable lifecycle/authority behavior as well
  as imports. Check old/new references and introduced dead wrappers after moves.
- Use [the development commands](docs/development.md) and
  [CI](.github/workflows/ci.yml) as the gate definitions. CI covers Ruff, the
  configured Mypy scope, unit tests, compilation, packaging, wheel smoke,
  dependency/secret scans and documentation links on its supported matrix.
- Controlled subprocess tests should use the intended interpreter explicitly
  (for example `sys.executable`), with dependencies installed in that environment.
  A system `python3` found through `PATH` is not necessarily that interpreter.
- Resource and packaging changes require an installed-wheel smoke check outside
  the source tree. Verify both resource loading and executable entrypoints.
- Update current architecture/development documentation with structural changes.
  Historical review evidence must retain its original meaning: if a linked file
  moves, pin the link to the reviewed commit rather than pointing old line numbers
  at a different implementation. Do not suppress link checks to hide stale links.

## Deployment checks

- During installation or upgrades, check the service account and configured
  workers. The `claude` and `tclaude` adapters request `bypassPermissions`,
  which Claude Code normally rejects when running as root.
- When root execution in an already restricted container is intended, prefix
  each affected worker's configured command with `env IS_SANDBOX=1`, keeping
  its adapter, executable path, and existing arguments. Follow the
  [container configuration guide](docs/guides.md#claude-workers-as-root-in-a-restricted-container).
  This variable does not create a sandbox or reduce root privileges; do not
  apply it automatically to an unrestricted host.
- Configuration validation, doctor, version checks, and mocked launches do not
  prove a real Claude worker can start. Verify an actual launch within the
  authorized deployment scope, or report that verification as pending.

## Delivery authority

Local changes and commits do not authorize pushing, opening pull requests,
publishing packages, creating releases, or writing to GitHub.
