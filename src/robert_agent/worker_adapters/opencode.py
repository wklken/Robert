from pathlib import Path

from robert_agent.worker_adapters import WorkerLaunch

AGENT_NAME = "opencode"
DEFAULT_COMMAND = "opencode"


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
    command = [*command_argv, "run"]
    if model != "default":
        command.extend(["--model", model])
    if reasoning_effort != "default":
        command.extend(["--variant", reasoning_effort])
    command.extend(
        [
            "--format",
            "json",
            "--auto",
            "--dir",
            str(cwd),
        ]
    )
    return WorkerLaunch(
        agent=AGENT_NAME,
        command=command,
        cwd=str(cwd),
        stdin_path=str(prompt_path),
        stdout_format="jsonl",
        environment_allowlist=tuple(environment_allowlist),
        timeout_seconds=int(timeout_seconds),
    )
