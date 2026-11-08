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
- Run `python3 -m ruff check src tests` and
  `lint-imports --config pyproject.toml --no-cache` for Python or dependency changes.
- Format Python with `python3 -m ruff format src tests`; verify with
  `python3 -m ruff format --check src tests`.
- Run `python3 -m build` for packaging or resource changes.
- Run `git diff --check` before committing.
- Documentation-only changes do not require the Python test suite.

## Source structure and ownership

The source is a modular monolith organized by domain, with smaller responsibility
and layer modules inside each domain. See [the architecture map](docs/architecture.md)
for the published source contract. Execution plans and review records under
`docs/superpowers/`, `docs/reviews/`, and `docs/remediation/` stay local and are
ignored by Git. Published documentation describes current code and supported
operations; keep implementation plans, review ledgers, historical comparisons
and upgrade/migration procedures out of it. Use [the documentation index](docs/README.md)
to find and maintain the current guides. Keep installation, general usage,
architecture and development entrypoints at the top level of `docs/`. Put
feature-specific configuration and implementation details in `docs/features/`
and add them to its index.

Paths below are relative to `src/robert_agent/`.

| Domain or boundary | Stable responsibilities | Application or composition responsibilities |
| --- | --- | --- |
| `work/` | Input validation, event/trust policy, workstream and operator queries, source batches, repository/task facts | `work/application/`: commands, admission, preparation, input refresh, handoff, completion and source lifecycle |
| `execution/` | Identity/ownership, capacity, leases, progress/command journals, result spool, shared model stages and round mechanics | `execution/application/`: dispatch, monitoring, recovery evidence and readiness |
| `delivery/` | Submission contract, pure policies, locks, result/intent facts, selection and notification persistence | `delivery/application/`: audit, submission, trusted scope, guarded effects, intents, receipts, notifications, reconciliation and publication |
| `supervision/` | Rules, lineage, model decision grants, durable recovery records, receipt proof and health projections | `supervision/application/`: observation, checks, decision calls and replies, publication recovery coordination |
| `context/` | Unified prompt contracts/rendering, stage inputs, skills, review guidance, project memory and runtime knowledge | `context/application/`: transactional human knowledge review |
| `workflows/` | Concrete review, vote, PR remediation and maintenance policies/processes | Compose domain capabilities; shared execution does not select a workflow implementation |
| `configuration/` | v2 loading, normalization, schema validation and route/supervision settings | Boundary parsing only; no execution or workflow imports |
| `readmodels/` | Read-only dashboard, board, status, execution, usage and history projections | No HTTP dependency and no lifecycle mutations |
| `models/` | Static SQLAlchemy table mappings preserving existing storage encodings | No I/O or imports of persistence or domain callers |
| `persistence/` | Shared queries, writes, conditional mutations, transactions, schema and SQLite dialect mechanics | No reverse imports of domain/use-case callers; return materialized records or affected counts |
| `adapters/` | GitHub, Git, processes, notifications and native services | External protocols; no reverse imports of their application callers |
| `runtime/` | Cycle, loop, daemon, scanner, supervisor and child entrypoints | Scheduling, resource lifetime and invoking use cases; no business SQL |
| `application/` | Cross-domain repository-cycle composition | Explicit coordination only; domain-owned use cases live under their own domain |
| `interfaces/web/`, `cli/`, `worker/`, `integrations/` | HTTP/CLI/Worker/OpenClaw input and output contracts | Translate input, authenticate and call use cases; no business SQL in HTTP |
| `foundation/`, `security/` | Shared value/error/command capabilities, redaction and safe text | Keep shared concepts narrow; do not create a general business helper collection |
| `operations/`, `acceptance/` | Initialization, database upgrades, diagnostics, maintenance commands and verification tools | Preserve dry-run, read-only and explicit live-write authority boundaries |
| `resources/`, `web_assets/` | Installed package data and frontend assets | Resolve through the package/resource API, not relative to a moved implementation file |

## Dependency and reuse rules

- `foundation/contracts/{work,execution,delivery,supervision}.py` owns shared
  lifecycle, status and action values, grouped by domain. These pure leaf modules
  contain `StrEnum` definitions and immutable value groups only. Persistence,
  configuration and domain callers may import these contracts directly, without
  importing another domain's implementation. Keep distinct state machines in
  separate enum classes; use `.value` at database and serialization boundaries.
  Stored strings, schema constraints and protocol values must stay aligned.
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
- Database expressions and operations belong to `persistence/`; mapped tables belong to `models/`.
  Business callers must not import SQLAlchemy or sqlite3, build SQL fragments, or execute SQL.
  Share query and mutation capabilities across callers. SQL text is limited to existing schema
  upgrades and SQLite-specific controls inside persistence.
- Execution controls and shared model slots belong to `persistence/`.
  Adapters cannot import their domain/use-case callers. Database schema initialization and upgrades belong
  to `persistence/schema/`; execution controls use the current `execution_subjects`
  schema directly.
  Public process-handle capabilities own access to the dispatch registry.
- Same-layer use cases may call a named capability in another domain only in
  the direction permitted by the complete module DAG. Share facts/policies below
  both use cases; do not import a command handler to read its private helper.
- Keep `execution.stages` independent of concrete review/vote implementations.
  Workflows supply store/workspace/artifact capabilities. Keep model table/store
  pairings consistent; ordinary Worker capacity and model subprocess capacity
  remain distinct. Persistence operations share stable schema mechanics and ownership predicates.
- Prefer normal Python functions, small values and protocols where they express
  real contracts. No required class/service/repository hierarchy, generic event
  bus, plugin registry or empty layer scaffold for hypothetical use cases.
- Use public capabilities across modules. Keep private functions local; extract
  a shared public owner rather than importing a caller's underscore helper.
  Configuration may depend on resource access and Worker adapter capabilities,
  not application execution. Pure policies must not gain I/O.
- [Architecture tests](tests/architecture/test_architecture_boundaries.py) enforce dependency
  direction, import acyclicity, the package root allowlist and the business-caller SQL
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

- Agent-facing workflow instructions are rendered through `context.prompts`
  from structured inputs; their common, stage and protocol text lives in
  `resources/prompts/`. Callers supply authorized facts and preserve their own
  output validators; they must not append a separate handwritten instruction
  contract. Keep context independent of concrete workflows and application use cases.

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

## Operator messaging

- `work/messaging.py` owns attention/outbox correlation; event and notification
  writes share the caller transaction. `delivery/messages.py` owns scoped,
  redacted envelopes; adapters own transport only. All messaging queries and
  writes use named operations in `persistence/queries/` and `persistence/stores/`;
  inbox, delivery and readiness share filtering before limits.
- Issue dispatch binds a durable authenticated request to its confirmed Issue. Reconcile unknown creation effects before retrying; classifier or waiting results are not delivery. Completion requires final publication receipts and the original Issue result comment.
- Chat ingress checks explicit provider/session/sender/repository configuration
  and delegates to existing versioned work commands. Acknowledgement is a
  receipt, never incident resolution, recovery authority or process release.
- Preserve `robert.message.v1`, bounded delivery retries and idempotent operator
  responses. Future providers must prove equivalent authenticated context;
  never derive authority from message text or bypass lifecycle guards.

## Entry points and verification

- The package root contains only `__init__.py`, `paths.py`, and
  `resource_files.py`. Executable modules live with their owning domain or
  interface. [entrypoints.json](src/robert_agent/resources/entrypoints.json)
  lists canonical executable modules for packaging checks.
- Preserve the installed `robert` CLI contract at `robert_agent.cli.main:main`.
  Call Worker, status and daemon subprocess entrypoints at their current paths.
  When an entrypoint moves, update every caller directly and delete the old
  module. Do not add forwarding scripts or aliases for the old paths.
- Inventory source callers, tests, registrations, dynamic/module commands,
  persisted prompt paths, resources and service targets before moving a symbol.
  Update mocks where production actually looks up the collaborator. Static search
  does not prove a public executable is unused.
- Check old/new references and dead/forwarding residue after moves. Retain useful
  contract tests and compare discovered test IDs before/after; moving a test must
  not silently stop unittest discovery. Avoid wholesale test directory churn.
- Follow [test ownership rules](tests/AGENTS.md),
  [development commands](docs/development.md) and [CI](.github/workflows/ci.yml).
  Structural changes require focused boundaries plus the full code-change gates.
  Controlled subprocess tests use the intended interpreter (`sys.executable`),
  not an unrelated `python3` selected by PATH.
- Packaging/resources require clean installed-wheel smoke outside the source tree,
  including executable entrypoints and resources. PR/main CI runs Linux tests on
  the minimum/maximum supported Python versions, lint/types and secret scans.
  Release tags add the full Linux/macOS Python matrix, dependency audits, build,
  installed-wheel smoke tests and links; publishing waits for these checks.
- Update AGENTS and current architecture docs with the actual final structure.
  Keep source links, executable examples and diagrams aligned with their current
  owners. Do not suppress documentation or architecture checks to hide stale references.

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
