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

The source is a modular monolith organized by domain, with smaller responsibility
and layer modules inside each domain. See [the architecture map](docs/architecture.md)
and [the complete execution plan](docs/superpowers/plans/2026-11-07-final-source-architecture.md).
A unit of that plan is a verification checkpoint, not a partial delivery boundary.
Do not describe the reorganization as complete while planned ownership or
integration checks remain unfinished.

Paths below are relative to `src/robert_agent/`.

| Domain or boundary | Stable responsibilities | Application or composition responsibilities |
| --- | --- | --- |
| `work/` | Input validation, event/trust policy, workstream and operator queries, source batches, repository/task facts | `work/application/`: commands, admission, preparation, input refresh, handoff, completion and source lifecycle |
| `execution/` | Identity/ownership, capacity, leases, progress/command journals, result spool, shared model stages and round mechanics | `execution/application/`: dispatch, monitoring, recovery evidence, replay and readiness |
| `delivery/` | Submission contract, pure policies, locks, result/intent facts, selection and notification persistence | `delivery/application/`: audit, submission, trusted scope, guarded effects, intents, receipts, notifications, reconciliation and publication |
| `supervision/` | Rules, lineage, durable recovery records, receipt proof and health projections | `supervision/application/`: observation, checks and publication recovery coordination |
| `context/` | Prompt/skill rendering, review guidance, project memory and runtime knowledge | `context/application/`: transactional human knowledge review |
| `workflows/` | Concrete review, vote, PR remediation and maintenance policies/processes | Compose domain capabilities; shared execution does not select a workflow implementation |
| `configuration/` | v2 loading, normalization, schema validation and route/supervision settings | Boundary parsing only; no execution or workflow imports |
| `readmodels/` | Read-only dashboard, board, status, execution, usage and history projections | No HTTP dependency and no lifecycle mutations |
| `adapters/` | GitHub, Git, processes, SQLite, notifications and native services | External protocols and persistence details; no reverse imports of their application callers |
| `runtime/` | Cycle, loop, daemon, scanner, supervisor and child entrypoints | Scheduling, resource lifetime and invoking use cases; no business SQL |
| `application/` | Cross-domain repository-cycle composition | Explicit coordination only; domain-owned use cases live under their own domain |
| `interfaces/web/`, `cli/`, `worker/`, `integrations/` | HTTP/CLI/Worker/OpenClaw input and output contracts | Translate input, authenticate and call use cases; no business SQL in HTTP |
| `foundation/`, `security/` | Shared value/error/command capabilities, redaction and safe text | Keep shared concepts narrow; do not create a general business helper collection |
| `operations/`, `acceptance/` | Initialization, database upgrades, diagnostics, maintenance commands and verification tools | Preserve dry-run, read-only and explicit live-write authority boundaries |
| `resources/`, `web_assets/` | Installed package data and frontend assets | Resolve through the package/resource API, not relative to a moved implementation file |

## Dependency and reuse rules

- Group by domain first, then split a domain into responsibility/layer submodules.
  Reuse the domain's existing input types, errors, policies, facts and public
  capabilities. Do not scatter another implementation of the same concept into
  a top-level utility module or a parallel application directory.
- Keep stable rules/contracts and capability modules independent of application
  use cases, concrete workflows, runtime, interfaces and operations. Inside a
  domain, dependencies flow toward these stable owners, never back to callers.
  Cross-domain coordination belongs to a named use case or the outer composition
  layer. A read model uses query capabilities, not command handlers or HTTP code.
- The explicit Python import graph must be acyclic, including function-local
  imports. Removing top-level imports alone is not evidence of a resolved cycle.
  Do not use delayed imports, dynamic import/lookup, `sys.modules` aliases,
  mutable shared globals, forwarding facades or a registry to evade a boundary.
- Resolve dependency cycles by correcting ownership, extracting shared functions,
  constants/types/enums with a concrete common meaning, or supplying a narrow
  collaborator explicitly from composition. Do not add a lazy fallback import
  when a required collaborator is missing. A non-recovering monitor must not
  acquire a new recovery capability implicitly.
- Worker result, snapshot and heartbeat modules parse command-line input only.
  Shared progress/command execution belongs to `execution/`; durable result
  submission belongs to `delivery/application/submission.py`. Lower layers must
  never import Worker entrypoints, including for a helper or constant.
- SQLite execution controls and shared model slots belong to `adapters/sqlite/`.
  Adapters cannot import their domain/use-case callers. Database upgrades belong
  to `operations/migration.py`, which coordinates stable persistence and work facts.
  Public process-handle capabilities own access to the dispatch registry.
- Same-layer use cases may call a named capability in another domain only in
  the direction permitted by the complete module DAG. Share facts/policies below
  both use cases; do not import a command handler to read its private helper.
- Keep `execution.stages` independent of concrete review/vote implementations.
  Workflows supply store/workspace/artifact capabilities. Keep model table/store
  pairings consistent; ordinary Worker capacity and model subprocess capacity
  remain distinct. Persistence adapters may share stable schema mechanics.
- Prefer normal Python functions, small values and protocols where they express
  real contracts. No required class/service/repository hierarchy, generic event
  bus, plugin registry or empty layer scaffold for hypothetical use cases.
- Use public capabilities across modules. Keep private functions local; extract
  a shared public owner rather than importing a caller's underscore helper.
  Configuration may depend on resource access and Worker adapter capabilities,
  not application execution. Pure policies must not gain I/O.
- [Architecture tests](tests/test_architecture_boundaries.py) enforce dependency
  direction, import acyclicity, thin root entrypoints and the runtime/HTTP SQL
  boundary. Do not add broad package exceptions to make a failing check pass.
  Document and test the actual dependency contract when introducing a capability.

## State, authority and transaction boundaries

- Keep task, workstream, input, lineage, attempt, round, publication intent and
  recovery identities distinct. Preserve cross-attempt budgets and parent-child
  ownership. A live process alone does not prove progress or authority.
- Preserve claims, leases, fencing and idempotency checks before effects. Result
  arrival does not prove process exit or permit resource release. Unknown process
  identity remains visible through the existing attention/recovery path.
- Persistence helpers using a supplied SQLite connection must not silently commit
  or open another transaction. Application use cases own durable transitions,
  including receipts, audit evidence and required savepoint/checkpoint behavior.
  Mutating ownership operations are not read-only predicates.
- Keep network/Git/process waits outside long write transactions. Recheck the
  task/attempt, owner and relevant current state under the writer before applying
  an observation. Preserve existing callback, deadline and cancellation behavior.
- Publication and recovery retain current trust, action-scope, reauthorization,
  redaction and audit checks. An unknown remote effect must be reconciled before
  retrying. UI formatting cannot become the source of lifecycle state.

## Configuration and extension rules

- Version 2 is the configuration contract. Loading, schema application,
  normalization and validation have one owner in `configuration/`; no alternate
  CLI/workflow parser or restored legacy aliases.
- `resources/routes.yml` is the fixed route-policy source. Python representations
  derive from it. Operators may select supported workers/skills, but cannot widen
  action permissions, verification, output contracts or workspace policies.
- Preserve defaults, overrides, worker order, author whitelist semantics and
  explicit units. New options need a demonstrated operational need and validation;
  do not expose internal implementation choices as configuration by default.
- A new workflow composes domain capabilities. A new external protocol belongs
  in its adapter. A new invariant belongs to the owning domain's rules or use case.
  Importing policies/configuration must not connect to production or launch work.

## Entry points, migration and verification

- The root package contains only package primitives and the finite CLI contracts
  in [entrypoints.json](src/robert_agent/resources/entrypoints.json). These scripts
  contain a main import and execution guard, not business code or private exports.
  Keep each entry only for a documented script, persisted command or module-launch
  consumer. Production modules import the real owner, never a compatibility entry.
- Preserve `robert_agent.cli.main:main`, Worker result/snapshot/heartbeat commands,
  status paths in persisted prompts, and daemon/subprocess launch contracts.
  Internal private import paths are migrated directly. Do not keep them merely
  because old tests patched or imported them.
- Inventory source callers, tests, registrations, dynamic/module commands,
  persisted prompt paths, resources and service targets before moving a symbol.
  Update mocks where production actually looks up the collaborator. Static search
  does not prove a public executable is unused.
- Check old/new references and dead/forwarding residue after moves. Retain useful
  contract tests and compare discovered test IDs before/after; moving a test must
  not silently stop unittest discovery. Avoid wholesale test directory churn.
- Follow [development commands](docs/development.md) and [CI](.github/workflows/ci.yml).
  Structural changes require focused boundaries plus the full code-change gates.
  Controlled subprocess tests use the intended interpreter (`sys.executable`),
  not an unrelated `python3` selected by PATH.
- Packaging/resources require clean installed-wheel smoke outside the source tree,
  including executable entrypoints and resources. CI covers Linux/macOS and the
  supported Python matrix, lint/types/tests/build, dependency/secret scans and links.
- Update AGENTS and current architecture docs with the actual final structure.
  Preserve historical review evidence: pin moved-file links to an appropriate
  historical commit instead of pointing old line numbers at new implementations.
  Do not suppress documentation or architecture checks to hide migration residue.

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
