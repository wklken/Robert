"""Independent, bounded decision calls over durable Issue/PR snapshots."""

from robert_agent.foundation.contracts.execution import ModelCommandStatus
from robert_agent.foundation.contracts.supervision import DecisionStatus, DecisionRunStatus

from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading
import os

from robert_agent.adapters.processes import identity, model_command
from robert_agent.configuration.supervision import normalize_diagnosis
from robert_agent.persistence import database, errors, transactions
from robert_agent.persistence.queries import supervision_decisions as queries
from robert_agent.persistence.stores import notifications
from robert_agent.security.text import safe_text
from robert_agent.supervision import decisions, store
from robert_agent.supervision.rules import timestamp


def _connect(config):
    return database.connect(f"{Path(config['db_path']).resolve().as_uri()}?mode=rw", uri=True, timeout=0.1)


def _read_runner(conn):
    return (store.get_checkpoint(conn, *decisions.RUNNER_KEY) or {}).get("metadata", {})


def _write_runner(conn, value, now):
    store.update_checkpoint(conn, *decisions.RUNNER_KEY, now=now, metadata=value)


class DecisionLoop:
    """Runtime-owned thread; durable claims fence other instances and restarts."""

    def __init__(self):
        self.thread = None
        self.stop = threading.Event()
        self.result = None

    def _started(self, config, request, process_identity):
        with closing(_connect(config)) as conn, conn:
            transactions.begin(conn, immediate=True)
            state = _read_runner(conn)
            claim = state.get("claim") or {}
            if claim.get("bundle_id") != request["bundle_id"]:
                return False
            _write_runner(
                conn, {**state, "claim": {**claim, "process_identity": process_identity}}, claim["claimed_at"]
            )
            return True

    def _run(self, config, profile, request):
        try:
            outcome = model_command.run(
                profile,
                decisions.encode(request),
                self.stop,
                lambda process_identity: self._started(config, request, process_identity),
            )
            output = None
            if outcome["status"] in {ModelCommandStatus.READ.value, ModelCommandStatus.EXITED.value} and outcome.get(
                "output"
            ):
                try:
                    output = decisions.validate(outcome["output"], request)
                    if "error" in output:
                        outcome.update(
                            status=DecisionRunStatus.PROVIDER_RATE_LIMITED.value, provider_error=output["error"]
                        )
                        output = None
                    elif outcome["status"] != ModelCommandStatus.READ.value:
                        output = None
                except (ValueError, RecursionError, UnicodeError):
                    outcome["status"] = ModelCommandStatus.INVALID_OUTPUT.value
                    outcome["safe_error"] = outcome.get("safe_error") or "Model output failed the decision contract."
            if outcome["status"] == ModelCommandStatus.READ.value and output is None:
                outcome["status"] = ModelCommandStatus.INVALID_OUTPUT.value
                outcome["safe_error"] = outcome.get("safe_error") or "Model returned no valid decision."
            self.result = (request, outcome, output)
        except (OSError, errors.Error, ValueError):
            # An unresolved identity keeps its durable claim for reconciliation.
            self.result = (request, {"status": ModelCommandStatus.PROCESS_ATTENTION.value, "stopped": False}, None)

    def _finish(self, config, now):
        request, outcome, output = self.result
        with closing(_connect(config)) as conn, conn:
            transactions.begin(conn, immediate=True)
            runner = _read_runner(conn)
            claim = runner.get("claim") or {}
            if claim.get("bundle_id") != request["bundle_id"]:
                return {"status": DecisionRunStatus.CLAIM_CHANGED.value}
            state = decisions.read(conn, request["root_id"])
            status = outcome["status"]
            if not outcome["stopped"]:
                status = ModelCommandStatus.PROCESS_ATTENTION.value
                output = None
            if output is not None:
                status = (
                    DecisionRunStatus.DECIDED.value
                    if decisions.approve(conn, config, request, output, now)
                    else DecisionRunStatus.STALE_EVIDENCE.value
                )
            if status != DecisionRunStatus.DECIDED.value:
                if output is None:
                    failure = {
                        "decision_id": request["bundle_id"],
                        "status": status,
                        "failed_at": now,
                        "safe_error": safe_text(outcome.get("safe_error"), 2000),
                        "exit_code": outcome.get("exit_code"),
                        "stderr_truncated": bool(outcome.get("stderr_truncated")),
                    }
                    state = {**state, "last_failure": failure}
                    runner["last_failure"] = failure
                    store.update_checkpoint(
                        conn,
                        "decision_record",
                        request["root_id"],
                        request["bundle_id"],
                        now=now,
                        metadata={"status": status, "last_failure": failure},
                    )
                decisions.write(
                    conn,
                    request["root_id"],
                    {
                        **state,
                        "status": status,
                        "next_check_at": (timestamp(now) + timedelta(seconds=claim["cooldown_seconds"])).isoformat(),
                    },
                    now,
                )
            failures = (
                0
                if status in {DecisionRunStatus.DECIDED.value, DecisionRunStatus.STALE_EVIDENCE.value}
                else runner.get("failures", 0) + 1
            )
            cooldown = max(
                claim["cooldown_seconds"], (outcome.get("provider_error") or {}).get("retry_after_seconds", 0)
            )
            runner.update(
                claim=None if outcome["stopped"] else claim,
                failures=failures,
                last_status=status,
                next_check_at=(timestamp(now) + timedelta(seconds=cooldown)).isoformat(),
            )
            if failures >= 2 and not runner.get("notice_sent"):
                notifications.insert_notification(
                    conn,
                    "decision-unavailable-" + request["bundle_id"],
                    None,
                    "supervision_decision_unavailable",
                    now,
                    decisions.encode(
                        {
                            "reason_code": status,
                            "workstream_id": request["root_id"],
                            "decision_id": request["bundle_id"],
                            "exit_code": outcome.get("exit_code"),
                            "safe_error": safe_text(outcome.get("safe_error"), 2000),
                            "summary": "自动决策调用连续失败。这是全局监督服务告警，Kanban 不会生成任务确认项。",
                            "required_action": "在 /operations 通知中心或 robert supervisor status 中检查决策服务；ack 仅记录收悉，后续有效模型调用会自动解除告警。",
                            "retryable": False,
                        }
                    ),
                )
                runner["notice_sent"] = True
            if not failures:
                runner["notice_sent"] = False
                notifications.resolve_decision_unavailable(conn, now, request["bundle_id"])
            _write_runner(conn, runner, now)
        return {"status": status, "workstream_id": request["root_id"]}

    def _recover_previous(self, config, claim, now):
        recorded = claim.get("process_identity")
        if not recorded:
            owner = claim.get("owner_identity")
            if claim.get("root_id") and owner and identity.inspect_recorded_identity(owner) == "dead":
                # The owner cannot checkpoint a late spawn any more. Retain
                # unknown children and fence this root, rather than every root.
                with closing(_connect(config)) as conn, conn:
                    transactions.begin(conn, immediate=True)
                    runner = _read_runner(conn)
                    if runner.get("claim") != claim:
                        return {"status": DecisionRunStatus.CLAIM_CHANGED.value}
                    attention = runner.get("attention_claims", {})
                    if claim["root_id"] in attention:
                        return {"status": DecisionRunStatus.PROCESS_ATTENTION.value}
                    _write_runner(
                        conn,
                        {
                            **runner,
                            "claim": None,
                            "attention_claims": {**attention, claim["root_id"]: claim},
                            "last_status": DecisionRunStatus.PROCESS_ATTENTION.value,
                        },
                        now,
                    )
                    previous = decisions.read(conn, claim["root_id"])
                    decisions.write(
                        conn,
                        claim["root_id"],
                        {**previous, "status": DecisionRunStatus.PROCESS_ATTENTION.value},
                        now,
                    )
            return {"status": DecisionRunStatus.PROCESS_ATTENTION.value}
        state = identity.inspect_recorded_identity(recorded)
        if state == "alive_same" and timestamp(now) >= timestamp(claim["deadline_at"]):
            stopped = identity.terminate_recorded_identity(recorded, timeout_seconds=0.5)
            if stopped["status"] in {"terminated", "already_dead"}:
                state = "dead"
        if state != "dead" or identity.live_process_group(recorded["process_group_id"]):
            return {
                "status": DecisionRunStatus.PREVIOUS_PROCESS_RUNNING.value
                if state == "alive_same"
                else DecisionRunStatus.PROCESS_ATTENTION.value
            }
        with closing(_connect(config)) as conn, conn:
            transactions.begin(conn, immediate=True)
            runner = _read_runner(conn)
            if (runner.get("claim") or {}).get("bundle_id") != claim["bundle_id"]:
                return {"status": DecisionRunStatus.CLAIM_CHANGED.value}
            _write_runner(
                conn,
                {
                    **runner,
                    "claim": None,
                    "next_check_at": (timestamp(now) + timedelta(seconds=claim["cooldown_seconds"])).isoformat(),
                },
                now,
            )
        return {"status": DecisionRunStatus.INTERRUPTED.value}

    def poll(self, config, *, now=None):
        now = now or datetime.now(timezone.utc).isoformat()
        try:
            if self.thread:
                if self.thread.is_alive():
                    return {"status": DecisionRunStatus.RUNNING.value}
                result = self._finish(config, now)
                self.thread, self.result = None, None
                return result
            if not decisions.enabled(config):
                return {"status": DecisionRunStatus.DISABLED.value}
            profile = normalize_diagnosis(config["supervision"].get("decision"))
            if not profile:
                return {"status": DecisionRunStatus.DISABLED.value}
            owner_identity = identity.capture_process_identity(os.getpid())
            with closing(_connect(config)) as conn, conn:
                transactions.begin(conn, immediate=True)
                runner = _read_runner(conn)
                if claim := runner.get("claim"):
                    # Do not infer death from a missing PID or an elapsed lease.
                    conn.commit()
                    return self._recover_previous(config, claim, now)
                if timestamp(runner.get("next_check_at")) and timestamp(now) < timestamp(runner["next_check_at"]):
                    return {"status": DecisionRunStatus.COOLDOWN.value}
                recent = [
                    stamp for stamp in runner.get("calls", []) if timestamp(stamp) > timestamp(now) - timedelta(hours=1)
                ]
                if len(recent) >= profile["max_calls_per_hour"]:
                    return {"status": DecisionRunStatus.CALL_BUDGET_EXHAUSTED.value}
                repos = [repo["full_name"] for repo in config.get("repos", ()) if repo.get("enabled", True)]
                targets = queries.targets(
                    conn, repos, config["supervision"].get("batch_size", 100), runner.get("cursor", "")
                )
                if not targets:
                    runner["cursor"] = ""
                for target in targets:
                    root_id = target["workstream_id"]
                    runner["cursor"] = root_id
                    if root_id in runner.get("attention_claims", {}):
                        continue
                    previous = decisions.read(conn, root_id)
                    if previous.get("status") == DecisionStatus.ASK_USER:
                        continue
                    try:
                        snapshot = decisions.snapshot(conn, root_id, config, now)
                    except (ValueError, UnicodeError, RecursionError):
                        decisions.write(
                            conn, root_id, {**previous, "status": DecisionStatus.EVIDENCE_UNAVAILABLE.value}, now
                        )
                        continue
                    if not snapshot:
                        continue
                    if previous.get("status") == DecisionStatus.FINISH_SUPERVISION and decisions.can_finish(snapshot):
                        continue
                    due = timestamp(previous.get("next_check_at"))
                    if due and timestamp(now) < due and previous.get("input_generation") == decisions.digest(snapshot):
                        continue
                    try:
                        request = decisions.packet(snapshot, previous)
                    except (ValueError, UnicodeError):
                        decisions.write(
                            conn,
                            root_id,
                            {
                                **previous,
                                "status": DecisionStatus.EVIDENCE_UNAVAILABLE.value,
                                "next_check_at": (
                                    timestamp(now) + timedelta(seconds=profile["cooldown_seconds"])
                                ).isoformat(),
                                "input_generation": decisions.digest(snapshot),
                            },
                            now,
                        )
                        continue
                    claim = {
                        "bundle_id": request["bundle_id"],
                        "root_id": root_id,
                        "claimed_at": now,
                        "owner_identity": owner_identity,
                        "cooldown_seconds": profile["cooldown_seconds"],
                        "deadline_at": (timestamp(now) + timedelta(seconds=profile["timeout_seconds"])).isoformat(),
                    }
                    _write_runner(conn, {**runner, "claim": claim, "calls": [*recent, now]}, now)
                    # Keep the preceding outcome intact until a new decision is accepted.
                    decisions.write(conn, root_id, {**previous, "pending_bundle_id": request["bundle_id"]}, now)
                    conn.commit()
                    self.stop.clear()
                    self.thread = threading.Thread(target=self._run, args=(config, profile, request), daemon=True)
                    self.thread.start()
                    return {"status": DecisionRunStatus.LAUNCHED.value, "workstream_id": root_id}
                _write_runner(conn, runner, now)
                return {
                    "status": DecisionRunStatus.PROCESS_ATTENTION.value
                    if runner.get("attention_claims")
                    else DecisionRunStatus.IDLE.value
                }
        except (OSError, errors.Error, ValueError, RecursionError, identity.IdentityReadError):
            return {"status": DecisionRunStatus.UNAVAILABLE.value}

    def shutdown(self, config):
        if self.thread:
            self.stop.set()
            self.thread.join(timeout=5)
            if not self.thread.is_alive():
                return self.poll(config)
        return {"status": DecisionRunStatus.PROCESS_ATTENTION.value if self.thread else DecisionRunStatus.IDLE.value}
