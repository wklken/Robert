"""Provider session evidence and explicit resume descriptors, without launch authority."""

from datetime import datetime
import os
from pathlib import Path

from robert_agent.adapters.processes import session_history as history
from robert_agent.worker_adapters._session import (
    PROVIDERS,
    directory_path,
    directory_identity,
    native_home_from_environment,
    prompt_digest,
    validate_resume,
)


def resolve_native_session(
    *,
    data_root,
    agent,
    task_id,
    run_id,
    prompt_path,
    prompt_sha256,
    cwd,
    stdout_path,
    started_at=None,
    native_home=None,
):
    """Observe registered launch stdout early; native prompt proof gates resume.

    Callers supply the registered stdout and frozen prompt from the run row,
    then durably save this evidence in dispatch. This function performs no writes.
    """
    if agent not in PROVIDERS:
        return None
    try:
        if isinstance(started_at, str):
            started_at = datetime.fromisoformat(started_at)
        if started_at is not None and (not isinstance(started_at, datetime) or started_at.utcoffset() is None):
            return None
        cwd = directory_path(cwd)
        home = directory_path(native_home) if native_home is not None else native_home_from_environment(agent, {})
        home_identity = directory_identity(home)
        if not task_id or not run_id or prompt_digest(prompt_path) != prompt_sha256:
            return None
        sid = history.stdout_session_id(data_root, stdout_path, agent=agent, trusted_root_alias=False)
        # When stdout exists it is authoritative: malformed/conflicting IDs
        # must not trigger discovery of another native session.
        if stdout_path and not sid:
            return None
        native = history.find_native_history(
            agent,
            started_at,
            task_id,
            run_id,
            sid,
            prompt_sha256=prompt_sha256,
            cwd=cwd,
            strict_root=True,
            native_home=home,
        )
        if native and not sid:
            sid = native["session_id"]
        if not sid:
            return None
        result = {
            "version": 1,
            "agent": agent,
            "provider": PROVIDERS[agent],
            "session_id": sid,
            "task_id": task_id,
            "run_id": run_id,
            "prompt_path": str(prompt_path),
            "prompt_sha256": prompt_sha256,
            "cwd": cwd,
            "add_dirs": list(dict.fromkeys((str(Path(prompt_path).parent), cwd))),
            "history_root": None,
            "history_path": None,
            "history_identity": None,
            "native_home": home,
            "native_home_identity": home_identity,
        }
        if native:
            with history.open_history(native["root"], native["path"]) as stream:
                if not history.native_prompt_binding(stream, agent, sid, prompt_sha256, cwd):
                    return result
                info = os.fstat(stream.fileno())
                result.update(
                    history_root=str(native["root"]),
                    history_path=str(native["path"]),
                    history_identity={"dev": info.st_dev, "ino": info.st_ino},
                )
        return result
    except (OSError, TypeError, ValueError):
        return None


def resume_descriptor(binding, *, task_id, run_id, prompt_path, prompt_sha256, cwd, segment, native_home=None):
    """Revalidate a saved original-run binding and create one new invocation.

    Caller owns stopped process/group proof, source checkpoint, missing output
    classification, fencing and the durable retry budget before spawning.
    """
    if not isinstance(binding, dict) or not binding.get("history_path"):
        raise ValueError("native session history binding unavailable")
    default_home = str(Path.home().absolute() / ("." + str(binding.get("agent"))))
    if native_home is None and binding.get("native_home") != default_home:
        raise ValueError("custom native runtime home requires registered launch evidence")
    if (binding.get("task_id"), binding.get("prompt_path"), binding.get("prompt_sha256"), binding.get("cwd")) != (
        task_id,
        str(prompt_path),
        prompt_sha256,
        str(cwd),
    ):
        raise ValueError("native session frozen input changed")
    descriptor = {**binding, "run_id": run_id, "parent_run_id": binding["run_id"], "segment": segment}
    try:
        descriptor = validate_resume(
            descriptor,
            agent=binding["agent"],
            prompt_path=prompt_path,
            cwd=cwd,
            native_home=native_home,
        )
        return descriptor
    except OSError as exc:
        raise ValueError("native session history unavailable") from exc
