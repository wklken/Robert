"""Small lifecycle wrapper around the process-wide DBOS runtime."""

from dataclasses import dataclass, field
import hashlib
import logging
import os
import socket
from urllib.parse import urlsplit

from dbos import DBOS, DBOSClient, DBOSConfig, SetWorkflowID


_runtime = None


@dataclass
class DBOSRuntime:
    mode: str
    system_database_url_env: str
    system_database_scheme: str | None
    executor_id: str | None
    config: dict = field(default_factory=dict)
    dbos: DBOS | None = field(default=None, repr=False, compare=False)
    launched: bool = field(default=False, repr=False)
    _signature: tuple[str, str, str | None, str | None, str | None] | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def public_payload(self):
        return {
            "mode": self.mode,
            "system_database_url_env": self.system_database_url_env,
            "system_database_scheme": self.system_database_scheme,
            "configured": self.mode != "legacy",
            "executor_id": self.executor_id,
            "launched": self.launched,
        }

    def __repr__(self):
        fields = self.public_payload()
        values = ", ".join(f"{key}={value!r}" for key, value in fields.items())
        return f"DBOSRuntime({values})"


def _workflow_engine(config_result):
    workflow_engine = (config_result or {}).get("workflow_engine") or {}
    if not isinstance(workflow_engine, dict):
        raise ValueError("workflow_engine must be a mapping")
    mode = str(workflow_engine.get("mode", "legacy")).strip()
    if mode not in {"legacy", "shadow", "active"}:
        raise ValueError("workflow_engine.mode must be one of: legacy, shadow, active")
    env_name = str(
        workflow_engine.get(
            "system_database_url_env",
            "ROBERT_DBOS_SYSTEM_DATABASE_URL",
        )
    ).strip()
    if not env_name:
        raise ValueError("workflow_engine.system_database_url_env must not be empty")
    return mode, env_name


def _resolve_database(config_result):
    mode, env_name = _workflow_engine(config_result)
    if mode == "legacy":
        return mode, env_name, None, None
    url = os.environ.get(env_name, "")
    if not url:
        raise ValueError(f"{env_name} must be set for workflow_engine.mode {mode}")
    scheme = urlsplit(url).scheme
    if scheme not in {"postgresql", "postgres", "sqlite"}:
        raise ValueError(
            "DBOS system database URL must use PostgreSQL or SQLite"
        )
    return mode, env_name, scheme, url


def _executor_id(config_result):
    for key in ("dbos_executor_id", "executor_id"):
        value = (config_result or {}).get(key)
        if value:
            return str(value)
    return os.environ.get(
        "ROBERT_DBOS_EXECUTOR_ID",
        f"robert-{socket.gethostname()}",
    )


def _signature(config_result):
    mode, env_name, scheme, url = _resolve_database(config_result)
    return (
        mode,
        env_name,
        scheme,
        hashlib.sha256(url.encode("utf-8")).hexdigest() if url else None,
        _executor_id(config_result) if mode != "legacy" else None,
    )


def configure_runtime(config_result) -> DBOSRuntime:
    """Configure DBOS once for this process, without launching it."""

    global _runtime
    mode, env_name, scheme, url = _resolve_database(config_result)
    signature = _signature(config_result)
    if _runtime is not None:
        if getattr(_runtime, "_signature", None) == signature:
            return _runtime
        raise ValueError("DBOS runtime is already configured with a conflicting configuration")

    if mode == "legacy":
        runtime = DBOSRuntime(
            mode=mode,
            system_database_url_env=env_name,
            system_database_scheme=None,
            executor_id=None,
            config={
                "name": "robert",
                "application_version": "v1",
                "run_admin_server": False,
            },
        )
        runtime._signature = signature
        _runtime = runtime
        return runtime

    # Register all versioned workflows before DBOS is constructed/launched.  The
    # import is intentionally lazy so legacy upgrades never touch DBOS setup.
    from robert_agent import durable_workflows

    del durable_workflows
    executor_id = _executor_id(config_result)
    use_listen_notify = scheme not in {"sqlite"}
    dbos_config = DBOSConfig(
        name="robert",
        system_database_url=url,
        application_version="v1",
        executor_id=executor_id,
        run_admin_server=False,
        use_listen_notify=use_listen_notify,
        enable_otlp=False,
        log_level="WARNING",
        console_log_level="WARNING",
        otlp_log_level="WARNING",
    )
    try:
        dbos = DBOS(config=dbos_config)
    except Exception:
        # DBOS construction can fail before a usable runtime exists.  Do not
        # retain a half-configured singleton in our wrapper.
        DBOS.destroy()
        raise
    runtime = DBOSRuntime(
        mode=mode,
        system_database_url_env=env_name,
        system_database_scheme=scheme,
        executor_id=executor_id,
        config={
            "name": "robert",
            "application_version": "v1",
            "run_admin_server": False,
            "use_listen_notify": use_listen_notify,
            "enable_otlp": False,
            "console_log_level": "WARNING",
        },
        dbos=dbos,
    )
    runtime._signature = signature
    _runtime = runtime
    return runtime


def launch_runtime(runtime: DBOSRuntime | None) -> None:
    """Launch a configured runtime; repeated launches are harmless."""

    global _runtime
    if runtime is None or runtime.mode == "legacy":
        return None
    if _runtime is not runtime:
        raise ValueError("DBOS runtime is not the configured process runtime")
    if runtime.launched:
        return None
    dbos_logger = logging.getLogger("dbos")
    previous_disabled = dbos_logger.disabled
    previous_level = dbos_logger.level
    dbos_logger.disabled = True
    try:
        DBOS.launch()
    except Exception:
        dbos_logger.disabled = previous_disabled
        dbos_logger.setLevel(previous_level)
        runtime.launched = False
        DBOS.destroy()
        _runtime = None
        raise
    dbos_logger.disabled = previous_disabled
    dbos_logger.setLevel(previous_level)
    runtime.launched = True
    return None


def destroy_runtime() -> None:
    """Destroy the configured DBOS runtime, if any."""

    global _runtime
    runtime = _runtime
    _runtime = None
    # Driver registration is process-local test/adapter state.  Reset it even
    # when this process was running in legacy mode or had no DBOS instance.
    from robert_agent import workflow_operations

    workflow_operations.reset_human_request_driver()
    workflow_operations.reset_remediation_driver()
    if runtime is None or runtime.mode == "legacy":
        return None
    DBOS.destroy()
    runtime.launched = False
    return None


def _workflow_for_binding(binding):
    subject_type = binding.get("subject_type")
    subject_id = binding.get("subject_id")
    workflow_kind = binding.get("workflow_kind")
    expected = {
        ("task", "human_request"): (
            f"robert:v1:task:{subject_id}",
            "human_request_workflow_v1",
        ),
        ("remediation_episode", "pr_remediation"): (
            f"robert:v1:remediation:{subject_id}",
            "pr_remediation_workflow_v1",
        ),
    }.get((subject_type, workflow_kind))
    if expected is None:
        raise ValueError("illegal workflow subject and kind pairing")
    workflow_id, function_name = expected
    if binding.get("workflow_id") != workflow_id:
        raise ValueError("workflow binding has a conflicting durable workflow ID")
    if binding.get("definition_version", "v1") != "v1":
        raise ValueError("unsupported workflow definition version")
    from robert_agent import durable_workflows

    return workflow_id, subject_id, getattr(durable_workflows, function_name)


def start_bound_workflow(binding) -> str:
    """Start the versioned workflow for an active DBOS-owned binding."""

    runtime = _runtime
    if runtime is None or runtime.mode == "legacy" or runtime.dbos is None:
        raise ValueError("DBOS runtime is not configured")
    if not isinstance(binding, dict):
        raise ValueError("workflow binding must be a mapping")
    if binding.get("owner_engine") != "dbos":
        raise ValueError("workflow binding is not DBOS-owned")
    if binding.get("binding_status", "active") != "active":
        raise ValueError("workflow binding is not active")
    workflow_id, subject_id, workflow = _workflow_for_binding(binding)
    with SetWorkflowID(workflow_id):
        runtime.dbos.start_workflow(workflow, subject_id)
    return workflow_id


def check_connectivity(config_result):
    """Read-only DBOS system-database connectivity check for Doctor."""

    mode, _, scheme, url = _resolve_database(config_result)
    if mode == "legacy":
        return {
            "status": "passed",
            "mode": mode,
            "configured": False,
            "skipped": True,
            "system_database_scheme": None,
        }
    client = None
    dbos_logger = logging.getLogger("dbos")
    previous_disabled = dbos_logger.disabled
    previous_level = dbos_logger.level
    dbos_logger.disabled = True
    try:
        client = DBOSClient(
            system_database_url=url,
            use_listen_notify=scheme not in {"sqlite"},
        )
        client.list_workflows(limit=1)
    except Exception:
        return {
            "status": "failed",
            "mode": mode,
            "configured": True,
            "system_database_scheme": scheme,
            "safe_error": "DBOS system database connectivity check failed",
        }
    finally:
        if client is not None:
            try:
                client.destroy()
            except Exception:
                pass
        dbos_logger.disabled = previous_disabled
        dbos_logger.setLevel(previous_level)
    return {
        "status": "passed",
        "mode": mode,
        "configured": True,
        "system_database_scheme": scheme,
    }


def current_runtime():
    """Return the configured runtime for process-local integrations/tests."""

    return _runtime
