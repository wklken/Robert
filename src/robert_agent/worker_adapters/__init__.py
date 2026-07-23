"""Plugin loader for DD GitHub worker launch adapters."""

from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
import shlex


@dataclass(frozen=True)
class WorkerLaunch:
    agent: str
    command: list
    cwd: str
    stdin_path: str
    stdout_format: str
    environment_allowlist: tuple[str, ...] = ()
    timeout_seconds: int = 5400

    def metadata(self):
        return {
            "agent": self.agent,
            "cwd": self.cwd,
            "stdin_path": self.stdin_path,
            "stdout_format": self.stdout_format,
            "environment_allowlist": list(self.environment_allowlist),
            "timeout_seconds": self.timeout_seconds,
        }


def available_worker_agents():
    package_dir = Path(__file__).parent
    return tuple(
        sorted(
            path.stem
            for path in package_dir.glob("*.py")
            if path.stem != "__init__" and not path.stem.startswith("_")
        )
    )


SUPPORTED_WORKER_AGENTS = available_worker_agents()


def _split_command(command):
    if isinstance(command, (list, tuple)):
        parts = [str(part) for part in command if str(part).strip()]
    else:
        parts = shlex.split(str(command or "").strip())
    if not parts:
        raise ValueError("worker_command must not be empty")
    if parts[0].startswith("~"):
        parts[0] = str(Path(parts[0]).expanduser())
    return parts


def _load_adapter(agent):
    if not agent.isidentifier() or agent.startswith("_"):
        raise ValueError(f"unsupported worker_agent: {agent}")
    try:
        module = import_module(f"{__name__}.{agent}")
    except ModuleNotFoundError as exc:
        if exc.name == f"{__name__}.{agent}":
            raise ValueError(f"unsupported worker_agent: {agent}") from exc
        raise
    required = ["AGENT_NAME", "DEFAULT_COMMAND", "build_launch"]
    missing = [name for name in required if not hasattr(module, name)]
    if missing:
        raise ValueError(
            f"worker_agent {agent} adapter missing required symbols: {', '.join(missing)}"
        )
    if module.AGENT_NAME != agent:
        raise ValueError(f"worker_agent adapter name mismatch: {agent} != {module.AGENT_NAME}")
    return module


def normalize_worker_agent_config(config):
    config = config or {}
    agent = str(config.get("worker_agent", config.get("agent", "cbc"))).strip().lower()
    if not agent:
        raise ValueError("worker_agent must not be empty")
    adapter = _load_adapter(agent)
    command = config.get("worker_command", config.get("command_argv", config.get("command")))
    if command is None:
        command = adapter.DEFAULT_COMMAND
    if agent == "command" and not isinstance(command, (list, tuple)):
        raise ValueError("command adapter requires command to be a YAML sequence")
    command_argv = _split_command(command)
    prompt_transport = str(config.get("prompt_transport", "stdin")).strip()
    timeout_seconds = int(config.get("timeout_seconds", 5400))
    if timeout_seconds < 1:
        raise ValueError("timeout_seconds must be at least 1")
    environment_allowlist = config.get("environment_allowlist", [])
    if not isinstance(environment_allowlist, list) or any(
        not isinstance(name, str) or not name.strip()
        for name in environment_allowlist
    ):
        raise ValueError("environment_allowlist must contain variable names")
    return {
        "agent": agent,
        "command": " ".join(shlex.quote(part) for part in command_argv),
        "command_argv": command_argv,
        "prompt_transport": prompt_transport,
        "timeout_seconds": timeout_seconds,
        "environment_allowlist": [
            name.strip()
            for name in environment_allowlist
        ],
    }


def build_worker_launch(
    prompt_path,
    worktree_path=None,
    model="gpt-5.4",
    reasoning_effort="high",
    worker_command=None,
    worker_agent="cbc",
    worker_agent_config=None,
):
    prompt_path = Path(prompt_path)
    config = normalize_worker_agent_config(
        worker_agent_config
        or {
            "worker_agent": worker_agent,
            "worker_command": worker_command,
        }
    )
    adapter = _load_adapter(config["agent"])
    return adapter.build_launch(
        command_argv=config["command_argv"],
        prompt_path=prompt_path,
        worktree_path=worktree_path,
        model=model,
        reasoning_effort=reasoning_effort,
        prompt_transport=config.get("prompt_transport", "stdin"),
        environment_allowlist=config.get("environment_allowlist", []),
        timeout_seconds=config.get("timeout_seconds", 5400),
    )
