"""Construct and run the direct Claude source-model no-tools invocation."""

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import subprocess

from robert_agent.adapters.processes import identity as process_identity, model_command
from robert_agent.foundation.contracts.execution import ModelCommandStatus, SourceModelExit
from robert_agent.worker_adapters import WorkerLaunch

_MODEL_ENVIRONMENT = frozenset({"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"})
_ROUTING_ENVIRONMENT = frozenset(
    {
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_BEDROCK_BASE_URL",
        "ANTHROPIC_VERTEX_BASE_URL",
        "ANTHROPIC_FOUNDRY_BASE_URL",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_USE_CUSTOM_GATEWAY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    }
)


def _reject_routing_overrides(configured=()):
    if _ROUTING_ENVIRONMENT.intersection(configured) or any(os.environ.get(name) for name in _ROUTING_ENVIRONMENT):
        raise UnsupportedProfile("source_model_custom_provider_unavailable")


_MAC_NATIVE = {
    b"\xfe\xed\xfa\xce",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf",
    b"\xbf\xba\xfe\xca",
}
_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}")


class UnsupportedProfile(ValueError):
    """The selected worker cannot run the reviewed direct no-tools invocation."""


@dataclass(frozen=True, slots=True)
class SourceModelProfile:
    worker_id: str
    executable: Path
    model: str
    effort: str
    timeout: int
    credential_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SourceModelOutcome:
    status: str
    output: bytes = field(default=b"", repr=False)
    exit_kind: str | None = None
    identity: dict | None = field(default=None, repr=False)


def _native_binary(executable):
    try:
        with executable.open("rb") as stream:
            magic = stream.read(4)
    except OSError:
        return False
    return magic == b"\x7fELF" or magic in _MAC_NATIVE


def profile_for(worker):
    command = worker.get("command_argv", ())
    model = worker.get("default_model", "default")
    effort = worker.get("default_effort", "default")
    worker_id = worker.get("name")
    timeout = worker.get("timeout_seconds")
    if (
        worker.get("agent") != "claude"
        or worker.get("prompt_transport", "stdin") != "stdin"
        or not isinstance(command, (tuple, list))
        or len(command) != 1
        or not isinstance(command[0], str)
        or not isinstance(worker_id, str)
        or not worker_id.strip()
        or not isinstance(model, str)
        or not _MODEL_NAME.fullmatch(model)
        or effort not in ("default", "low", "medium", "high", "max")
        or type(timeout) is not int
        or timeout <= 0
    ):
        raise UnsupportedProfile("unsupported_source_model_profile")
    path = Path(command[0])
    if not path.is_absolute() or path.name != "claude":
        raise UnsupportedProfile("source_model_requires_absolute_claude")
    try:
        executable = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise UnsupportedProfile("source_model_executable_unavailable") from None
    if not executable.is_file() or not os.access(executable, os.X_OK) or not _native_binary(executable):
        raise UnsupportedProfile("source_model_requires_native_claude")
    configured = worker.get("environment_allowlist", ())
    _reject_routing_overrides(configured)
    credentials = tuple(sorted(_MODEL_ENVIRONMENT & set(configured)))
    return SourceModelProfile(worker_id, executable, model, effort, timeout, credentials)


def _launch(profile, prompt_path, cwd):
    mcp = cwd / "empty-mcp.json"
    mcp.write_text('{"mcpServers": {}}', encoding="utf-8")
    command = [
        str(profile.executable),
        "--print",
        "--bare",
        "--tools",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        str(mcp),
        "--setting-sources",
        "",
        "--disable-slash-commands",
        "--no-chrome",
        "--no-session-persistence",
        "--input-format",
        "text",
        "--output-format",
        "text",
    ]
    if profile.model != "default":
        command.extend(("--model", profile.model))
    if profile.effort != "default":
        command.extend(("--effort", profile.effort))
    return WorkerLaunch(
        "claude",
        command,
        str(cwd),
        str(prompt_path),
        "text",
        environment_allowlist=tuple(sorted(_MODEL_ENVIRONMENT.intersection(profile.credential_names))),
        timeout_seconds=profile.timeout,
    )


def run(profile, prompt, stop, on_started):
    if not isinstance(profile, SourceModelProfile):
        raise UnsupportedProfile("unsupported_source_model_profile")
    try:
        _reject_routing_overrides()
    except UnsupportedProfile:
        return SourceModelOutcome("source_model_custom_provider_unavailable", exit_kind=SourceModelExit.SPAWN_FAILED)
    result = model_command.run(
        None, prompt, stop, on_started, build_launch=lambda source, cwd: _launch(profile, source, cwd), lifecycle=True
    )
    identity = result["identity"]
    if not result["process_created"]:
        # No Popen handle was returned by the direct launch. The original
        # grant holder may record that no child was started by this call.
        return SourceModelOutcome(result["status"], exit_kind=SourceModelExit.SPAWN_FAILED.value)
    if not result["stopped"] or identity is None:
        return SourceModelOutcome(ModelCommandStatus.PROCESS_ATTENTION.value)
    return SourceModelOutcome(result["status"], result["output"], SourceModelExit.GROUP_EXITED.value, identity)


def group_exited(identity):
    """Read only. A gone leader and an empty isolated group are both required."""
    if not process_identity.validate_identity(identity):
        return False
    pid = identity["pid"]
    if type(pid) is not int or pid <= 1 or identity["process_group_id"] != pid:
        return False
    try:
        return (
            process_identity.inspect_recorded_identity(identity) == "dead"
            and not process_identity.live_process_group(pid)
            and process_identity.inspect_recorded_identity(identity) == "dead"
        )
    except (OSError, subprocess.SubprocessError):
        return False
