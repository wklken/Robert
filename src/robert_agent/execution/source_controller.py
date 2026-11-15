"""Own the local controller resource used by native source entrypoints."""

from contextlib import closing, contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import time
from uuid import uuid4

from robert_agent.execution import control_children, runtime_state
from robert_agent.persistence import errors, transactions


def resource_key(db_path):
    return f"database:{Path(db_path).resolve()}"


def standalone_owner(db_path):
    return f"source-cli:{resource_key(db_path)}"


@dataclass(slots=True)
class StandaloneController:
    executor: str | None
    owner: str | None
    status: str
    summary: dict | None = None


@contextmanager
def standalone(db_path, config_path, *, ttl_seconds, cleanup=None):
    """Acquire the exact DB daemon lease and retain it for a bounded local pass."""
    executor = f"source-cli-{uuid4().hex}"
    key = resource_key(db_path)
    with closing(runtime_state.connect(db_path)) as conn, conn:
        transactions.begin_immediate(conn)
        lease = runtime_state.acquire_daemon_lease(conn, resource_key=key, owner_id=executor, ttl_seconds=ttl_seconds)
        if lease["ok"]:
            runtime_state.start_daemon_run(conn, config_path=str(config_path or ""), owner_id=executor)
    if not lease["ok"]:
        yield StandaloneController(None, None, lease["status"])
        return
    result = StandaloneController(executor, standalone_owner(db_path), "acquired")
    completed = False
    try:
        if cleanup is None:
            with closing(runtime_state.connect(db_path)) as conn:
                safe = not control_children.previous_owner_children(conn, scope_key=key, current_owner=executor)
        else:
            safe = cleanup(db_path, scope_key=key, current_owner=executor)
        if not safe:
            yield StandaloneController(None, None, "control_child_attention")
            return
        yield result
        completed = True
    finally:
        with closing(runtime_state.connect(db_path)) as conn, conn:
            runtime_state.finish_daemon_run(
                conn,
                executor,
                "stopped" if completed and result.status == "acquired" else "failed",
                summary=result.summary,
            )
            runtime_state.release_daemon_lease(conn, lease["lease_id"])


def _publisher_child_owner(db_path, executor):
    if not Path(db_path).is_file():
        return None
    with closing(runtime_state.connect(db_path, busy_timeout_ms=250)) as conn:
        return control_children.source_controller_owner(
            conn,
            child_id=executor,
            lane="publish",
            database_path=db_path,
            wrapper_pid=os.getppid(),
            wrapper_group=os.getpgrp(),
        )


class PublicationStop:
    """Cooperative deadline and current controller fence for one publication pass."""

    def __init__(self, deadline, current, upstream=None):
        self.deadline = deadline
        self.current = current
        self.upstream = upstream

    def is_set(self):
        if time.monotonic() >= self.deadline or (self.upstream is not None and self.upstream.is_set()):
            return True
        try:
            current = self.current()
        except errors.Error:
            return True
        return (
            not current or time.monotonic() >= self.deadline or (self.upstream is not None and self.upstream.is_set())
        )

    def wait(self, seconds):
        time.sleep(min(seconds, max(0, self.deadline - time.monotonic())))
        return self.is_set()


@contextmanager
def publication(db_path, config_path, *, seconds, stop=None):
    """Resolve a managed publication child, or take standalone DB ownership."""
    deadline = min(time.monotonic() + seconds, float(os.environ.get("_ROBERT_CONTROL_DEADLINE", "inf")))
    if time.monotonic() >= deadline or (stop is not None and stop.is_set()):
        yield None, PublicationStop(deadline, lambda: False, stop), "source_publication_stopped"
        return
    executor = os.environ.get("_ROBERT_EXECUTOR_ID")
    if executor:
        owner = _publisher_child_owner(db_path, executor)

        def check():
            return owner is not None and _publisher_child_owner(db_path, executor) == owner

        yield owner, PublicationStop(deadline, check, stop), "acquired" if owner else "control_owner_required"
        return
    with standalone(db_path, config_path, ttl_seconds=seconds + 30) as controller:

        def current():
            with closing(runtime_state.connect(db_path)) as conn:
                return control_children.owner_lease_active(
                    conn, controller.executor, resource_key=resource_key(db_path)
                )

        yield controller.owner, PublicationStop(deadline, current, stop), controller.status
