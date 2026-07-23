# Worker Contract

Robert sends a task prompt to a configured local worker through standard input.
The prompt names the task, attempt, workstream, route, allowed GitHub actions,
workspace, verification policy, skill guidance, and registered context files.

The worker records a structured result with:

- task and attempt identifiers;
- expected output type;
- planned GitHub actions;
- consumed event fingerprints;
- skills actually used;
- verification evidence;
- optional memory and review-point evaluation.

Workers must not publish GitHub changes directly. Robert audits and publishes
accepted actions. The environment contains only standard variables plus names
listed in the worker's `environment_allowlist`.

Worker state and artifacts live under `~/.local/share/robert/`; configuration
lives at `~/.config/robert/config.yml`.
