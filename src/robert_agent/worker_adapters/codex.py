import json
from pathlib import Path

from robert_agent.worker_adapters import WorkerLaunch


AGENT_NAME = "codex"
DEFAULT_COMMAND = "codex"


def build_launch(
    command_argv,
    prompt_path,
    worktree_path,
    model,
    reasoning_effort,
    prompt_transport="stdin",
    environment_allowlist=(),
    timeout_seconds=5400,
):
    cwd = Path(worktree_path or prompt_path.parent)
    command = [*command_argv]
    if reasoning_effort != "default":
        command.extend(
            [
                "--config",
                f"model_reasoning_effort={json.dumps(reasoning_effort)}",
            ]
        )
    command.append("exec")
    if model != "default":
        command.extend(["--model", model])
    command.extend(
        [
        "--dangerously-bypass-approvals-and-sandbox",
        "--sandbox",
        "danger-full-access",
        "--json",
        "--cd",
        str(cwd),
        "--add-dir",
        str(prompt_path.parent),
        ]
    )
    if worktree_path:
        command.extend(["--add-dir", str(Path(worktree_path))])
    command.append("-")
    return WorkerLaunch(
        agent=AGENT_NAME,
        command=command,
        cwd=str(cwd),
        stdin_path=str(prompt_path),
        stdout_format="jsonl",
        environment_allowlist=tuple(environment_allowlist),
        timeout_seconds=int(timeout_seconds),
    )
