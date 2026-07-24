# Claude and OpenCode Worker Adapters Implementation Plan

> **For agentic workers:** Execute this plan inline. Repository policy prohibits
> subagents unless the user explicitly authorizes delegation.

**Goal:** Add built-in `claude`, `tclaude`, and `opencode` worker adapters that
run unattended, receive Robert's prompt through stdin, and emit structured
stdout.

**Architecture:** Keep one small adapter module per CLI so the existing
filesystem-based adapter registry discovers them without central registration.
Claude and tclaude use the same Claude Code flags but remain separate modules
to preserve their command and audit identities. OpenCode uses its native
non-interactive `run` command.

**Tech Stack:** Python 3.10+, `unittest`, Claude Code CLI, Tencent tclaude
wrapper, OpenCode CLI.

## Global Constraints

- Preserve Robert's trust, workstream, audit, redaction, and idempotency
  boundaries.
- Pass prompts through stdin; never place prompt text in command arguments.
- Do not add runtime dependencies.
- Do not commit, push, or publish without separate user authorization.
- Run `python3 -B -m unittest discover -s tests`, `python3 -m build`, and
  `git diff --check` before completion.

---

### Task 1: Lock the Adapter Command Contracts

**Files:**
- Modify: `tests/test_operational_commands.py`
- Modify: `tests/test_config_and_schema.py`

**Interfaces:**
- Consumes: `worker_adapters.available_worker_agents()` and
  `dispatch.build_worker_command(...)`.
- Produces: Behavior tests for discovery, stdin transport, model/effort
  forwarding, unattended permissions, structured output, and working directory.

- [ ] **Step 1: Extend discovery coverage**

Assert that `claude`, `tclaude`, and `opencode` appear in
`available_worker_agents()` and that each loaded adapter reports the matching
`AGENT_NAME`.

- [ ] **Step 2: Add Claude-family launch tests**

For both adapter names, assert this command contract:

```python
[
    executable,
    "-p",
    "--model", model,
    "--effort", effort,
    "--permission-mode", "bypassPermissions",
    "--disallowedTools", disallowed_tools,
    "--input-format", "text",
    "--output-format", "stream-json",
    "--add-dir", prompt_directory,
    "--add-dir", worktree,
]
```

Also assert that prompt contents never appear in the argv list.

- [ ] **Step 3: Add the OpenCode launch test**

Assert this command contract:

```python
[
    executable,
    "run",
    "--model", model,
    "--variant", effort,
    "--format", "json",
    "--auto",
    "--dir", worktree,
]
```

Also assert that the prompt is supplied only through stdin.

- [ ] **Step 4: Preserve unknown-adapter validation**

Change the existing unknown-adapter fixture from the newly supported `claude`
name to `notarealagent`, preserving the rejection assertion.

- [ ] **Step 5: Run the new tests and verify they fail**

Run:

```bash
.venv/bin/python -B -m unittest \
  tests.test_operational_commands.OperationalCommandTests.test_worker_agents_are_discovered_from_adapter_modules \
  tests.test_operational_commands.OperationalCommandTests.test_dispatch_builds_claude_family_worker_commands \
  tests.test_operational_commands.OperationalCommandTests.test_dispatch_builds_opencode_worker_command_with_stdin_prompt \
  tests.test_config_and_schema.ConfigAndSchemaTests.test_config_rejects_unknown_worker_agent
```

Expected: discovery and command tests fail because the three adapter modules do
not exist yet; unknown-adapter validation remains green.

### Task 2: Implement the Three Adapters

**Files:**
- Create: `src/robert_agent/worker_adapters/claude.py`
- Create: `src/robert_agent/worker_adapters/tclaude.py`
- Create: `src/robert_agent/worker_adapters/opencode.py`

**Interfaces:**
- Consumes: the existing `WorkerLaunch` dataclass and adapter loader contract.
- Produces: `AGENT_NAME`, `DEFAULT_COMMAND`, and `build_launch(...)` in each
  module.

- [ ] **Step 1: Implement `claude.py`**

Build a Claude Code print-mode command with the configured model and effort,
`bypassPermissions`, Robert's existing disallowed orchestration tools, text
stdin, stream-json stdout, and both the prompt and worktree directories.
Return `WorkerLaunch(stdout_format="stream-json")`.

- [ ] **Step 2: Implement `tclaude.py`**

Use the same command contract as `claude.py`, with
`AGENT_NAME = DEFAULT_COMMAND = "tclaude"`.

- [ ] **Step 3: Implement `opencode.py`**

Build `opencode run` with optional `--model` and `--variant`, plus
`--format json`, `--auto`, and `--dir <cwd>`. Return
`WorkerLaunch(stdout_format="jsonl")`.

- [ ] **Step 4: Run the focused tests**

Run the Task 1 command again. Expected: all four tests pass.

### Task 3: Publish the Built-in Adapter Surface

**Files:**
- Modify: `README.md`
- Modify: `README_EN.md`
- Modify: `docs/guides.md`

**Interfaces:**
- Consumes: the built-in adapter names implemented in Task 2.
- Produces: Accurate public adapter lists and a minimal configuration example.

- [ ] **Step 1: Update adapter lists**

List `claude`, `tclaude`, and `opencode` alongside the existing built-in
adapters in both READMEs and the Worker Adapters guide.

- [ ] **Step 2: Add one alternate-worker example**

Add a compact `opencode` worker definition that demonstrates that its model
uses `provider/model` form and that Robert's `effort` maps to OpenCode's model
variant.

- [ ] **Step 3: Run repository verification**

Run:

```bash
.venv/bin/python -B -m unittest discover -s tests
.venv/bin/python -m build
.venv/bin/ruff check src tests
git diff --check
```

Expected: all commands pass with no skipped or hidden failures.
