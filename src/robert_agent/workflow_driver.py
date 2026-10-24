"""Production v2 controllers use the same audited domain operations as legacy.

DBOS owns the polling/replay loop. SQLite owns attempts, actions and leases.
Only identifiers and bounded outcomes enter DBOS step history.
"""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
from uuid import uuid4

from robert_agent import workflow_store, workflow_ownership
from robert_agent import run_once, publish, work_items

_driver = None


def configure_driver(config):
    global _driver
    if not config.get("db_path") or not config.get("repos"):
        raise ValueError(
            "active workflow requires domain database and repository configuration"
        )
    if _driver is not None and _driver.db_path != Path(config["db_path"]).resolve():
        raise ValueError("workflow driver already configured for a different database")
    if _driver is None:
        with closing(sqlite3.connect(config["db_path"])) as conn:
            if conn.execute(
                "SELECT 1 FROM workflow_bindings WHERE owner_engine='dbos' AND binding_status='active' AND definition_version!='v2' LIMIT 1"
            ).fetchone():
                raise ValueError(
                    "Active v1 workflows must be drained with their original driver before enabling production v2"
                )
        _driver = ProductionDriver(config)
    return _driver


def reset_driver():
    global _driver
    _driver = None


def current_driver():
    if _driver is None:
        raise RuntimeError("Production workflow driver is not configured")
    return _driver


class ProductionDriver:
    def __init__(self, config, *, publish_runner=subprocess.run):
        self.config = config
        self.db_path = Path(config["db_path"]).resolve()
        self.data_dir = Path(config["data_dir"])
        self.repos = {}
        with closing(sqlite3.connect(self.db_path)) as conn:
            for repo in config["repos"]:
                row = conn.execute(
                    "SELECT repo_id FROM repos WHERE full_name=?", (repo["full_name"],)
                ).fetchone()
                self.repos[row[0] if row else run_once._repo_id(repo["full_name"])] = (
                    run_once._with_python_bin(repo, config)
                )
        self.publish_runner = publish_runner

    def _task(self, conn, subject_type, subject_id):
        if subject_type == "task":
            return conn.execute(
                "SELECT task_id,workstream_id,lifecycle FROM tasks WHERE task_id=?",
                (subject_id,),
            ).fetchone()
        return conn.execute(
            "SELECT task_id,workstream_id,lifecycle FROM tasks WHERE json_extract(metadata_json,'$.remediation_episode_id')=? ORDER BY created_at DESC, task_id DESC LIMIT 1",
            (subject_id,),
        ).fetchone()

    def attention(self, subject_type, subject_id, reason):
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            binding = workflow_store.get_binding(conn, subject_type, subject_id)
            if binding and binding["binding_status"] == "active":
                workflow_store.mark_binding_attention(
                    conn, subject_type, subject_id, run_once._now()
                )
                metadata = json.loads(binding["metadata_json"] or "{}")
                metadata["reason_code"] = reason
                conn.execute(
                    "UPDATE workflow_bindings SET metadata_json=? WHERE subject_type=? AND subject_id=?",
                    (json.dumps(metadata), subject_type, subject_id),
                )
                task = self._task(conn, subject_type, subject_id)
                run_once._insert_notification(
                    conn,
                    "workflow_attention_required",
                    "recorded",
                    run_once._now(),
                    {
                        "task_id": task[0] if task else None,
                        "reason_code": reason,
                        "required_action": "Inspect the stopped workflow, cancel if necessary, then retry.",
                    },
                )
        return {"status": "attention", "reason_code": reason}

    def _consume_events(self, conn, binding, task, repo, now):
        from robert_agent import workflow_signals, remediation

        rows = conn.execute(
            "SELECT * FROM workflow_outbox WHERE workflow_id=? AND NOT EXISTS (SELECT 1 FROM workflow_signal_receipts r WHERE r.signal_id=workflow_outbox.signal_id) ORDER BY created_at,signal_id",
            (binding["workflow_id"],),
        )
        names = [column[0] for column in rows.description]
        signals = [dict(zip(names, row)) for row in rows.fetchall()]
        events = []
        for signal in signals:
            message = workflow_signals._message_for_signal(conn, signal)
            event_row = conn.execute(
                "SELECT payload_json FROM github_events WHERE event_fingerprint=?",
                (message["fact_id"],),
            ).fetchone()
            if event_row:
                event = json.loads(event_row[0])
                if event.get("workstream_id") != task[1]:
                    raise ValueError("signal_workstream_mismatch")
                events.append((signal, event))

        def trusted(event):
            return run_once._is_driving_context_event(
                event
            ) and run_once._is_trusted_actor(event, repo)

        trigger = None
        if task[2] == "waiting_for_user":
            trigger = next((event for _, event in events if trusted(event)), None)
        elif binding["subject_type"] == "remediation_episode" and task[2] in {
            "completed",
            "failed",
        }:
            episode = remediation._load_episode(conn, binding["subject_id"])
            policy = remediation._policy_for_episode(repo, episode["episode_kind"])
            budget = remediation.budget_status(
                conn, episode=episode, policy=policy, now=now
            )
            if episode["status"] == "open" and policy["enabled"] and budget["ok"]:
                trigger = next(
                    (
                        event
                        for signal, event in events
                        if signal["topic"] == "remediation_decision"
                        and signal["dedupe_key"]
                        == f"{event['event_fingerprint']}:{episode['attempt_count']}"
                        and event.get("authorization_status")
                        == "authorized_system_trigger"
                        and (
                            (event.get("metadata") or {}).get("remediation") or {}
                        ).get("episode_id")
                        == binding["subject_id"]
                    ),
                    None,
                )
            elif episode["status"] == "open" and not budget["ok"]:
                remediation.exhaust_episode(
                    conn,
                    episode_id=episode["episode_id"],
                    reason=budget["reason"],
                    now=now,
                )
        if trigger:
            if trusted(trigger):
                work_items.record_github_response(
                    conn,
                    task[1],
                    actor_identity=trigger.get("actor_login") or "unknown",
                    body=trigger.get("body") or "",
                    event_fingerprint=trigger["event_fingerprint"],
                    metadata={"source_key": trigger.get("source_key")},
                    now=now,
                )
            related = (
                [trigger]
                + [event for _, event in events if event is not trigger]
                + run_once._pending_events_for_task(conn, task[0], [])
            )
            # Prepare externally, then atomically hand the whole observed batch
            # to one child before closing the parent and acknowledging receipts.
            route = run_once._effective_route(self.config, repo, trigger)
            conn.commit()
            workspace = run_once._prepare_worktree(repo, trigger, route, False)
            if not workflow_ownership.allows(
                conn, binding["subject_type"], binding["subject_id"]
            ):
                return
            child = run_once._create_task_attempt_and_prompt(
                conn,
                self.data_dir,
                self.db_path,
                repo,
                run_once._repo_id(repo["full_name"]),
                trigger,
                route,
                {
                    "workstream_id": task[1],
                    "source_relationship": run_once.workstream.source_relationship(
                        trigger
                    ),
                },
                now,
                worktree_result=workspace,
                parent_task_id=task[0],
                related_events=related,
            )
            if not child:
                raise ValueError("workflow_child_not_created")
            if task[2] == "waiting_for_user":
                conn.execute(
                    "UPDATE tasks SET lifecycle='completed',updated_at=? WHERE task_id=?",
                    (now, task[0]),
                )
            run_once._close_task_binding_if_terminal(conn, task[0], now)
        else:
            for _, event in events:
                run_once._append_event_to_active_task(
                    conn, event, "pending" if trusted(event) else "context", now
                )
        for signal in signals:
            workflow_store.consume_signal_once(
                conn, signal["signal_id"], binding["workflow_id"], signal["topic"], now
            )

    def advance(self, subject_type, subject_id):
        """One restartable domain cycle; remote effects keep their own intents."""
        now = run_once._now()
        with closing(sqlite3.connect(self.db_path)) as conn:
            binding = workflow_store.get_binding(conn, subject_type, subject_id)
            if (
                not binding
                or binding["owner_engine"] != "dbos"
                or binding["definition_version"] != "v2"
            ):
                return {"status": "attention", "reason_code": "workflow_owner_mismatch"}
            if binding["binding_status"] != "active":
                return {
                    "status": "completed"
                    if binding["binding_status"] == "terminal"
                    else "attention"
                }
            task = self._task(conn, subject_type, subject_id)
            if not task:
                return self.attention(subject_type, subject_id, "workflow_task_missing")
            repo_id = conn.execute(
                "SELECT repo_id FROM workstreams WHERE workstream_id=?", (task[1],)
            ).fetchone()[0]
        if repo_id not in self.repos:
            return self.attention(subject_type, subject_id, "repository_not_configured")
        repo = self.repos[repo_id]
        run_id = "run-" + uuid4().hex[:12]
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            lease = run_once._acquire_agent_lease(
                conn, repo_id, run_id, now, self.config.get("lease_ttl_minutes", 9)
            )
            if not lease["ok"]:
                previous = json.loads(conn.execute("SELECT metadata_json FROM tasks WHERE task_id=?", (task[0],)).fetchone()[0]).get("wait_state", {})
                conn.execute("UPDATE tasks SET metadata_json=json_set(metadata_json, '$.wait_state', json(?)) WHERE task_id=?",
                    (json.dumps({"reason_code": "repository_busy", "since": previous.get("since") or now,
                                 "lease_expires_at": lease["expires_at"]}), task[0]))
                return {"status": "waiting", "reason_code": "repository_busy"}
            conn.execute("UPDATE tasks SET metadata_json=json_remove(metadata_json, '$.wait_state') WHERE task_id=?", (task[0],))
            conn.execute(
                "INSERT INTO agent_runs(run_id,status,started_at,config_path,dry_run) VALUES (?,'running',?,'dbos:v2',0)",
                (run_id, now),
            )
        outcome = {"status": "waiting"}
        try:
            with workflow_ownership.executing(subject_type, subject_id):
                queue = []
                budget = {"remaining": 1}
                with closing(sqlite3.connect(self.db_path)) as conn, conn:
                    conn.execute("PRAGMA foreign_keys=ON")
                    binding = workflow_store.get_binding(conn, subject_type, subject_id)
                    if binding["binding_status"] != "active":
                        outcome = {"status": binding["binding_status"]}
                        return outcome
                    self._consume_events(conn, binding, task, repo, now)
                    run_once._supervise_running_attempts(
                        conn,
                        repo_id,
                        self.config,
                        now,
                        data_dir=self.data_dir,
                        db_path=self.db_path,
                        repo=repo,
                    )
                    run_once._recover_failed_timeout_attempts(
                        conn,
                        repo_id,
                        now,
                        data_dir=self.data_dir,
                        db_path=self.db_path,
                        repo=repo,
                    )
                    run_once._audit_completed_results(
                        conn,
                        repo_id,
                        self.data_dir,
                        self.db_path,
                        repo,
                        run_id,
                        now,
                        False,
                        queue,
                        budget,
                        config_result=self.config,
                    )
                publish.publish_ready_actions(
                    self.db_path,
                    run_command=self.publish_runner,
                    repo_id=repo_id,
                    task_ids=[task[0]],
                )
                with closing(sqlite3.connect(self.db_path)) as conn, conn:
                    run_once._finalize_published_results(
                        conn,
                        repo_id,
                        self.data_dir,
                        self.db_path,
                        repo,
                        run_id,
                        run_once._now(),
                        False,
                        queue,
                        budget,
                        config_result=self.config,
                    )
                    latest = self._task(conn, subject_type, subject_id)
                    binding = workflow_store.get_binding(conn, subject_type, subject_id)
                    if latest[2] in {"completed", "failed", "canceled", "ignored"}:
                        if subject_type == "task":
                            failure = conn.execute(
                                "SELECT failure_json,finished_at FROM attempts WHERE task_id=? ORDER BY attempt_no DESC LIMIT 1",
                                (latest[0],),
                            ).fetchone()
                            if (
                                latest[2] == "failed"
                                and failure
                                and json.loads(failure[0] or "{}").get("status")
                                == "failed_timeout"
                                and binding["binding_status"] == "active"
                            ):
                                if failure[1] == now:
                                    outcome = {
                                        "status": "waiting",
                                        "reason_code": "timeout_recovery_pending",
                                    }
                                    return outcome
                                conn.commit()
                                outcome = self.attention(
                                    subject_type,
                                    subject_id,
                                    "timeout_recovery_unavailable",
                                )
                                return outcome
                            if binding["binding_status"] == "active":
                                workflow_store.close_binding(
                                    conn, subject_type, subject_id, run_once._now()
                                )
                            outcome = {"status": latest[2]}
                        else:
                            episode = conn.execute(
                                "SELECT status FROM pr_remediation_episodes WHERE episode_id=?",
                                (subject_id,),
                            ).fetchone()
                            if episode[0] in {
                                "resolved",
                                "exhausted",
                                "superseded",
                                "canceled",
                            }:
                                if binding["binding_status"] == "active":
                                    workflow_store.close_binding(
                                        conn, subject_type, subject_id, run_once._now()
                                    )
                                outcome = {"status": episode[0]}
                    if binding["binding_status"] == "attention":
                        outcome = {"status": "attention"}
                        return outcome
                    attempt = conn.execute(
                        "SELECT attempt_id FROM attempts WHERE task_id=? AND status='prepared' ORDER BY attempt_no DESC LIMIT 1",
                        (latest[0],),
                    ).fetchone()
                    if attempt and workflow_ownership.allows(
                        conn, subject_type, subject_id
                    ):
                        target = run_once._current_dispatch_target(conn, attempt[0])
                        metadata = json.loads(binding["metadata_json"] or "{}")
                        if metadata.get("dispatch_inflight") == attempt[0]:
                            conn.commit()
                            outcome = self.attention(
                                subject_type, subject_id, "dispatch_outcome_unknown"
                            )
                            return outcome
                        if target:
                            # Committed fence prevents a replay from spawning a second process
                            # after an ambiguous crash between spawn and PID persistence.
                            metadata["dispatch_inflight"] = attempt[0]
                            conn.execute(
                                "UPDATE workflow_bindings SET metadata_json=? WHERE subject_type=? AND subject_id=?",
                                (json.dumps(metadata), subject_type, subject_id),
                            )
                            queue = [
                                {
                                    "task_info": target[0],
                                    "worktree_result": target[1],
                                    "repo_id": repo_id,
                                    "repo": repo,
                                }
                            ]
                        else:
                            queue = []
                    else:
                        queue = []
                if queue:
                    run_once._dispatch_queued(
                        self.db_path,
                        list(self.repos),
                        queue,
                        False,
                        self.config.get("max_concurrency", 1),
                        self.config.get("workers"),
                        self.config.get("default_worker"),
                        self.config.get("route_worker_models"),
                        self.config,
                    )
                    with closing(sqlite3.connect(self.db_path)) as conn, conn:
                        binding = workflow_store.get_binding(
                            conn, subject_type, subject_id
                        )
                        metadata = json.loads(binding["metadata_json"] or "{}")
                        metadata.pop("dispatch_inflight", None)
                        conn.execute(
                            "UPDATE workflow_bindings SET metadata_json=? WHERE subject_type=? AND subject_id=?",
                            (json.dumps(metadata), subject_type, subject_id),
                        )
            return outcome
        except Exception as exc:
            outcome = self.attention(
                subject_type, subject_id, "workflow_step_" + type(exc).__name__
            )
            return outcome
        finally:
            with closing(sqlite3.connect(self.db_path)) as conn, conn:
                run_once._release_agent_lease(conn, lease["lease_id"], run_once._now())
                conn.execute(
                    "UPDATE agent_runs SET status=?,finished_at=?,summary_json=? WHERE run_id=?",
                    (
                        "failed" if outcome["status"] == "attention" else "completed",
                        run_once._now(),
                        json.dumps(outcome),
                        run_id,
                    ),
                )

    def start_pending(self):
        from robert_agent import dbos_runtime

        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            bindings = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM workflow_bindings WHERE owner_engine='dbos' AND binding_status='active'"
                )
            ]
        started = 0
        for binding in bindings:
            with closing(sqlite3.connect(self.db_path)) as conn:
                task = self._task(conn, binding["subject_type"], binding["subject_id"])
                repo_id = (
                    conn.execute(
                        "SELECT repo_id FROM workstreams WHERE workstream_id=?",
                        (task[1],),
                    ).fetchone()[0]
                    if task
                    else None
                )
            if repo_id not in self.repos:
                continue
            if binding["definition_version"] != "v2":
                raise ValueError(
                    "unsupported active workflow definition; drain with its original driver"
                )
            runtime = dbos_runtime.current_runtime()
            status = (
                runtime.dbos.get_workflow_status(binding["workflow_id"])
                if runtime and runtime.dbos
                else None
            )
            if status and status.status in {
                "ERROR",
                "CANCELLED",
                "MAX_RECOVERY_ATTEMPTS_EXCEEDED",
                "SUCCESS",
            }:
                self.attention(
                    binding["subject_type"],
                    binding["subject_id"],
                    "workflow_runtime_" + status.status.lower(),
                )
                continue
            if status is None:
                dbos_runtime.start_bound_workflow(binding)
                started += 1
        return started
