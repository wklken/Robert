"""Shared supervised model stages for review and vote rounds."""

from robert_agent.foundation.contracts.execution import AttemptStatus, BindingStatus, ModelRunStatus, RoundStatus
from robert_agent.foundation.contracts.work import TaskLifecycle

from robert_agent.persistence import database as db_database
from robert_agent.persistence import errors as db_errors
from robert_agent.persistence import transactions as db_transactions
from robert_agent.persistence.dialects import sqlite as db_sqlite

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import sqlite_master as db_read_sqlite_master
from robert_agent.persistence.queries import tasks as db_read_tasks
from robert_agent.persistence.stores import artifacts as db_write_artifacts
from robert_agent.persistence.stores import attempts as db_write_attempts
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import threading
import time
from typing import Any, ContextManager, Protocol

from robert_agent.adapters.processes import worker as dispatch
from robert_agent.persistence import execution_control as execution_store
from robert_agent.adapters.processes import environment as model_environment
from robert_agent.adapters.processes import model_usage
from robert_agent.execution import retry as retry_policy
from robert_agent.adapters.processes import identity as worker_identity
from robert_agent.delivery.lock import task_publication_lock
from robert_agent.execution import progress as snapshot
from robert_agent.execution.usage import record_dispatch_usage, start_call, record_call_event
from robert_agent.execution import stage_evidence
from robert_agent.persistence.rounds.records import artifact_available
from robert_agent.adapters.processes import session_resume
from robert_agent.worker_adapters import supports_native_resume
from robert_agent.worker_adapters._session import native_home_from_environment


class StageStore(Protocol):
    def connect(self, db_path: str) -> ContextManager[db_database.DatabaseConnection]: ...
    def session(self, conn: db_database.DatabaseConnection, *, task_id: str) -> dict[str, Any] | None: ...
    def now(self) -> str: ...
    def digest(self, path: str | Path) -> str: ...
    def update(self, conn: db_database.DatabaseConnection, round_id: str, **fields: Any) -> None: ...
    def runs(self, conn: db_database.DatabaseConnection, round_id: str) -> list[dict[str, Any]]: ...
    def new_run(
        self,
        conn: db_database.DatabaseConnection,
        round_id: str,
        stage: str,
        worker_id: str,
        owner_attempt_id: str,
        root: str,
        prompt_sha256: str,
        model: str | None = None,
    ) -> dict[str, Any]: ...
    def update_run(self, conn: db_database.DatabaseConnection, run_id: str, **fields: Any) -> None: ...
    def reserve_slot(self, conn: db_database.DatabaseConnection, run_id: str) -> bool: ...
    def release_slot(self, conn: db_database.DatabaseConnection, run_id: str) -> None: ...
    def reserved_runs(self, conn: db_database.DatabaseConnection) -> list[dict[str, Any]]: ...
    def release_finished_slot(self, conn: db_database.DatabaseConnection, run: dict[str, Any]) -> None: ...


class StageWorkspace(Protocol):
    def verify_snapshot(self, manifest: dict[str, Any]) -> bool: ...


class StageArtifacts(Protocol):
    def safe_path(self, path: str | Path, root: str | Path) -> Path: ...
    def atomic_json(self, path: Path, payload: dict[str, Any]) -> None: ...


class StageStopped(RuntimeError):
    pass


class StageSuperseded(RuntimeError):
    pass


class ModelRateLimited(RuntimeError):
    pass


def _database_busy(error):
    message = str(error)
    return message in {
        "database is locked",
        "database table is locked",
        "database schema is locked",
    } or message.startswith(("database table is locked:", "database schema is locked:"))


def _raise_rate_limit(launched):
    logs = {}
    for kind in ("stdout", "stderr"):
        path = launched.get(kind + "_path")
        if not path:
            continue
        try:
            with Path(path).open("rb") as stream:
                stream.seek(max(0, Path(path).stat().st_size - 32768))
                logs[kind] = {"tail": stream.read(32768).decode("utf-8", errors="replace")}
        except OSError:
            continue
    failure = retry_policy.terminal_rate_limit(logs)
    if failure:
        raise ModelRateLimited("rate_limited: " + failure["message"])


def _launch_failed_without_process(recorded):
    return (
        recorded.get("ok") is False
        and recorded.get("status") == "failed_dispatch"
        and recorded.get("pid") is None
        and not recorded.get("cleanup_pending")
    )


def _group_finished(recorded):
    """A dead leader alone cannot prove its output files have stopped changing."""
    pid = recorded.get("pid")
    pgid = recorded.get("process_group_id", pid)
    if type(pid) is not int or pid <= 1 or pgid != pid:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _wait_for_group_exit(recorded, timeout=2):
    deadline = time.monotonic() + timeout
    while True:
        proc = dispatch.tracked_process(recorded.get("pid"))
        if proc is not None and proc.poll() is not None:
            dispatch.reap_finished_process(proc.pid)
        if _group_finished(recorded):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def attempt_is_active(conn, task_id, attempt_id):
    row = db_records.first(db_read_tasks.find_for_attempt_is_active(conn, task_id, attempt_id))
    binding = execution_store.get_binding(conn, "task", task_id)
    return bool(
        row
        and row[0] in {TaskLifecycle.QUEUED.value, TaskLifecycle.RUNNING.value}
        and row[1] in {AttemptStatus.PREPARED.value, AttemptStatus.RUNNING.value, AttemptStatus.STALE.value}
        and binding
        and binding["binding_status"] == BindingStatus.ACTIVE
    )


def _stopped_dispatch(run, *, observe_source):
    """Record proven group exit without replacing original launch/source pins."""
    recorded = dict(run["dispatch"])
    recorded.update(process_exited=True, group_exit_confirmed=True)
    if run["status"] in {ModelRunStatus.STARTING, ModelRunStatus.RUNNING}:
        recorded["interrupted"] = True
        recorded.setdefault("failure_kind", "interrupted")
    launch = recorded.get("launch_checkpoint")
    if observe_source and launch:
        try:
            path = launch["worktree_path"]
            if recorded.get("worktree_path", path) != path:
                raise ValueError("stopped source worktree changed")
            manifest = {key: launch.get(key) for key in ("snapshot_id", "head_sha", "base_sha")}
            manifest["implementing"] = run["stage"] == "implement"
            manifest["files"] = launch.get("context_files", {})
            checkpoint = stage_evidence.observe_source(run, manifest, path)
            saved = recorded.get("result_checkpoint")
            if saved and saved != checkpoint:
                raise ValueError("stopped source checkpoint changed")
            recorded["result_checkpoint"] = checkpoint
        except (OSError, ValueError, subprocess.SubprocessError):
            recorded["resume_fallback_reason"] = "stopped_source_unavailable"
    return recorded


def _stop_snapshot(record):
    """Ignore heartbeat timestamps, preserving every ownership and result fact."""
    return {key: value for key, value in record.items() if key != "updated_at"} if record is not None else None


def terminate_children(conn, task_id, *, store: StageStore):
    """Cancel all model process groups before allowing another attempt to start."""
    review = store.session(conn, task_id=task_id)
    if not review:
        return "already_dead"
    binding = execution_store.get_binding(conn, "task", task_id)
    outcomes = []
    updates: list[tuple[str, dict[str, Any]]] = []
    finished = {}
    runs = store.runs(conn, review["round_id"])
    for run in runs:
        if run["status"] in {ModelRunStatus.PREPARED, ModelRunStatus.WAITING} and not run["slot_reserved"]:
            updates.append(
                (
                    run["run_id"],
                    {"status": ModelRunStatus.INTERRUPTED.value, "error": "coordinator stopped before launch"},
                )
            )
            continue
        if run["status"] not in {ModelRunStatus.STARTING, ModelRunStatus.RUNNING, ModelRunStatus.FAILED}:
            continue
        recorded = run["dispatch"]
        if _launch_failed_without_process(recorded):
            fields: dict[str, Any] = {"slot_reserved": 0}
            if run["status"] in {ModelRunStatus.STARTING, ModelRunStatus.RUNNING}:
                fields.update(
                    status=ModelRunStatus.FAILED.value, error=recorded.get("safe_error", "review launch failed")
                )
            updates.append((run["run_id"], fields))
            outcomes.append("already_dead")
            continue
        if run["status"] == ModelRunStatus.FAILED and not recorded.get("pid"):
            if run["slot_reserved"]:
                outcomes.append("attention")
            continue
        if _group_finished(recorded):
            finished[run["run_id"]] = recorded
            outcomes.append("already_dead")
            continue
        if recorded.get("process_exited") or (
            recorded and worker_identity.inspect_recorded_identity(recorded) == "dead"
        ):
            exited = _wait_for_group_exit(recorded)
            if exited:
                finished[run["run_id"]] = recorded
            outcomes.append("already_dead" if exited else "attention")
            continue
        if not recorded:
            outcomes.append("attention")
            continue
        result = worker_identity.terminate_recorded_identity(recorded)
        if result["status"] != "attention" and not _wait_for_group_exit(recorded):
            result = {"status": "attention"}
        outcomes.append(result["status"])
        if result["status"] != "attention":
            finished[run["run_id"]] = recorded
    # Observe/stop every child before opening our write transaction. In
    # particular, stopping the second child must not hold the first's update.
    # A canceled command already holds its writer and is terminal. Recovery
    # callers observe stopped writer progress before beginning any writes.
    for run in runs:
        if run["run_id"] not in finished:
            continue
        recorded = _stopped_dispatch(run, observe_source=not conn.in_transaction)
        finished[run["run_id"]] = recorded
        fields = {"dispatch_json": json.dumps(recorded), "slot_reserved": 0}
        if run["status"] in {ModelRunStatus.STARTING, ModelRunStatus.RUNNING}:
            fields.update(status=ModelRunStatus.INTERRUPTED.value, error="coordinator stopped")
        updates.append((run["run_id"], fields))
    if updates:
        if not conn.in_transaction:
            db_transactions.begin(conn, immediate=True)
        if (
            _stop_snapshot(store.session(conn, task_id=task_id)) != _stop_snapshot(review)
            or [_stop_snapshot(run) for run in store.runs(conn, review["round_id"])]
            != [_stop_snapshot(run) for run in runs]
            or _stop_snapshot(execution_store.get_binding(conn, "task", task_id)) != _stop_snapshot(binding)
        ):
            return "attention"
    if finished and (database := db_sqlite.main_database_path(conn)):
        for run_id, recorded in finished.items():
            record_dispatch_usage(conn, database.parent, "stage", run_id, task_id, recorded, terminal=True)
    for run_id, fields in updates:
        store.update_run(conn, run_id, **fields)
    return "attention" if "attention" in outcomes else "terminated"


class StageCoordinator:
    """Run stages using capabilities supplied by the concrete workflow."""

    store: StageStore
    workspace: StageWorkspace
    artifact_files: StageArtifacts
    slot_stores: tuple[tuple[StageStore, str], ...]
    voter_stage: str
    handoff_files: dict[str, tuple[str, ...]]
    artifact_kinds: tuple[str, ...]
    workflow_name: str

    def __init__(self, db_path, task_id, attempt_id, *, launcher=dispatch.dispatch_worker, poll_seconds=1):
        store = self.store
        self.db_path, self.task_id, self.attempt_id = str(db_path), task_id, attempt_id
        self.launcher = launcher
        self.poll_seconds = poll_seconds
        self.stopping = threading.Event()
        self.processes = {}
        self._heartbeat_lock = threading.Lock()
        self._next_heartbeat_at = 0.0
        with store.connect(db_path) as conn:
            self.session = store.session(conn, task_id=task_id)
        if not self.session:
            raise ValueError("no registered review round for task")
        self.round_id = self.session["round_id"]
        self.request = self.session["request"]
        self.data_dir = Path(db_path).resolve().parent
        self.manifest = self.session["manifest"]

    def check_active(self):
        store = self.store
        if self.stopping.is_set():
            raise StageStopped("review coordinator is stopping")
        with store.connect(self.db_path) as conn:
            if not attempt_is_active(conn, self.task_id, self.attempt_id):
                raise StageStopped("review attempt is no longer current")
        # Ownership/cancellation is checked on every poll. The shared parent
        # heartbeat is diagnostic and must not turn a busy writer into a failed
        # model round or make every reviewer contend on the same UPDATE.
        now = time.monotonic()
        if now < self._next_heartbeat_at or not self._heartbeat_lock.acquire(blocking=False):
            return
        try:
            if now < self._next_heartbeat_at:
                return
            try:
                with closing(db_database.connect(self.db_path, timeout=0.1)) as conn, conn:
                    if not attempt_is_active(conn, self.task_id, self.attempt_id):
                        raise StageStopped("review attempt is no longer current")
                    db_write_attempts.set_heartbeat_at_by_attempt_id(conn, store.now(), self.attempt_id)
            except db_errors.OperationalError as exc:
                if not _database_busy(exc):
                    raise
                self._next_heartbeat_at = now + 1
            else:
                self._next_heartbeat_at = now + 30
        finally:
            self._heartbeat_lock.release()

    def phase(self, status):
        store = self.store
        self.check_active()
        with store.connect(self.db_path) as conn:
            store.update(conn, self.round_id, status=status, error=None)
        snapshot.record_snapshot(
            self.db_path,
            self.task_id,
            self.attempt_id,
            "prepare" if status == RoundStatus.PREPARING.value else "analyze",
            "running",
            f"{self.workflow_name.title()} {self.round_id}: {status}",
        )

    def save_manifest(self):
        store = self.store
        with store.connect(self.db_path) as conn:
            store.update(conn, self.round_id, manifest_json=json.dumps(self.manifest, ensure_ascii=False))
            db_write_attempts.set_worktree_path_by_attempt_id(conn, self.manifest["worktree_path"], self.attempt_id)

    def recovery_workers(self):
        return {worker["name"]: worker for worker in self.request["workers"]}

    def reconcile_running(self):
        """Finish previous model processes before scheduling any new phase."""
        store = self.store
        with store.connect(self.db_path) as conn:
            previous = store.runs(conn, self.round_id)
        workers = self.recovery_workers()
        pending = []
        for run in previous:
            if run["status"] == ModelRunStatus.STARTING and not run["dispatch"].get("pid"):
                raise StageStopped("model launch outcome unknown; inspect before retry")
            if run["status"] == ModelRunStatus.RUNNING:
                pending.append(run)

        def reconcile(run):
            try:
                self._wait(run, run["dispatch"], workers[run["worker_id"]]["timeout_seconds"])
            except StageStopped as exc:
                self.checkpoint_stopped_run(run, exc)
                raise
            except RuntimeError as exc:
                with store.connect(self.db_path) as conn:
                    store.update_run(conn, run["run_id"], status=ModelRunStatus.FAILED.value, error=str(exc))

        self.parallel(pending, reconcile)

    def parallel(self, items, operation):
        """Stop sibling waiters on cancellation without losing running children."""
        if not items:
            return []
        with ThreadPoolExecutor(max_workers=len(items)) as executor:
            futures = {}
            try:
                for index, item in enumerate(items):
                    futures[executor.submit(operation, item)] = index
                results = [None] * len(items)
                for future in as_completed(futures):
                    results[futures[future]] = future.result()
            except BaseException:
                self.stopping.set()
                for future in futures:
                    future.cancel()
                raise
        return results

    def release_slot(self, run, launched):
        store = self.store
        # Callers release only after the group is gone or spawn failed without a PID.
        observed = {**launched, "group_exit_confirmed": True} if launched.get("pid") else launched
        with store.connect(self.db_path) as conn:
            record_dispatch_usage(conn, self.data_dir, "stage", run["run_id"], self.task_id, observed, terminal=True)
            store.release_slot(conn, run["run_id"])

    def reclaim_finished_slots(self):
        # A shared pool must reclaim proven-dead groups from either workflow;
        # unknown launches retain their reservation until explicit intervention.
        reserved = []
        with self.store.connect(self.db_path) as conn:
            for run_store, table in self.slot_stores:
                if db_read_sqlite_master.object_exists(conn, table):
                    reserved.extend((run_store, run) for run in run_store.reserved_runs(conn))
        for run_store, run in reserved:
            if _launch_failed_without_process(run["dispatch"]) or (
                run["dispatch"] and _group_finished(run["dispatch"])
            ):
                with self.store.connect(self.db_path) as conn:
                    run_store.release_finished_slot(conn, run)

    def stage_worktree(self, run):
        if resume := (run.get("dispatch") or {}).get("native_resume"):
            return resume["cwd"]
        return self.manifest["worktree_path"]

    def stage_environment(self, run):
        return model_environment.stage_environment(
            self.manifest, run["output_dir"], self.data_dir, workflow_name=self.workflow_name
        )

    def authorize_launch(self, run):
        """Allow a workflow to recheck authority after waiting for a model slot."""

    def launch_with_slot(self, run, worker, prompt_path, environment):
        store = self.store
        while True:
            self.check_active()
            self.reclaim_finished_slots()
            # Cancellation takes this same lock before changing the owner or
            # terminating children. Keep reservation/spawn/PID registration
            # together, but never retain a SQLite writer across external I/O.
            with task_publication_lock(self.db_path, self.task_id) as locked:
                if locked:
                    self.check_active()
                    worktree = self.stage_worktree(run)
                    if parent_id := (run.get("dispatch") or {}).get("parent_run_id"):
                        with store.connect(self.db_path) as conn:
                            parent = next(
                                (r for r in store.runs(conn, self.round_id) if r["run_id"] == parent_id), None
                            )
                        if not parent or not _group_finished(parent["dispatch"]):
                            raise StageStopped("native resume source process changed before launch")
                        pinned = parent["dispatch"].get("result_checkpoint") or parent["dispatch"].get(
                            "launch_checkpoint"
                        )
                        if not pinned or pinned.get("source_pin_error"):
                            raise StageStopped("native resume source checkpoint incomplete before launch")
                        if pinned != self.source_checkpoint(parent):
                            raise StageStopped("native resume source changed before launch")
                    checkpoint = self.source_checkpoint(run)
                    resume = (run.get("dispatch") or {}).get("native_resume")
                    if resume and checkpoint.get("source_pin_error"):
                        raise StageStopped("native resume launch source checkpoint incomplete")
                    native_home = resume["native_home"] if resume else None
                    if not resume and supports_native_resume(worker["agent"]):
                        try:
                            native_home = native_home_from_environment(worker["agent"], environment)
                        except (OSError, ValueError):
                            pass  # A missing home does not prevent a fresh CLI from initializing it.
                    with store.connect(self.db_path) as conn:
                        db_transactions.begin(conn, immediate=True)
                        if not attempt_is_active(conn, self.task_id, self.attempt_id):
                            raise StageStopped("review attempt is no longer current")
                        acquired = store.reserve_slot(conn, run["run_id"])
                        if acquired:
                            recorded = dict(run["dispatch"] or {})
                            recorded["launch_checkpoint"] = checkpoint
                            recorded.update(
                                worker_agent=worker["agent"],
                                worker_name=worker["name"],
                                model=run["model"],
                                logical_execution_id=f"{self.round_id}:{run['stage']}:{worker['name']}:{run['prompt_sha256']}",
                            )
                            if native_home:
                                recorded["native_home"] = native_home
                            store.update_run(conn, run["run_id"], dispatch_json=json.dumps(recorded))
                            start_call(
                                conn,
                                self.data_dir,
                                "stage",
                                run["run_id"],
                                self.task_id,
                                recorded,
                                stage=run["stage"],
                                confirmed=False,
                            )
                    if acquired:
                        resume = recorded.get("native_resume")
                        try:
                            self.authorize_launch(run)
                        except StageStopped as exc:
                            launched = {"ok": False, "status": "failed_dispatch", "safe_error": str(exc)}
                        else:
                            launched = self.launcher(
                                task_id=self.task_id,
                                attempt_id=run["run_id"],
                                prompt_path=prompt_path,
                                worktree_path=worktree,
                                worker_agent_config=worker,
                                model=run["model"],
                                reasoning_effort=worker["default_effort"],
                                trusted_environment=environment,
                                log_dir=run["output_dir"],
                                dry_run=False,
                                **({"resume": resume} if resume else {}),
                            )
                        launched = {
                            **recorded,
                            **launched,
                            "prompt_path": str(prompt_path),
                            "worktree_path": str(worktree),
                        }
                        self.bind_native_session(run, launched, worker, prompt_path)
                        # Retain our Popen even when another thread's dispatch
                        # reaps the shared registry before this waiter polls it.
                        process = dispatch.tracked_process(launched.get("pid"))
                        if process is not None:
                            self.processes[launched["pid"]] = process
                        with store.connect(self.db_path) as conn:
                            if launched.get("pid") or launched.get("process_exited"):
                                start_call(
                                    conn,
                                    self.data_dir,
                                    "stage",
                                    run["run_id"],
                                    self.task_id,
                                    launched,
                                    stage=run["stage"],
                                )
                            store.update_run(
                                conn,
                                run["run_id"],
                                status=ModelRunStatus.RUNNING.value
                                if launched.get("ok") or launched.get("pid")
                                else ModelRunStatus.FAILED.value,
                                dispatch_json=json.dumps(launched),
                            )
                            fresh = next(r for r in store.runs(conn, self.round_id) if r["run_id"] == run["run_id"])
                        run.update(fresh)
                        return launched
            self.stopping.wait(self.poll_seconds)

    def register(self, run, artifact):
        store = self.store
        self.check_active()
        with store.connect(self.db_path) as conn:
            db_transactions.begin(conn, immediate=True)
            if not attempt_is_active(conn, self.task_id, self.attempt_id):
                raise StageStopped("review attempt is no longer current")
            fields = {"artifact_json": json.dumps(artifact)}
            if run["status"] in {ModelRunStatus.FAILED, ModelRunStatus.INTERRUPTED}:
                recorded = dict(run["dispatch"])
                receipt = recorded.get("artifact_adoption") or {
                    "run_id": run["run_id"],
                    "prompt_sha256": run["prompt_sha256"],
                    "original_status": run["status"],
                    "original_error": run["error"],
                    "artifact_sha256": hashlib.sha256(json.dumps(artifact, sort_keys=True).encode()).hexdigest(),
                    "registered_at": store.now(),
                    "result_checkpoint": recorded.get("result_checkpoint"),
                }
                recorded["artifact_adoption"] = receipt
                fields["dispatch_json"] = json.dumps(recorded)
            else:
                fields.update(status=ModelRunStatus.SUCCEEDED.value, error=None)
            store.update_run(conn, run["run_id"], **fields)
            record_call_event(
                conn,
                "stage",
                run["run_id"],
                self.task_id,
                "artifact_adopted" if "dispatch_json" in fields else "registered",
                stage=run["stage"],
            )
            for kind in self.artifact_kinds:
                path = artifact.get(kind + "_path")
                if not path:
                    continue
                db_write_artifacts.insert_for_register(
                    conn,
                    f"{run['run_id']}:{kind}",
                    self.task_id,
                    self.attempt_id,
                    f"{self.workflow_name}_{kind}",
                    path,
                    artifact[kind + "_sha256"],
                    Path(path).stat().st_size,
                    store.now(),
                )
        return {**artifact, "run_id": run["run_id"], "worker_id": run["worker_id"]}

    def source_checkpoint(self, run):
        """Observe the immutable stage inputs and the writer's resulting source."""
        recorded = run.get("dispatch") or {}
        saved = recorded.get("result_checkpoint") or recorded.get("launch_checkpoint") or {}
        path = recorded.get("worktree_path") or saved.get("worktree_path") or self.stage_worktree(run)
        if saved.get("worktree_path") and str(path) != saved["worktree_path"]:
            raise StageStopped("registered stage source worktree changed")
        try:
            return stage_evidence.observe_source(run, self.manifest, path)
        except stage_evidence.SourcePinUnavailable as exc:
            # Ordinary execution still validates the frozen snapshot and output.
            # This provenance cannot prove source compatibility for recovery.
            return exc.checkpoint
        except ValueError as exc:
            raise StageStopped(str(exc)) from exc

    def checkpoint_result(self, run, *, failure_kind=None):
        """Persist completed source evidence before validating or registering files."""
        checkpoint = self.source_checkpoint(run)
        self.workspace.verify_snapshot(self.manifest)
        with self.store.connect(self.db_path) as conn:
            current = next(r for r in self.store.runs(conn, self.round_id) if r["run_id"] == run["run_id"])
            recorded = dict(current["dispatch"])
            saved = recorded.get("result_checkpoint")
            if saved and saved.get("source_pin_error"):
                # A later complete observation cannot retrospectively prove
                # the inputs of the execution whose first result pin failed.
                identity = {key: value for key, value in saved.items() if key != "source_pin_error"}
                if any(checkpoint.get(key) != value for key, value in identity.items()):
                    raise StageStopped("completed model source checkpoint changed")
                checkpoint = saved
            if saved and saved != checkpoint:
                raise StageStopped("completed model source checkpoint changed")
            recorded["result_checkpoint"] = checkpoint
            if failure_kind:
                recorded["failure_kind"] = failure_kind
            self.store.update_run(conn, run["run_id"], dispatch_json=json.dumps(recorded))
            record_call_event(conn, "stage", run["run_id"], self.task_id, "result_saved", stage=run["stage"])
        run["dispatch"] = recorded

    def checkpoint_stopped_run(self, run, error):
        """Account for a stopped stage only after exit and unchanged ownership."""
        with self.store.connect(self.db_path) as conn:
            session = self.store.session(conn, task_id=self.task_id)
            original = next((r for r in self.store.runs(conn, self.round_id) if r["run_id"] == run["run_id"]), None)
            binding = execution_store.get_binding(conn, "task", self.task_id)
            active = attempt_is_active(conn, self.task_id, self.attempt_id)
        if not active or not original or original["status"] not in {ModelRunStatus.STARTING, ModelRunStatus.RUNNING}:
            return
        if not _group_finished(original["dispatch"]):
            return
        recorded = _stopped_dispatch(original, observe_source=True)
        with self.store.connect(self.db_path) as conn:
            db_transactions.begin(conn, immediate=True)
            current = next((r for r in self.store.runs(conn, self.round_id) if r["run_id"] == run["run_id"]), None)
            if (
                _stop_snapshot(current) != _stop_snapshot(original)
                or _stop_snapshot(self.store.session(conn, task_id=self.task_id)) != _stop_snapshot(session)
                or _stop_snapshot(execution_store.get_binding(conn, "task", self.task_id)) != _stop_snapshot(binding)
                or not attempt_is_active(conn, self.task_id, self.attempt_id)
            ):
                return
            self.store.update_run(
                conn,
                run["run_id"],
                status=ModelRunStatus.INTERRUPTED.value,
                error=str(error),
                dispatch_json=json.dumps(recorded),
                slot_reserved=0,
            )
            record_dispatch_usage(conn, self.data_dir, "stage", run["run_id"], self.task_id, recorded, terminal=True)
            if recorded.get("result_checkpoint"):
                record_call_event(conn, "stage", run["run_id"], self.task_id, "result_saved", stage=run["stage"])
        run.update(status=ModelRunStatus.INTERRUPTED.value, error=str(error), dispatch=recorded, slot_reserved=0)

    def bind_native_session(self, run, recorded, worker, prompt_path):
        """Bind native provider history to the registered frozen launch."""
        saved = recorded.get("native_session")
        if saved and saved.get("history_path") or not supports_native_resume(worker["agent"]):
            return
        binding = session_resume.resolve_native_session(
            data_root=self.data_dir,
            agent=worker["agent"],
            task_id=self.task_id,
            run_id=run["run_id"],
            prompt_path=prompt_path,
            prompt_sha256=run["prompt_sha256"],
            cwd=recorded.get("worktree_path") or recorded.get("launch_checkpoint", {}).get("worktree_path"),
            stdout_path=recorded.get("stdout_path"),
            started_at=run.get("started_at") or run["created_at"],
            native_home=recorded.get("native_home"),
        )
        if binding:
            if saved and saved["session_id"] != binding["session_id"]:
                raise StageStopped("native session identity changed")
            recorded["native_session"] = binding
            recorded["native_session_id"] = binding["session_id"]

    def missing_output(self, run):
        parent = "runs" if run["stage"] == self.voter_stage else run["stage"]
        marker = Path(self.manifest["artifact_root"]) / parent / "sealed" / run["run_id"] / "seal.json"
        if marker.exists():
            return False
        output = self.artifact_files.safe_path(run["output_dir"], self.manifest["artifact_root"])
        return any(not (output / name).is_file() for name in self.handoff_files[run["stage"]])

    def native_resume_source(self, stage, worker, prompt_path, prompt_hash, previous):
        """Choose only a stopped, unchanged, incomplete invocation for resume."""
        if not supports_native_resume(worker["agent"]):
            return None, "adapter_unsupported"
        matching = [r for r in previous if r["prompt_sha256"] == prompt_hash]
        if not matching:
            return None, None
        run = matching[-1]
        recorded = run["dispatch"]
        if run["status"] not in {ModelRunStatus.FAILED, ModelRunStatus.INTERRUPTED} or not self.missing_output(run):
            return None, None
        if (run.get("error") or "").startswith("rate_limited: "):
            return None, "rate_limited_session"
        if not recorded.get("pid"):
            if (
                run.get("slot_reserved")
                or recorded.get("launch_checkpoint")
                and recorded.get("status") != "failed_dispatch"
            ):
                raise StageStopped("native resume launch outcome unknown")
            return None, "no_native_launch"
        if recorded.get("cleanup_pending") or not _group_finished(recorded):
            raise StageStopped("native resume requires the original process group to exit")
        checkpoint = recorded.get("result_checkpoint") or recorded.get("launch_checkpoint")
        if not checkpoint:
            return None, "source_checkpoint_unavailable"
        current = self.source_checkpoint(run)
        if checkpoint.get("source_pin_error"):
            identity = {key: value for key, value in checkpoint.items() if key != "source_pin_error"}
            if any(current.get(key) != value for key, value in identity.items()):
                raise StageStopped("native resume source checkpoint changed")
            return None, "source_checkpoint_incomplete"
        if checkpoint != current:
            raise StageStopped("native resume source checkpoint changed")
        self.workspace.verify_snapshot(self.manifest)
        self.bind_native_session(run, recorded, worker, prompt_path)
        binding = recorded.get("native_session")
        if not binding or not binding.get("history_path"):
            return None, "native_history_unavailable"
        if sum(bool(r["dispatch"].get("native_resume")) for r in matching) >= 3:
            raise StageStopped("native session resume budget exhausted")
        return (run, binding), None

    def adoptable_result(self, run):
        recorded = run["dispatch"]
        if not recorded or not _group_finished(recorded):
            if recorded and (recorded.get("pid") or run.get("slot_reserved")):
                raise StageStopped("completed artifact process identity requires attention")
            return False
        checkpoint = recorded.get("result_checkpoint")
        classification = recorded.get("failure_kind")
        accepted = artifact_available(run) or classification in {"registration", "execution_complete"}
        accepted |= classification == "interrupted" or run["status"] == ModelRunStatus.INTERRUPTED
        if not checkpoint or not accepted:
            return False
        if self.missing_output(run):
            return False
        if checkpoint.get("source_pin_error"):
            raise StageStopped("completed artifact source checkpoint incomplete; adoption requires exact evidence")
        if checkpoint != self.source_checkpoint(run):
            raise StageStopped("completed artifact source checkpoint changed")
        self.workspace.verify_snapshot(self.manifest)
        return True

    def sealed_output(self, run):
        """Copy handoff files to a separate registration directory and hash them.

        Later writes to the original outputs do not change these copies. This
        is an application-level snapshot, not an OS access restriction on the
        CLI. The marker also recovers a seal committed before SQLite.
        """
        store = self.store
        names = self.handoff_files[run["stage"]]
        parent = "runs" if run["stage"] == self.voter_stage else run["stage"]
        sealed = Path(self.manifest["artifact_root"]) / parent / "sealed" / run["run_id"]
        self.artifact_files.safe_path(sealed, self.manifest["artifact_root"])
        sealed.mkdir(parents=True, exist_ok=True)
        marker = sealed / "seal.json"
        identity = {"run_id": run["run_id"], "prompt_sha256": run["prompt_sha256"]}
        if not marker.exists():
            hashes = {}
            output = self.artifact_files.safe_path(run["output_dir"], self.manifest["artifact_root"])
            directory_fd = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                for name in names:
                    # Anchor the open directory too: replacing the producer
                    # directory with a symlink cannot redirect this read.
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
                    with os.fdopen(fd, "rb") as stream:
                        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                            raise ValueError("review handoff must be a regular file")
                        raw = stream.read(16 * 1024 * 1024 + 1)
                    if len(raw) > 16 * 1024 * 1024:
                        raise ValueError("review handoff exceeds 16 MiB")
                    temporary = sealed / (name + ".tmp")
                    temporary.write_bytes(raw)
                    temporary.replace(sealed / name)
                    hashes[name] = hashlib.sha256(raw).hexdigest()
            finally:
                os.close(directory_fd)
            self.artifact_files.atomic_json(marker, {**identity, "files": hashes})
        saved = json.loads(marker.read_text(encoding="utf-8"))
        if any(saved.get(key) != value for key, value in identity.items()) or set(saved.get("files", {})) != set(names):
            raise ValueError("review seal identity changed")
        for name, expected in saved["files"].items():
            path = self.artifact_files.safe_path(sealed / name, self.manifest["artifact_root"])
            if store.digest(path) != expected:
                raise ValueError("sealed review artifact changed")
        return sealed

    def _wait(self, run, launched, timeout):
        store = self.store
        with store.connect(self.db_path) as conn:
            current = next(item for item in store.runs(conn, self.round_id) if item["run_id"] == run["run_id"])
        started = datetime.fromisoformat(current.get("started_at") or current["created_at"])
        workers = self.recovery_workers()
        worker = workers[run["worker_id"]]
        saved_dispatch = json.dumps(current["dispatch"], sort_keys=True)
        if launched.get("prompt_path"):
            self.bind_native_session(run, launched, worker, Path(launched["prompt_path"]))
        next_binding_at = 0.0
        while not launched.get("process_exited"):
            self.check_active()
            if launched.get("prompt_path") and time.monotonic() >= next_binding_at:
                self.bind_native_session(run, launched, worker, Path(launched["prompt_path"]))
                payload = json.dumps(launched, sort_keys=True)
                if payload != saved_dispatch:
                    # Session discovery is recoverable from provider history.
                    # Retry only this periodic checkpoint on a busy writer;
                    # completion/source/slot checkpoints below remain strict.
                    try:
                        with closing(db_database.connect(self.db_path, timeout=0.1, foreign_keys=True)) as conn, conn:
                            db_transactions.begin(conn, immediate=True)
                            if not attempt_is_active(conn, self.task_id, self.attempt_id):
                                raise StageStopped("review attempt is no longer current")
                            store.update_run(conn, run["run_id"], dispatch_json=payload)
                    except db_errors.OperationalError as exc:
                        if not _database_busy(exc):
                            raise
                    else:
                        saved_dispatch = payload
                next_binding_at = time.monotonic() + 1
            proc = self.processes.get(launched.get("pid")) or dispatch.tracked_process(launched.get("pid"))
            if proc is not None:
                code = proc.poll()
                if code is not None:
                    dispatch.reap_finished_process(proc.pid)
                    self.processes.pop(proc.pid, None)
                    launched = {**launched, "process_exited": True, "returncode": code}
                    break
            else:
                identity = worker_identity.inspect_recorded_identity(launched)
                if identity == "dead":
                    # The coordinator restarted after the model finished. Its
                    # complete expected file is the durable completion evidence.
                    launched = {**launched, "process_exited": True}
                    break
                if identity != "alive_same":
                    raise StageStopped("review worker identity requires attention")
            if (datetime.now(timezone.utc) - started).total_seconds() > timeout:
                termination = worker_identity.terminate_recorded_identity(launched)
                if termination["status"] == "attention":
                    raise StageStopped("review worker timeout termination requires attention")
                if not _wait_for_group_exit(launched):
                    raise StageStopped("review worker timeout left a process group; inspect remaining processes")
                launched = {**launched, "process_exited": True, "interrupted": True}
                with store.connect(self.db_path) as conn:
                    store.update_run(conn, run["run_id"], dispatch_json=json.dumps(launched))
                run["dispatch"] = launched
                self.checkpoint_result(run, failure_kind="interrupted")
                self.release_slot(run, launched)
                raise RuntimeError("review worker timed out")
            time.sleep(self.poll_seconds)
        if launched.get("prompt_path"):
            self.bind_native_session(run, launched, worker, Path(launched["prompt_path"]))
        with store.connect(self.db_path) as conn:
            store.update_run(conn, run["run_id"], dispatch_json=json.dumps(launched))
        if not _wait_for_group_exit(launched):
            raise StageStopped(
                "review leader exited but process group remains; inspect and stop remaining processes before retry"
            )
        self.release_slot(run, launched)
        run["dispatch"] = launched
        # Final verification inputs are reconstructed after reconcile_running.
        # A modern launch pins its cwd before spawn; historical runs wait until
        # the owning phase reconstructs those inputs before recording evidence.
        if run["stage"] != "verify_final" or launched.get("launch_checkpoint"):
            self.checkpoint_result(run, failure_kind="execution_complete")
        _raise_rate_limit(launched)
        if launched.get("returncode", 0) != 0:
            raise RuntimeError(f"review worker exited with code {launched['returncode']}")

    def phase_input_changed(self, stage, run):
        return stage == "synthesis" or (stage == "prepare" and not self.manifest.get("frozen"))

    def preserve_phase_artifact(self, run):
        """Workflow hook for immutable successes referenced by later stage evidence."""
        return False

    def check_launch_budget(self, stage, prompt_hash, previous):
        """Workflow hook for a durable limit on stage launches."""

    def validate_output(self, run, validate, model):
        output = self.sealed_output(run)
        if artifact_available(run):
            return validate(output, model)
        ready_at = self.store.now()
        validated_at = None
        try:
            artifact = validate(output, model)
            validated_at = self.store.now()
            return artifact
        finally:
            with self.store.connect(self.db_path) as conn:
                db_transactions.begin(conn, immediate=True)
                if attempt_is_active(conn, self.task_id, self.attempt_id):
                    facts = {"source": "controller", "precision": "observed"}
                    for event, at in (("artifact_ready", ready_at), ("artifact_validated", validated_at)):
                        if at:
                            record_call_event(
                                conn,
                                "stage",
                                run["run_id"],
                                self.task_id,
                                event,
                                at=at,
                                stage=run["stage"],
                                metadata=facts,
                            )

    def run_phase(self, stage, worker, prompt_path, validate, *, input_hash=None):
        """Adopt a complete unregistered file, or run only this failed stage."""
        store = self.store
        self.workspace.verify_snapshot(self.manifest)
        prompt_hash = store.digest(prompt_path)
        if stage == self.voter_stage and prompt_hash != self.manifest["prompt_sha256"]:
            raise ValueError("review prompt hash mismatch")
        with store.connect(self.db_path) as conn:
            prior = [
                r for r in store.runs(conn, self.round_id) if r["stage"] == stage and r["worker_id"] == worker["name"]
            ]
        for run in reversed(prior):
            if run["status"] in {ModelRunStatus.PREPARED, ModelRunStatus.WAITING} and not run["slot_reserved"]:
                with store.connect(self.db_path) as conn:
                    store.update_run(
                        conn, run["run_id"], status=ModelRunStatus.INTERRUPTED.value, error="recovered before launch"
                    )
                continue
            if run["prompt_sha256"] != prompt_hash:
                if self.phase_input_changed(stage, run):
                    if run["status"] in {
                        ModelRunStatus.RUNNING,
                        ModelRunStatus.SUCCEEDED,
                    } and not self.preserve_phase_artifact(run):
                        with store.connect(self.db_path) as conn:
                            store.update_run(
                                conn,
                                run["run_id"],
                                status=ModelRunStatus.SUPERSEDED.value,
                                error="synthesis input set changed",
                            )
                    continue
                raise ValueError("same-round prompt changed")
            if run["status"] == ModelRunStatus.STARTING and not run["dispatch"].get("pid"):
                raise StageStopped("model launch outcome unknown; cancel and inspect this round")
            adopting = run["status"] in {ModelRunStatus.FAILED, ModelRunStatus.INTERRUPTED}
            if adopting and not self.adoptable_result(run):
                continue
            if not adopting and run["status"] not in {ModelRunStatus.SUCCEEDED, ModelRunStatus.RUNNING}:
                continue
            try:
                if run["status"] == ModelRunStatus.RUNNING:
                    self._wait(run, run["dispatch"], worker["timeout_seconds"])
                if self.store.digest(prompt_path) != prompt_hash:
                    raise StageStopped("registered phase prompt changed")
                if run["status"] == ModelRunStatus.RUNNING:
                    self.checkpoint_result(run)
                model = run["model"] or worker["default_model"]
                artifact = self.validate_output(run, validate, model)
                artifact["model"] = model
                if input_hash:
                    artifact["input_sha256"] = input_hash
                if artifact_available(run) and artifact != run["artifact"]:
                    raise ValueError("registered review artifact changed")
                self.workspace.verify_snapshot(self.manifest)
                return self.register(run, artifact)
            except StageStopped as exc:
                self.checkpoint_stopped_run(run, exc)
                raise
            except (ValueError, OSError, RuntimeError) as exc:
                if adopting:
                    raise StageStopped("completed artifact failed independent validation: " + str(exc)) from exc
                with store.connect(self.db_path) as conn:
                    store.update_run(conn, run["run_id"], status=ModelRunStatus.FAILED.value, error=str(exc))
                break
        self.check_active()
        with store.connect(self.db_path) as conn:
            prior_runs = [
                r for r in store.runs(conn, self.round_id) if r["stage"] == stage and r["worker_id"] == worker["name"]
            ]
        resume_source, fallback_reason = self.native_resume_source(stage, worker, prompt_path, prompt_hash, prior_runs)
        with store.connect(self.db_path) as conn:
            previous = [
                r for r in store.runs(conn, self.round_id) if r["stage"] == stage and r["worker_id"] == worker["name"]
            ]
            self.check_launch_budget(stage, prompt_hash, previous)
            model = worker["default_model"]
            limited = [
                r
                for r in previous
                if r["prompt_sha256"] == prompt_hash
                and r["status"] == ModelRunStatus.FAILED
                and (r["error"] or "").startswith("rate_limited: ")
            ]
            if limited:
                fallback = worker.get("fallback_model")
                if not fallback or any(r["model"] == fallback for r in limited):
                    raise ModelRateLimited(limited[-1]["error"])
                model = fallback
            elif (
                worker.get("fallback_model")
                and len(previous) >= 2
                and all(
                    r["status"] == ModelRunStatus.FAILED and r["error"] == "review worker timed out"
                    for r in previous[-2:]
                )
            ):
                model = worker["fallback_model"]
            run = store.new_run(
                conn,
                self.round_id,
                stage,
                worker["name"],
                self.attempt_id,
                self.manifest["artifact_root"],
                prompt_hash,
                model=model,
            )
            store.update_run(conn, run["run_id"], status=ModelRunStatus.WAITING.value)
            record_call_event(conn, "stage", run["run_id"], self.task_id, "queued", stage=stage)
        recorded = {}
        if resume_source:
            source_run, binding = resume_source
            segment = 1 + sum(
                bool(r["dispatch"].get("native_resume")) for r in previous if r["prompt_sha256"] == prompt_hash
            )
            try:
                descriptor = session_resume.resume_descriptor(
                    binding,
                    task_id=binding["task_id"],
                    run_id=run["run_id"],
                    prompt_path=prompt_path,
                    prompt_sha256=prompt_hash,
                    cwd=binding["cwd"],
                    segment=segment,
                    native_home=source_run["dispatch"].get("native_home"),
                )
                recorded = {
                    "native_resume": descriptor,
                    "native_session": binding,
                    "native_session_id": binding["session_id"],
                    "parent_run_id": source_run["run_id"],
                    "usage_segment_id": run["run_id"],
                    "native_home": descriptor["native_home"],
                }
                if cursor := model_usage.native_resume_usage_cursor(binding):
                    recorded["native_usage_cursor"] = cursor
                raw_dispatch = {key: value for key, value in source_run["dispatch"].items() if key != "usage_baseline"}
                original_usage = model_usage.read_dispatch(self.data_dir, raw_dispatch)
                if (
                    original_usage.get("counter_scope") == "session"
                    and original_usage.get("counter_mode") == "cumulative"
                ):
                    recorded["usage_baseline"] = {
                        key: value
                        for key, value in original_usage.items()
                        if key in {"usage", "provider_usage", "provider_raw_usage", "total_cost_usd"}
                    }
            except (OSError, ValueError):
                fallback_reason = "native_history_invalid"
        if fallback_reason and not recorded:
            recorded = {"resume_fallback_reason": fallback_reason}
        if recorded:
            with store.connect(self.db_path) as conn:
                db_transactions.begin(conn, immediate=True)
                if not attempt_is_active(conn, self.task_id, self.attempt_id):
                    raise StageStopped("review owner changed while preparing native resume")
                store.update_run(conn, run["run_id"], dispatch_json=json.dumps(recorded))
            run["dispatch"] = recorded
        failure_kind = "execution"
        try:
            environment = self.stage_environment(run)
            launched = self.launch_with_slot(run, worker, prompt_path, environment)
            if not launched.get("ok"):
                if launched.get("pid") and not _wait_for_group_exit(launched):
                    raise StageStopped("failed model launch left a process group; inspect remaining processes")
                if launched.get("pid") and launched.get("process_exited"):
                    self.checkpoint_result(run, failure_kind="execution_complete")
                self.release_slot(run, launched)
                _raise_rate_limit(launched)
                raise RuntimeError(launched.get("safe_error", "review launch failed"))
            self._wait(run, launched, worker["timeout_seconds"])
            failure_kind = "output_validation"
            if store.digest(prompt_path) != prompt_hash:
                raise ValueError("model changed prompt file")
            self.workspace.verify_snapshot(self.manifest)
            self.checkpoint_result(run)
            artifact = self.validate_output(run, validate, model)
            artifact["model"] = model
            if input_hash:
                artifact["input_sha256"] = input_hash
            failure_kind = "registration"
            return self.register(run, artifact)
        except StageStopped as exc:
            self.checkpoint_stopped_run(run, exc)
            raise
        except Exception as exc:
            with store.connect(self.db_path) as conn:
                current = next(r for r in store.runs(conn, self.round_id) if r["run_id"] == run["run_id"])
                if current["status"] == ModelRunStatus.STARTING.value and not current["dispatch"].get("pid"):
                    raise StageStopped("model launch outcome unknown; inspect before retry") from exc
                recorded = dict(current["dispatch"])
                if failure_kind != "execution" or recorded.get("failure_kind") != "execution_complete":
                    recorded["failure_kind"] = failure_kind
                store.update_run(
                    conn,
                    run["run_id"],
                    status=ModelRunStatus.FAILED.value,
                    error=str(exc),
                    dispatch_json=json.dumps(recorded),
                )
            if isinstance(exc, ModelRateLimited) and worker.get("fallback_model") and model != worker["fallback_model"]:
                return self.run_phase(stage, worker, prompt_path, validate, input_hash=input_hash)
            raise

    def prompt(self, name, text, *, stage=None):
        path = Path(self.manifest["artifact_root"]) / name
        if not path.exists():
            path.write_text(text, encoding="utf-8")
            return path
        existing = path.read_bytes()
        if existing == text.encode("utf-8"):
            return path
        if stage is not None:
            # A deployment may change the renderer after a phase has run.
            # Only the exact path and bytes recorded by that phase can be reused.
            with self.store.connect(self.db_path) as conn:
                registered = {
                    run["prompt_sha256"]
                    for run in self.store.runs(conn, self.round_id)
                    if run["stage"] == stage and (run["dispatch"] or {}).get("prompt_path") == str(path)
                }
            if registered == {hashlib.sha256(existing).hexdigest()}:
                return path
        raise ValueError("registered phase prompt changed")
