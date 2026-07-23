from pathlib import Path

from robert_agent.worker_adapters import WorkerLaunch


AGENT_NAME = "command"
DEFAULT_COMMAND = ()


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
    if prompt_transport != "stdin":
        raise ValueError("command adapter supports prompt_transport=stdin only")
    if not command_argv:
        raise ValueError("command adapter requires a non-empty command array")
    cwd = Path(worktree_path or Path(prompt_path).parent)
    return WorkerLaunch(
        agent=AGENT_NAME,
        command=list(command_argv),
        cwd=str(cwd),
        stdin_path=str(prompt_path),
        stdout_format="text",
        environment_allowlist=tuple(environment_allowlist),
        timeout_seconds=int(timeout_seconds),
    )
