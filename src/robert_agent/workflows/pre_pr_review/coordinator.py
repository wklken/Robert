"""Controller for a reviewed new PR, from local commit to audited result."""

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import signal
import subprocess

from robert_agent.adapters.git.review_revision import clean_head, git, remote_branch_sha
from robert_agent.context.native_prompts import address_findings_input
from robert_agent.context.prompts import render_prompt
from robert_agent.delivery import contract
from robert_agent.delivery.application import submission, validation, audit
from robert_agent.delivery.lock import task_publication_lock
from robert_agent.execution import retry
from robert_agent.execution.stages import StageStopped, StageSuperseded, attempt_is_active, terminate_children
from robert_agent.foundation.contracts.execution import ModelRunStatus
from robert_agent.persistence import transactions
from robert_agent.persistence.stores import artifacts as db_write_artifacts
from robert_agent.persistence.rounds import pre_pr_review as store
from robert_agent.security.redaction import redact_and_truncate, redact_text
from robert_agent.workflows.review import files, workspace
from robert_agent.workflows.review.coordinator import ReviewStages


class ReviewDecisionRequired(StageStopped):
    def __init__(self, path, value):
        super().__init__("a review finding requires an operator decision")
        self.path, self.value = path, value


class CandidateWorkspace:
    @staticmethod
    def verify_snapshot(manifest):
        return manifest.get("implementing") or workspace.verify_snapshot(manifest)


class Coordinator(ReviewStages):
    store = store
    workspace = CandidateWorkspace()
    handoff_files = {
        **ReviewStages.handoff_files,
        "implement": ("candidate.json",),
        "address_findings": ("dispositions.json",),
    }
    artifact_kinds = (*ReviewStages.artifact_kinds, "candidate", "dispositions")
    stage = "implement"

    def __init__(self, db_path, task_id, attempt_id, *, observe_base=remote_branch_sha, **kwargs):
        super().__init__(db_path, task_id, attempt_id, **kwargs)
        self.plan = self.request["plan"]
        self.request = {**self.request, "workers": self.plan["workers"]}
        self.repo = self.request["repo"]
        self.observe_base = observe_base
        with store.connect(db_path) as conn:
            settings, rejected = validation.context(conn, task_id, attempt_id)
        if rejected:
            raise StageStopped("the task no longer authorizes implementation")
        self.origin_type = settings["origin_type"]
        self.scope = settings["action_scope"]
        reply = self.request.get("operator_reply")
        if reply:
            if any(self.scope.get(key) != value for key, value in reply["scope"].items()):
                raise StageStopped("operator reply cannot change the original review scope")
            try:
                store.validate_reply_checkpoint(self.session, reply["decision"])
            except (OSError, ValueError) as exc:
                raise StageStopped(str(exc)) from exc
        self.assigned_path = self.scope["worktree_path"]
        self.branch = self.scope["branch_name"]
        self.base = self.scope["base_branch"]
        self.root = self.data_dir / "artifacts" / "pre-pr-reviews"
        self.implementer = self.plan["implementer"]
        self.deadline = None

    def check_active(self):
        super().check_active()
        if self.deadline and datetime.now(timezone.utc) >= self.deadline:
            raise StageStopped("pre-publication review deadline expired")

    def save_manifest(self):
        with store.connect(self.db_path) as conn:
            transactions.begin(conn, immediate=True)
            if not attempt_is_active(conn, self.task_id, self.attempt_id):
                raise StageStopped("candidate owner changed")
            store.save_snapshot(conn, self.round_id, self.manifest)

    def check_launch_budget(self, stage, prompt_hash, previous):
        failed = [
            run for run in previous if run["status"] == ModelRunStatus.FAILED and run["prompt_sha256"] == prompt_hash
        ]
        override = self.request.get("retry_attempt_id") == self.attempt_id and not any(
            run["owner_attempt_id"] == self.attempt_id for run in failed
        )
        if len(failed) >= retry.MAX_ATTEMPTS and not override:
            raise StageStopped(f"{stage} retry budget exhausted")

    def phase_input_changed(self, stage):
        return stage != "implement" or super().phase_input_changed(stage)

    def stage_environment(self, run):
        environment = super().stage_environment(run)
        if run["stage"] == "implement":
            environment["ROBERT_PRE_PR_CANDIDATE_DIR"] = run["output_dir"]
        return environment

    def stage_worktree(self, run):
        if run["stage"] == "address_findings":
            path = self.address_workspace(run["run_id"])
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                git(self.repo["repo_root"], "worktree", "add", "--detach", str(path), self.manifest["head_sha"])
            return str(path)
        return super().stage_worktree(run)

    def address_workspace(self, run_id):
        return Path(self.repo["worktree_root"]) / "pre-pr-address" / self.round_id / run_id

    def validate_candidate(self, output, _model):
        path = output / "candidate.json"
        candidate = json.loads(path.read_text(encoding="utf-8"))
        self.validate_submission(candidate)
        return {
            "candidate_path": str(path),
            "candidate_sha256": store.digest(path),
            "head": clean_head(self.assigned_path, self.branch),
        }

    def envelope(self, payload):
        return {
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "output_type": "new_pr",
            "submission": payload,
        }

    def recovery_workers(self):
        return {**super().recovery_workers(), self.implementer["name"]: self.implementer}

    def prepare(self):
        self.phase("preparing")
        candidate = self.manifest.get("candidate")
        if not candidate:
            root = self.root / self.round_id
            root.mkdir(parents=True, exist_ok=True)
            prompt_path = Path(self.request["prompt_path"])
            if store.digest(prompt_path) != self.request["prompt_sha256"]:
                raise StageStopped("recorded implementation prompt changed")
            self.manifest = {
                "implementing": True,
                "artifact_root": str(root),
                "worktree_path": self.assigned_path,
                "context_dir": str(prompt_path.parent),
            }
            self.stage = "implement"
            candidate = self.run_phase("implement", self.implementer, prompt_path, self.validate_candidate)
        self.stage = "snapshot"
        base_sha = self.observe_base(self.repo["repo_root"], "upstream", self.base)
        if self.manifest.get("base_sha") != base_sha:
            git(self.repo["repo_root"], "fetch", "upstream", base_sha)
            self.manifest = workspace.prepare_candidate_snapshot(
                self.repo["repo_root"],
                self.repo["worktree_root"],
                self.root,
                self.round_id,
                self.repo["full_name"],
                self.base,
                base_sha,
                candidate["head"],
                self.request["event"],
            )
            self.manifest["candidate"] = candidate
            self.manifest["github_account"] = self.repo["github_account"]
            if not self.manifest["frozen"]:
                files.atomic_json(Path(self.manifest["context_dir"]) / "manifest.json", self.manifest)
            self.save_manifest()
        self.check_base()
        self.stage = "prepare"
        self.prepare_context()

    def check_base(self):
        if self.observe_base(self.repo["repo_root"], "upstream", self.base) != self.manifest["base_sha"]:
            raise StageSuperseded("target base changed after candidate snapshot")

    def report(self, records):
        self.stage = "synthesis"
        if len(records) == 1:
            result = files.read_result(records[0]["result_path"], self.manifest)
            items = result["findings"] + result["dismissed_findings"]
            body = files.render_individual(result, records[0]["worker_id"], records[0]["model"])
        else:
            evaluation = self.evaluate(records)
            items = evaluation["items"]
            body = files.render_merged(self.manifest, records, evaluation)
        body += "\n\n## Finding IDs\n" + "\n".join(f"- `{item['id']}` {item['title']}" for item in items)
        report = redact_text(body)
        if not report["ok"]:
            raise StageStopped("review report did not pass public content redaction")
        body = report["text"]
        # The exact review input set determines the immutable report path.
        path = self.prompt(f"report-{hashlib.sha256(body.encode()).hexdigest()}.md", body)
        sha = store.digest(path)
        self.check_active()
        with store.connect(self.db_path) as conn:
            transactions.begin(conn, immediate=True)
            if not attempt_is_active(conn, self.task_id, self.attempt_id):
                raise StageStopped("review owner changed")
            store.update(conn, self.round_id, report_path=str(path), report_sha256=sha)
            db_write_artifacts.insert_for_register(
                conn,
                f"{self.round_id}:{sha}",
                self.task_id,
                self.attempt_id,
                "pre_pr_review_report",
                str(path),
                sha,
                path.stat().st_size,
                store.now(),
            )
        return path, sha, [item["id"] for item in items]

    def address(self, path, report_sha, ids):
        self.stage = "address_findings"
        self.phase(self.stage)
        candidate_path = self.manifest["candidate"]["candidate_path"]
        head = self.manifest["head_sha"]
        reply = self.request.get("operator_reply")
        generation = hashlib.sha256(json.dumps(reply, sort_keys=True).encode()).hexdigest() if reply else ""
        input_hash = report_sha + ("-" + generation if generation else "")
        prompt = self.prompt(
            "address-" + input_hash + ".md",
            render_prompt(
                address_findings_input(
                    self.manifest,
                    candidate_path,
                    path,
                    report_sha,
                    ids,
                    operator_reply=reply,
                )
            ).text,
        )

        def validate(output, _model):
            disposition = output / "dispositions.json"
            value = json.loads(disposition.read_text(encoding="utf-8"))
            if (
                not isinstance(value, dict)
                or value.get("candidate_sha") != head
                or value.get("report_sha256") != report_sha
                or not isinstance(value.get("body"), str)
                or not value["body"].strip()
                or not isinstance(value.get("verification"), list)
                or not isinstance(value.get("dispositions"), list)
            ):
                raise ValueError("invalid finding disposition handoff")
            seen = set()
            for item in value["dispositions"]:
                if (
                    not isinstance(item, dict)
                    or not isinstance(item.get("id"), str)
                    or item["id"] not in ids
                    or item["id"] in seen
                    or item.get("status") not in ("fixed", "dismissed", "deferred", "needs_decision")
                    or not isinstance(item.get("reason"), str)
                    or not item["reason"].strip()
                ):
                    raise ValueError("each finding needs exactly one supported disposition with a reason")
                seen.add(item["id"])
            if seen != set(ids):
                raise ValueError("disposition handoff omitted a finding")
            if any(item["status"] == "needs_decision" for item in value["dispositions"]):
                raise ReviewDecisionRequired(disposition, value)
            tree = self.address_workspace(output.name)
            final = clean_head(tree)
            if final != value.get("final_sha"):
                raise ValueError("verification does not match the final worktree HEAD")
            git(tree, "merge-base", "--is-ancestor", head, final)
            if final == head and any(item["status"] == "fixed" for item in value["dispositions"]):
                raise ValueError("a finding marked fixed requires a new commit")
            if not any(isinstance(check, dict) and check.get("status") == "passed" for check in value["verification"]):
                raise ValueError("final verification must include a passing check")
            if any(not isinstance(check, dict) or check.get("status") != "passed" for check in value["verification"]):
                raise ValueError("final verification contains an unsuccessful check")
            draft = self.candidate_submission()
            draft["verification"] = value["verification"]
            open_pr = next(action for action in draft["planned_github_actions"] if action["type"] == "open_pr")
            open_pr["body"] = value["body"]
            self.validate_submission(draft)
            return {
                "dispositions_path": str(disposition),
                "dispositions_sha256": store.digest(disposition),
                "head": final,
            }

        return self.run_phase(self.stage, self.implementer, prompt, validate, input_hash=input_hash)

    def wait_for_decision(self, decision, report_sha):
        self.check_active()
        self.check_base()
        pending = [item for item in decision.value["dispositions"] if item["status"] == "needs_decision"]
        summary = "Review findings require a decision within the original task scope.\n" + "\n".join(
            f"{item['id']}: {item['reason']}" for item in pending
        )
        summary = redact_and_truncate(summary, 1900).get("text", "Review findings require an operator decision.")
        binding = {
            "round_id": self.round_id,
            "snapshot_id": self.manifest["snapshot_id"],
            "candidate_sha": self.manifest["head_sha"],
            "report_sha256": report_sha,
            "dispositions_path": str(decision.path),
            "dispositions_sha256": store.digest(decision.path),
        }
        draft = self.candidate_submission()
        draft.update(
            planned_github_actions=[]
            if self.origin_type == "web"
            else [{"type": "comment", "target_url": self.request["event"]["html_url"], "body": summary}],
            operator_question={"kind": "scope_decision", "summary": summary, "choices": []},
            handoff="Waiting for an authenticated reply within the original task scope.",
        )
        envelope = {**self.envelope(draft), "output_type": "waiting_for_user"}
        scope_keys = ("repo", "github_account", "base_branch", "worktree_path", "branch_name", "remote", "sources")
        proof = {
            "attempt_id": self.attempt_id,
            "snapshot_id": self.manifest["snapshot_id"],
            "report_sha256": report_sha,
            "head_sha": "",
            "decision": binding,
            "scope": {key: self.scope[key] for key in scope_keys},
            "fingerprint": contract.semantic_fingerprint(envelope),
        }
        with store.connect(self.db_path) as conn:
            transactions.begin(conn, immediate=True)
            if not attempt_is_active(conn, self.task_id, self.attempt_id):
                raise StageStopped("review owner changed before question handoff")
            if any(run["slot_reserved"] for run in store.runs(conn, self.round_id)):
                raise StageStopped("review processes must exit before requesting a reply")
            store.update(conn, self.round_id, status="waiting_for_user", proof_json=json.dumps(proof, sort_keys=True))
        result = submission.record_result(self.db_path, envelope)
        if not result.get("ok"):
            raise StageStopped("review question submission blocked: " + result.get("reason_code", result["status"]))
        return result

    def candidate_submission(self):
        candidate = self.manifest["candidate"]
        if store.digest(candidate["candidate_path"]) != candidate["candidate_sha256"]:
            raise StageStopped("candidate handoff changed")
        return json.loads(Path(candidate["candidate_path"]).read_text(encoding="utf-8"))

    def validate_submission(self, submission_payload):
        with store.connect(self.db_path) as conn:
            settings, rejected = validation.context(conn, self.task_id, self.attempt_id)
        if rejected:
            raise StageStopped("candidate attempt is no longer current")
        normalized = contract.normalize(self.envelope(submission_payload), settings)
        checked = audit.audit_result(normalized, final=False, **settings)
        if not checked["ok"]:
            raise ValueError("handoff failed the new PR result contract")
        return normalized

    def finalize(self, report, report_sha, disposition):
        self.stage = "finalize"
        self.phase(self.stage)
        head, final = self.manifest["head_sha"], disposition["head"]
        if (
            store.digest(report) != report_sha
            or store.digest(disposition["dispositions_path"]) != disposition["dispositions_sha256"]
        ):
            raise StageStopped("review handoff changed")
        body = report.read_text(encoding="utf-8")
        draft = self.candidate_submission()
        response = json.loads(Path(disposition["dispositions_path"]).read_text(encoding="utf-8"))
        draft["verification"] = response["verification"]
        action = next(action for action in draft["planned_github_actions"] if action["type"] == "open_pr")
        fixed = [f"- {item['id']}：{item['reason']}" for item in response["dispositions"] if item["status"] == "fixed"]
        deferred = [
            f"- {item['id']}（{item['status']}）：{item['reason']}"
            for item in response["dispositions"]
            if item["status"] in ("deferred", "dismissed")
        ]
        append = "\n".join(
            [
                "## Review 报告",
                f"送审提交：`{head}`",
                f"最终提交：`{final}`",
                "",
                body,
                "",
                "## 已修复",
                *(fixed or ["无。"]),
                "",
                "## 未处理及原因",
                *(deferred or ["无。"]),
            ]
        )
        redacted = redact_text(append)
        if not redacted["ok"]:
            raise StageStopped("review dispositions did not pass public content redaction")
        action["body"] = response["body"] + "\n\n" + redacted["text"]
        normalized = self.validate_submission(draft)
        normalized_pr = next(action for action in normalized["planned_github_actions"] if action["type"] == "open_pr")
        envelope = self.envelope(draft)
        fingerprint = contract.semantic_fingerprint(envelope)
        self.check_base()
        with task_publication_lock(self.db_path, self.task_id) as locked:
            if not locked:
                raise RuntimeError("review delivery lock is busy")
            self.check_active()
            current = clean_head(self.assigned_path, self.branch)
            if current == head and final != head:
                git(self.assigned_path, "merge", "--ff-only", final)
            if clean_head(self.assigned_path, self.branch) != final:
                raise StageSuperseded("assigned branch changed before final review handoff")
            proof = {
                "attempt_id": self.attempt_id,
                "snapshot_id": self.manifest["snapshot_id"],
                "candidate_sha": head,
                "head_sha": final,
                "base_ref": self.base,
                "base_sha": self.manifest["base_sha"],
                "report_sha256": report_sha,
                "dispositions_sha256": disposition["dispositions_sha256"],
                "fingerprint": fingerprint,
                "body_sha256": hashlib.sha256(normalized_pr["body"].encode()).hexdigest(),
            }
            with store.connect(self.db_path) as conn:
                transactions.begin(conn, immediate=True)
                if not attempt_is_active(conn, self.task_id, self.attempt_id):
                    raise StageStopped("review owner changed before final handoff")
                store.seal_result(conn, self.round_id, proof)
            result = submission.record_result(self.db_path, envelope)
        if not result.get("ok"):
            raise StageStopped("audited new PR submission blocked: " + result.get("reason_code", result["status"]))
        return result

    def run(self):
        with task_publication_lock(self.db_path, f"pre-pr-coordinator:{self.task_id}") as locked:
            if not locked:
                raise StageStopped("a coordinator already owns this task")
            with store.connect(self.db_path) as conn:
                transactions.begin(conn, immediate=True)
                if not attempt_is_active(conn, self.task_id, self.attempt_id):
                    raise StageStopped("candidate owner changed")
                entry = store.session(conn, task_id=self.task_id)
                now = datetime.now(timezone.utc)
                self.deadline = (
                    datetime.fromisoformat(entry["deadline_at"])
                    if entry["deadline_at"]
                    else now + timedelta(seconds=self.plan["timeout_seconds"])
                )
                store.update(conn, self.round_id, deadline_at=self.deadline.isoformat())
            if scheduled := entry["next_retry_at"]:
                seconds = (datetime.fromisoformat(scheduled) - datetime.now(timezone.utc)).total_seconds()
                if seconds > 0:
                    self.stopping.wait(min(seconds, self.plan["timeout_seconds"]))
            return self.run_stages()

    def run_stages(self):
        while True:
            try:
                self.reconcile_running()
                self.prepare()
                self.stage = "review"
                records = self.review()
                report, sha, ids = self.report(records)
                disposition = self.address(report, sha, ids)
                return self.finalize(report, sha, disposition)
            except ReviewDecisionRequired as exc:
                return self.wait_for_decision(exc, sha)
            except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as exc:
                error = redact_and_truncate(str(exc), 500).get("text", "[redacted]")
                with store.connect(self.db_path) as conn:
                    stopped = terminate_children(conn, self.task_id, store=store)
                    stage_runs = [run for run in store.runs(conn, self.round_id) if run["stage"] == self.stage]
                failed = {}
                for run in stage_runs:
                    failed.setdefault(run["worker_id"], []).append(run)
                counts = [
                    sum(item["status"] == ModelRunStatus.FAILED for item in history)
                    for history in failed.values()
                    if history[-1]["status"] == ModelRunStatus.FAILED
                ]
                can_retry = (
                    bool(counts)
                    and max(counts) < retry.MAX_ATTEMPTS
                    and stopped != "attention"
                    and not isinstance(exc, (StageStopped, StageSuperseded))
                )
                delay = 5 * 2 ** (max(counts) - 1) if can_retry else 0
                next_retry = datetime.now(timezone.utc) + timedelta(seconds=delay)
                can_retry = can_retry and next_retry < self.deadline
                with store.connect(self.db_path) as conn:
                    transactions.begin(conn, immediate=True)
                    if attempt_is_active(conn, self.task_id, self.attempt_id):
                        store.update(
                            conn,
                            self.round_id,
                            status="retry_wait" if can_retry else "attention",
                            next_retry_at=next_retry.isoformat() if can_retry else None,
                            error=f"{self.stage}: {error}",
                        )
                if not can_retry:
                    raise
                if self.manifest.get("implementing"):
                    self.manifest = {}
                self.stopping.wait(delay)
                self.check_active()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--attempt-id", required=True)
    args = parser.parse_args(argv)
    coordinator = Coordinator(args.db, args.task_id, args.attempt_id)
    signal.signal(signal.SIGTERM, lambda *_args: coordinator.stopping.set())
    coordinator.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
