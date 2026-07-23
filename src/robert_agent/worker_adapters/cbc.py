from pathlib import Path

from robert_agent.worker_adapters import WorkerLaunch


AGENT_NAME = "cbc"
DEFAULT_COMMAND = "cbc"

DEFAULT_DISALLOWED_WORKER_TOOLS = ",".join(
    [
        "TaskCreate",
        "TaskGet",
        "TaskUpdate",
        "TaskList",
        "TaskStop",
        "TaskOutput",
        "TeamCreate",
        "TeamDelete",
        "SkillManage",
        "CronCreate",
        "CronDelete",
        "CronList",
        "EnterWorktree",
        "LeaveWorktree",
    ]
)


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
    command = [*command_argv, "-p"]
    if model != "default":
        command.extend(["--model", model])
    if reasoning_effort != "default":
        command.extend(["--effort", reasoning_effort])
    command.extend(
        [
        "--permission-mode",
        "bypassPermissions",
        "--disallowedTools",
        DEFAULT_DISALLOWED_WORKER_TOOLS,
        "--input-format",
        "text",
        "--output-format",
        "stream-json",
        "--add-dir",
        str(prompt_path.parent),
        ]
    )
    if worktree_path:
        command.extend(["--add-dir", str(Path(worktree_path))])
    return WorkerLaunch(
        agent=AGENT_NAME,
        command=command,
        cwd=str(worktree_path or prompt_path.parent),
        stdin_path=str(prompt_path),
        stdout_format="stream-json",
        environment_allowlist=tuple(environment_allowlist),
        timeout_seconds=int(timeout_seconds),
    )
