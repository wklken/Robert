# Test ownership

Read the repository AGENTS.md and the rules beside the production code under
test. Put a test for a single production capability in the matching domain
directory and name the file for that capability. Use `integration/` for a
workflow that exercises multiple owners and name the file for the contract it
protects. The repository cycle scenarios are grouped by behavior under
`integration/repository_cycle/`. Use `simulations/` for executable scenario
harnesses and `browser/` for frontend behavior.

- Use public inputs, outputs, persisted state and observable effects in
  assertions. Exercise relevant refusals and failure paths at the boundary.
- Put shared checkout, database and process fixtures in `support/`. Share
  setup and helper methods through a base with zero test methods. Avoid
  importing a concrete TestCase as a reusable fixture.
- Keep fixture data under `fixtures/`. Use temporary repositories and
  databases. Inject test runners at GitHub or process boundaries, and use
  `sys.executable` for Python children.
- Keep new package directories discoverable with `__init__.py`. Run a
  targeted check with `python3 -B -m unittest tests.<domain>.<test_module>`.
  Run `python3 -B -m tests.run --workers 8` for the final full suite when
  local CPU and memory allow. Use `--workers 4` on constrained machines.
- CI runs the same complete suite with `--workers 4`. Each module runs in an
  isolated subprocess. Keep module/class fixtures together, use temporary paths
  and dynamically assigned ports, and preserve the serial full-suite command.
  Use `--list` to compare test inventories after changes.
- One agent owns the final full-suite run per worktree; other agents run focused
  checks until changes are ready. Do not edit tested files during verification.
  The runner refuses simultaneous runs of the same test tree. Separate worktrees
  still share CPU, memory and the installed Python environment.
- After moving a test, compare discovered IDs with an explicit relocation
  map. Preserve each distinct assertion and check embedded subprocess
  imports, fixture paths and targeted test commands.

The opt-in Compose suite lives in `simulations/compose/`. Keep its fixture
protocol tests in `integration/`. The container image installs the Robert wheel
and copies only the Compose harness. Drive business changes through the installed
entry points and use read-only SQL for observations, except for explicit lock
contention tests that acquire and release a writer transaction without changing rows.
