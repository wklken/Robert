"""Controller for a reviewed new PR, from local commit to audited result."""

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import signal
import subprocess

from robert_agent.adapters.git.review_revision import clean_head, git, remote_branch_sha
from robert_agent.adapters.git.workspace import check_worktree_reuse
from robert_agent.adapters.git.merge_preflight import check_merge_environment, MergePreflightError
from robert_agent.context.native_prompts import address_findings_input
from robert_agent.context.prompts import render_prompt
from robert_agent.delivery import contract
from robert_agent.delivery.base_revisions import compatibility_refresh
from robert_agent.delivery.application import submission, validation, audit
from robert_agent.delivery.lock import task_publication_lock
from robert_agent.execution import retry
from robert_agent.execution.budgets import pipeline_timeout
from robert_agent.execution.stages import StageStopped, StageSuperseded, attempt_is_active, terminate_children
from robert_agent.foundation import question_choices
from robert_agent.foundation.contracts.execution import ModelRunStatus
from robert_agent.persistence import transactions
from robert_agent.persistence.stores import artifacts as db_write_artifacts
from robert_agent.persistence.rounds import pre_pr_review as store
from robert_agent.persistence.rounds.records import artifact_available
from robert_agent.security.redaction import redact_and_truncate, redact_report
from robert_agent.security import redaction
from robert_agent.workflows.review import files, workspace, report_checkpoint
from robert_agent.workflows.review.coordinator import ReviewStages
from robert_agent.workflows.pre_pr_review import base_impact, final_verification, handoff_repair, final_repair
from robert_agent.workflows.pre_pr_review.verification import final_verification_checks
from robert_agent.workflows.pre_pr_review import decisions


class ReviewDecisionRequired(StageStopped):
    def __init__(self, path, question, *, artifact=None, stage="address_findings"):
        super().__init__("the task requires an operator decision")
        self.path, self.question = path, question
        self.artifact, self.stage = artifact, stage


class BaseRefreshRequired(StageSuperseded):
    def __init__(self, current, disposition=None):
        super().__init__("target base changed after candidate snapshot")
        self.current, self.disposition = current, disposition


class CandidateWorkspace:
    @staticmethod
    def verify_snapshot(manifest):
        return manifest.get("implementing") or workspace.verify_snapshot(manifest)


class Coordinator(ReviewStages):
    store = store
    workspace = CandidateWorkspace()
    handoff_files = {
        **ReviewStages.handoff_files,
        "verify_final": ("final-verification.json",),
        "implement": ("candidate.json",),
        "address_findings": ("dispositions.json",),
        "repair_handoff": ("handoff.json",),
    }
    artifact_kinds = (*ReviewStages.artifact_kinds, "candidate", "dispositions", "final_verification")
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
                store.validate_reply_context(self.session, reply["decision"])
            except (OSError, ValueError) as exc:
                raise StageStopped(str(exc)) from exc
        self.assigned_path = self.scope["worktree_path"]
        self.branch = self.scope["branch_name"]
        self.base = self.scope["base_branch"]
        self.root = self.data_dir / "artifacts" / "pre-pr-reviews"
        self.implementer = self.plan["implementer"]
        self.deadline = None
        self.original_request = None
        self.base_refresh = None
        self.final_feedback = self.request.get("final_feedback")
        self.final_decision_feedback = None
        self.handoff_source_run_id = None
        if reply and reply["decision"].get("stage") == "implement" and not self.manifest.get("candidate"):
            decisions.verify_implementation_source(self, reply["decision"])

    def check_active(self):
        super().check_active()
        final_repair.validate_feedback(self)
        if self.original_request and store.digest(self.original_request["path"]) != self.original_request["sha256"]:
            raise StageStopped("pinned original request changed")
        if self.deadline and datetime.now(timezone.utc) >= self.deadline:
            raise StageStopped("pre-publication review deadline expired")

    def save_manifest(self):
        with store.connect(self.db_path) as conn:
            transactions.begin(conn, immediate=True)
            if not attempt_is_active(conn, self.task_id, self.attempt_id):
                raise StageStopped("candidate owner changed")
            store.save_snapshot(conn, self.round_id, self.manifest)
            entry = store.session(conn, task_id=self.task_id)
            request = dict(entry["request"])
            checkpoint = request.get("base_merge")
            compatibility = request.get("base_compatibility")
            if compatibility and compatibility.get("snapshot_id") != self.manifest["snapshot_id"]:
                request.pop("base_compatibility")
                store.update(conn, self.round_id, request_json=json.dumps(request, sort_keys=True))
                self.request.pop("base_compatibility", None)
                self.base_refresh = None
            if checkpoint and self.manifest["base_sha"] == checkpoint["base_sha"]:
                request.pop("base_merge")
                request.pop("final_feedback", None)
                store.update(conn, self.round_id, request_json=json.dumps(request, sort_keys=True))
                self.request.update(request)
                self.request.pop("base_merge", None)
                self.request.pop("final_feedback", None)

    def check_launch_budget(self, stage, prompt_hash, previous):
        failed = [
            run
            for run in previous
            if run["status"] in {ModelRunStatus.FAILED, ModelRunStatus.INTERRUPTED}
            and run["prompt_sha256"] == prompt_hash
            and (run["dispatch"].get("pid") or run["status"] == ModelRunStatus.FAILED)
        ]
        override = self.request.get("retry_attempt_id") == self.attempt_id and not any(
            run["owner_attempt_id"] == self.attempt_id for run in failed
        )
        if len(failed) >= retry.MAX_ATTEMPTS and (stage == "repair_handoff" or not override):
            raise StageStopped(f"{stage} retry budget exhausted")

    def phase_input_changed(self, stage):
        reply = self.request.get("operator_reply", {}).get("decision", {})
        return stage != "implement" or reply.get("stage") == "implement" or super().phase_input_changed(stage)

    def preserve_phase_artifact(self, run):
        return artifact_available(run) and (
            run["stage"] in {"address_findings", "verify_final"}
            or run["stage"] == "implement"
            and run["artifact"].get("output_type") == "waiting_for_user"
        )

    def stage_environment(self, run):
        environment = super().stage_environment(run)
        if run["stage"] == "implement":
            environment["ROBERT_PRE_PR_CANDIDATE_DIR"] = run["output_dir"]
        if run["stage"] == "verify_final":
            environment["ROBERT_PRE_PR_FINAL_REVIEW_INPUT"] = self.final_review["input_path"]
        if run["stage"] == "repair_handoff":
            environment["ROBERT_PRE_PR_HANDOFF_REPAIR_INPUT"] = self.handoff_repair_input
        return environment

    def stage_worktree(self, run):
        if (run.get("dispatch") or {}).get("native_resume"):
            return super().stage_worktree(run)
        if run["stage"] == "repair_handoff":
            return self.handoff_repair_worktree
        if run["stage"] == "verify_final":
            return self.final_review["worktree_path"]
        if run["stage"] == "address_findings":
            path = self.address_workspace(run["run_id"])
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                start = self.final_feedback["final_sha"] if self.final_feedback else self.manifest["head_sha"]
                git(self.repo["repo_root"], "worktree", "add", "--detach", str(path), start)
            return str(path)
        return super().stage_worktree(run)

    def validate_output(self, run, validate, model):
        try:
            return super().validate_output(run, validate, model)
        except ValueError as exc:
            if run["stage"] not in {"implement", "address_findings"} or not handoff_repair.repairable(exc):
                raise
            with store.connect(self.db_path) as conn:
                current = next(item for item in store.runs(conn, self.round_id) if item["run_id"] == run["run_id"])
            return handoff_repair.repair(self, current, validate, model, exc)

    def address_workspace(self, run_id):
        with store.connect(self.db_path) as conn:
            runs = store.runs(conn, self.round_id)
        run = next((r for r in runs if r["run_id"] == run_id), None)
        if run and (resume := run["dispatch"].get("native_resume")):
            path = Path(resume["cwd"])
            owner = Path(self.repo["worktree_root"]) / "pre-pr-address" / self.round_id
            if path.parent != owner or run["stage"] != "address_findings":
                raise StageStopped("native address writer is outside its round")
            parent = next((r for r in runs if r["run_id"] == run["dispatch"].get("parent_run_id")), None)
            checkpoint = parent and (
                parent["dispatch"].get("result_checkpoint") or parent["dispatch"].get("launch_checkpoint")
            )
            if (
                not parent
                or parent["stage"] != "address_findings"
                or not checkpoint
                or checkpoint["worktree_path"] != str(path)
            ):
                raise StageStopped("native address source writer changed")
            return path
        return Path(self.repo["worktree_root"]) / "pre-pr-address" / self.round_id / run_id

    def validate_candidate(self, output, _model):
        path = output / "candidate.json"
        saved = json.loads(path.read_text(encoding="utf-8"))
        output_type, candidate = decisions.candidate_payload(self, saved, source_path=path)
        self.validate_submission(candidate, output_type=output_type)
        artifact = {
            "candidate_path": str(path),
            "candidate_sha256": store.digest(path),
            "head": git(self.assigned_path, "rev-parse", "HEAD"),
        }
        if output_type == "waiting_for_user":
            artifact.update(output_type=output_type, operator_question=candidate["operator_question"])
        else:
            artifact["head"] = clean_head(self.assigned_path, self.branch)
        return artifact

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
        original = Path(self.request["prompt_path"])
        if store.digest(original) != self.request["prompt_sha256"]:
            raise StageStopped("recorded implementation prompt changed")
        root = self.root / self.round_id
        root.mkdir(parents=True, exist_ok=True)
        pinned = root / f"original-request-{self.request['prompt_sha256']}.md"
        if not pinned.exists():
            pinned.write_bytes(original.read_bytes())
        self.original_request = {"path": str(pinned), "sha256": self.request["prompt_sha256"]}
        self.check_active()
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
            if candidate.get("output_type") == "waiting_for_user":
                raise ReviewDecisionRequired(
                    Path(candidate["candidate_path"]),
                    candidate["operator_question"],
                    artifact=candidate,
                    stage="implement",
                )
        self.stage = "snapshot"
        base_impact.verify_original(self)
        base_impact.verify_saved(self, self.request.get("base_compatibility"))
        with store.connect(self.db_path) as conn:
            saved_merge = store.session(conn, task_id=self.task_id)["request"].get("base_merge")
        if saved_merge:
            # Reconcile the already charged operation against its pinned base
            # before observing a newer remote revision or creating another intent.
            preserved = self.preserve_completed_repair(candidate, align=False)
            if not isinstance(saved_merge, dict) or not saved_merge.get("base_sha"):
                raise StageStopped("base merge checkpoint changed")
            if "repair_sha" in saved_merge or "repair_parent_sha" in saved_merge:
                if saved_merge.get("repair_sha") != preserved["head"] or not saved_merge.get("repair_parent_sha"):
                    raise StageStopped("base merge repair checkpoint changed")
                candidate = base_impact.reconcile_repair_merge(
                    self, candidate, preserved, saved_merge["base_sha"], saved_merge["repair_parent_sha"]
                )
            else:
                candidate = self.merge_current_base(preserved, saved_merge["base_sha"])
            self.save_candidate_snapshot(candidate, saved_merge["base_sha"])
        base_sha = self.observe_base(self.repo["repo_root"], "upstream", self.base)
        candidate = base_impact.reconcile_operator_merge(self, candidate, base_sha)
        original_matches = self.manifest.get("original_request") == self.original_request
        if self.manifest.get("base_sha") != base_sha and original_matches:
            preserved = self.preserve_completed_repair(candidate, align=False)
            if self.accept_base_refresh(base_sha, preserved["head"]):
                self.check_base(preserved["head"])
                self.stage = "prepare"
                self.prepare_context()
                return
        if self.manifest.get("base_sha") != base_sha or not original_matches:
            changed_base = bool(self.manifest.get("base_sha") and self.manifest["base_sha"] != base_sha)
            candidate = self.preserve_completed_repair(candidate, align=not changed_base)
            git(self.repo["repo_root"], "fetch", "upstream", base_sha)
            if self.manifest.get("base_sha") and self.manifest["base_sha"] != base_sha:
                candidate = self.merge_current_base(candidate, base_sha)
            if self.final_feedback:
                with store.connect(self.db_path) as conn:
                    transactions.begin(conn, immediate=True)
                    if not attempt_is_active(conn, self.task_id, self.attempt_id):
                        raise StageStopped("feedback owner changed during snapshot refresh")
                    entry = store.session(conn, task_id=self.task_id)
                    request = dict(entry["request"])
                    request.pop("final_feedback", None)
                    store.update(conn, self.round_id, request_json=json.dumps(request, sort_keys=True))
                self.request.pop("final_feedback", None)
                self.final_feedback = None
            self.save_candidate_snapshot(candidate, base_sha)
        self.check_base()
        self.stage = "prepare"
        self.prepare_context()

    def save_candidate_snapshot(self, candidate, base_sha):
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
            original_request=self.original_request,
        )
        self.manifest.update(
            candidate=candidate,
            github_account=self.repo["github_account"],
            decision_owner=self.scope.get("decision_owner"),
        )
        if not self.manifest["frozen"]:
            self.manifest["original_request"] = self.original_request
            files.atomic_json(Path(self.manifest["context_dir"]) / "manifest.json", self.manifest)
        self.save_manifest()

    def preserve_completed_repair(self, candidate, *, align=True):
        """Carry a validated repaired commit into a new review snapshot."""
        if not self.manifest.get("snapshot_id"):
            return candidate
        with store.connect(self.db_path) as conn:
            entry = store.session(conn, task_id=self.task_id)
            history = store.runs(conn, self.round_id)
        for run in reversed(history):
            if run["stage"] != "address_findings" or not artifact_available(run):
                continue
            artifact = run["artifact"]
            path = artifact.get("dispositions_path")
            if not path or store.digest(path) != artifact.get("dispositions_sha256"):
                raise StageStopped("completed repair evidence changed")
            value = json.loads(Path(path).read_text())
            if (
                value.get("candidate_sha") != self.manifest["head_sha"]
                or value.get("report_sha256") != entry["report_sha256"]
            ):
                continue
            final = artifact.get("head") or value.get("final_sha")
            if value.get("final_sha") != final or clean_head(self.address_workspace(run["run_id"])) != final:
                raise StageStopped("completed repair worktree changed")
            git(self.assigned_path, "merge-base", "--is-ancestor", candidate["head"], final)
            if not align:
                return {**candidate, "head": final}
            current = clean_head(self.assigned_path, self.branch)
            if current == candidate["head"] and current != final:
                git(self.assigned_path, "merge", "--ff-only", final)
            if clean_head(self.assigned_path, self.branch) != final:
                raise StageStopped("assigned branch changed during repair recovery")
            return {**candidate, "head": final}
        return candidate

    def merge_current_base(self, candidate, current_base):
        """Pin a merge before its effect; recover only its exact parents and tree."""
        self.check_active()
        error = check_worktree_reuse(
            self.repo["repo_root"], self.repo["worktree_root"], self.assigned_path, branch_name=self.branch
        )
        if error:
            raise StageStopped(error)
        final = candidate["head"]
        current = clean_head(self.assigned_path, self.branch)
        if (
            current in {self.manifest["head_sha"], final}
            and git(self.assigned_path, "merge-base", final, current_base) != current_base
        ):
            try:
                check_merge_environment(self.assigned_path)
            except MergePreflightError as exc:
                raise StageStopped(str(exc)) from exc
        # A validated address commit may still need its original fast-forward.
        if current == self.manifest["head_sha"] and current != final:
            git(self.assigned_path, "merge", "--ff-only", final)
            current = clean_head(self.assigned_path, self.branch)
        try:
            tree = git(self.assigned_path, "merge-tree", "--write-tree", final, current_base)
        except subprocess.CalledProcessError as exc:
            raise StageStopped(
                "current base conflicts with the preserved candidate; conflict resolution is required"
            ) from exc
        checkpoint = {
            "version": 1,
            "round_id": self.round_id,
            "snapshot_id": self.manifest["snapshot_id"],
            "previous_base_sha": self.manifest["base_sha"],
            "candidate_sha": final,
            "base_sha": current_base,
            "tree_sha": tree,
            "branch": self.branch,
            "worktree_path": str(Path(self.assigned_path).resolve()),
        }
        if current != final:
            self.verify_base_merge(current, checkpoint)
        self.check_active()
        with store.connect(self.db_path) as conn:
            transactions.begin(conn, immediate=True)
            if not attempt_is_active(conn, self.task_id, self.attempt_id):
                raise StageStopped("base merge owner changed")
            entry = store.session(conn, task_id=self.task_id)
            if entry["snapshot_id"] != checkpoint["snapshot_id"]:
                raise StageStopped("base merge snapshot changed")
            request = dict(entry["request"])
            saved = request.get("base_merge")
            if saved:
                if saved != checkpoint:
                    raise StageStopped("base merge checkpoint changed")
            else:
                if current != final and not request.get("retried_from_task"):
                    raise StageStopped("completed base merge requires an explicit retrigger")
                count = request.get("base_refresh_count", 0)
                if count >= retry.MAX_ATTEMPTS:
                    raise StageStopped("base refresh budget exhausted; keep checkpoint for a later retry")
                request.update(base_merge=checkpoint, base_refresh_count=count + 1)
                request.pop("final_feedback", None)
                store.update(conn, self.round_id, request_json=json.dumps(request, sort_keys=True), proof_json=None)
        self.request.update(request)
        self.request.pop("final_feedback", None)
        self.final_feedback = None
        self.check_active()
        if clean_head(self.assigned_path, self.branch) != current:
            raise StageStopped("candidate branch changed after base merge checkpoint")
        if current == final:
            if git(self.assigned_path, "merge-base", final, current_base) == current_base:
                return candidate
            git(self.assigned_path, "merge", "--no-ff", "--no-edit", current_base)
        merged = clean_head(self.assigned_path, self.branch)
        self.verify_base_merge(merged, checkpoint)
        return {**candidate, "head": merged}

    def verify_base_merge(self, head, checkpoint):
        parents = git(self.assigned_path, "show", "-s", "--format=%P", head).split()
        if (
            parents != [checkpoint["candidate_sha"], checkpoint["base_sha"]]
            or git(self.assigned_path, "rev-parse", head + "^{tree}") != checkpoint["tree_sha"]
        ):
            raise StageStopped("assigned branch is not the exact intended base merge")

    def check_base(self, final=None):
        base_impact.verify_original(self)
        current = self.observe_base(self.repo["repo_root"], "upstream", self.base)
        base_impact.verify_saved(self, self.request.get("base_compatibility"))
        if current != self.manifest["base_sha"]:
            if final is None:
                preserved = self.preserve_completed_repair(self.manifest["candidate"], align=False)
                final = preserved["head"]
            if self.accept_base_refresh(current, final):
                return current
            raise BaseRefreshRequired(current)
        self.base_refresh = None
        return current

    def accept_base_refresh(self, current, final):
        base_impact.verify_original(self)
        root = self.repo["repo_root"]
        reviewed = self.manifest["base_sha"]
        git(root, "fetch", "upstream", current)
        certificate = compatibility_refresh(
            root,
            reviewed,
            current,
            final,
            round_id=self.round_id,
            snapshot_id=self.manifest["snapshot_id"],
            candidate=self.manifest["head_sha"],
        )
        if not certificate:
            self.base_refresh = None
            return False
        return base_impact.save_compatibility(self, certificate)

    def refresh_base(self, changed):
        base_impact.verify_original(self)
        candidate = self.manifest["candidate"]
        preserved = self.preserve_completed_repair(candidate, align=False)
        final = changed.disposition["head"] if changed.disposition else preserved["head"]
        if self.accept_base_refresh(changed.current, final):
            return
        candidate = preserved
        if changed.disposition:
            candidate = {**candidate, "head": changed.disposition["head"]}
            current = clean_head(self.assigned_path, self.branch)
            if current == self.manifest["head_sha"] and current != candidate["head"]:
                git(self.assigned_path, "merge", "--ff-only", candidate["head"])
            if clean_head(self.assigned_path, self.branch) != candidate["head"]:
                raise StageStopped("assigned branch changed during base refresh")
        git(self.repo["repo_root"], "fetch", "upstream", changed.current)
        candidate = self.merge_current_base(candidate, changed.current)
        self.manifest = workspace.prepare_candidate_snapshot(
            self.repo["repo_root"],
            self.repo["worktree_root"],
            self.root,
            self.round_id,
            self.repo["full_name"],
            self.base,
            changed.current,
            candidate["head"],
            self.request["event"],
            original_request=self.original_request,
        )
        self.manifest.update(
            candidate=candidate,
            github_account=self.repo["github_account"],
            decision_owner=self.scope.get("decision_owner"),
        )
        files.atomic_json(Path(self.manifest["context_dir"]) / "manifest.json", self.manifest)
        self.save_manifest()

    def report(self, records):
        self.stage = "synthesis"
        self.check_active()
        evaluation_identity = {"records": records}
        load_anchor, save_anchor = ReviewStages.report_anchor_callbacks(self)
        if len(records) == 1:
            for kind in ("result", "report"):
                if files.sha256_file(records[0][kind + "_path"]) != records[0][kind + "_sha256"]:
                    raise StageStopped("selected reviewer artifact hash changed")
            result = files.read_result(records[0]["result_path"], self.manifest)
            items = result["findings"] + result["dismissed_findings"]

            def render():
                return files.render_individual(result, records[0]["worker_id"], records[0]["model"])
        else:
            inputs = files.write_synthesis_inputs(self.manifest, records)

            def completed_evaluation():
                with store.connect(self.db_path) as conn:
                    runs = store.runs(conn, self.round_id)
                return next(
                    (
                        run["artifact"]
                        for run in reversed(runs)
                        if run["stage"] == "synthesis"
                        and artifact_available(run)
                        and run["artifact"].get("input_sha256") == inputs["sha256"]
                        and run["artifact"].get("evaluation_path")
                    ),
                    None,
                )

            completed = completed_evaluation()
            if completed is None:
                self.evaluate(records)
                completed = completed_evaluation()
            if completed is None:
                raise StageStopped("completed evaluation has no registered input identity")
            evaluation_identity.update(
                evaluation_path=completed["evaluation_path"],
                evaluation_sha256=completed["evaluation_sha256"],
                input_sha256=inputs["sha256"],
            )
            if files.sha256_file(completed["evaluation_path"]) != completed["evaluation_sha256"]:
                raise StageStopped("registered review evaluation changed")
            evaluation = files.read_evaluation(completed["evaluation_path"], self.manifest, records)
            items = evaluation["items"]

            def render():
                return files.render_merged(self.manifest, records, evaluation)

        def render_with_ids():
            return render() + "\n\n## Finding IDs\n" + "\n".join(f"- `{item['id']}` {item['title']}" for item in items)

        def register(report):
            self.check_active()
            path, sha = report["path"], report["sha256"]
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
                checkpoint = report["checkpoint_path"]
                db_write_artifacts.insert_for_register(
                    conn,
                    f"{self.round_id}:report-checkpoint:{report['input_sha256']}",
                    self.task_id,
                    self.attempt_id,
                    "pre_pr_review_report_checkpoint",
                    str(checkpoint),
                    report["checkpoint_sha256"],
                    checkpoint.stat().st_size,
                    store.now(),
                )

        try:
            report = report_checkpoint.seal_rendered_report(
                self.db_path,
                self.round_id,
                self.manifest,
                evaluation_identity,
                render_with_ids,
                redact=redact_report,
                renderer_version=report_checkpoint.code_version(files.__file__, __file__),
                redaction_version=report_checkpoint.code_version(redaction.__file__),
                check_active=self.check_active,
                register=register,
                load_anchor=load_anchor,
                save_anchor=save_anchor,
            )
        except report_checkpoint.RedactionBlocked as exc:
            raise StageStopped(str(exc)) from exc
        return report["path"], report["sha256"], [item["id"] for item in items]

    def address(self, path, report_sha, ids):
        self.stage = "address_findings"
        self.phase(self.stage)
        candidate_path = self.manifest["candidate"]["candidate_path"]
        head = self.manifest["head_sha"]
        reply = self.request.get("operator_reply")
        generation = hashlib.sha256(json.dumps(reply, sort_keys=True).encode()).hexdigest() if reply else ""
        input_hash = report_sha + ("-" + generation if generation else "")
        if self.original_request or self.final_feedback:
            generation = hashlib.sha256(
                json.dumps([self.original_request, self.final_feedback], sort_keys=True).encode()
            ).hexdigest()
            input_hash += "-" + generation
        prompt = self.prompt(
            "address-" + input_hash + ".md",
            render_prompt(
                address_findings_input(
                    {**self.manifest, "original_request": self.original_request},
                    candidate_path,
                    path,
                    report_sha,
                    ids,
                    operator_reply=reply,
                    final_feedback=self.final_feedback,
                )
            ).text,
            stage="address_findings",
        )
        require_choices = question_choices.PROMPT_MARKER in prompt.read_text(encoding="utf-8")

        def validate(output, _model):
            final_repair.validate_feedback(self)
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
            pending = [item for item in value["dispositions"] if item["status"] == "needs_decision"]
            if pending:
                summary = "Review findings require a decision within the original task scope.\n" + "\n".join(
                    f"{item['id']}: {item['reason']}" for item in pending
                )
                summary = redact_and_truncate(summary, 1900).get(
                    "text", "Review findings require an operator decision."
                )
                choices = value.get("choices", [])
                question = {"kind": "scope_decision", "summary": summary, "choices": choices}
                if require_choices:
                    validation.require_answerable_question(question)
                elif error := question_choices.validation_error(choices):
                    raise ValueError(error)
                return {
                    "dispositions_path": str(disposition),
                    "dispositions_sha256": store.digest(disposition),
                    "operator_question": question,
                }
            tree = self.address_workspace(self.handoff_source_run_id or output.name)
            final = clean_head(tree)
            if final != value.get("final_sha"):
                raise ValueError("verification does not match the final worktree HEAD")
            git(tree, "merge-base", "--is-ancestor", head, final)
            if final == head and any(item["status"] == "fixed" for item in value["dispositions"]):
                raise ValueError("a finding marked fixed requires a new commit")
            draft = self.candidate_submission()
            draft["verification"] = final_verification_checks(value["verification"])
            open_pr = next(action for action in draft["planned_github_actions"] if action["type"] == "open_pr")
            open_pr["body"] = value["body"]
            self.validate_submission(draft)
            return {
                "dispositions_path": str(disposition),
                "dispositions_sha256": store.digest(disposition),
                "head": final,
            }

        artifact = self.run_phase(self.stage, self.implementer, prompt, validate, input_hash=input_hash)
        if artifact.get("operator_question"):
            raise ReviewDecisionRequired(
                Path(artifact["dispositions_path"]), artifact["operator_question"], artifact=artifact
            )
        return artifact

    def wait_for_decision(self, decision, report_sha=None):
        return decisions.submit(self, decision, report_sha)

    def candidate_submission(self):
        candidate = self.manifest["candidate"]
        if store.digest(candidate["candidate_path"]) != candidate["candidate_sha256"]:
            raise StageStopped("candidate handoff changed")
        return decisions.candidate_payload(
            self,
            json.loads(Path(candidate["candidate_path"]).read_text(encoding="utf-8")),
            source_path=Path(candidate["candidate_path"]),
        )[1]

    def validate_submission(self, submission_payload, *, output_type="new_pr"):
        with store.connect(self.db_path) as conn:
            settings, rejected = validation.context(conn, self.task_id, self.attempt_id)
        if rejected:
            raise StageStopped("candidate attempt is no longer current")
        normalized = contract.normalize({**self.envelope(submission_payload), "output_type": output_type}, settings)
        if output_type == "waiting_for_user":
            validation.require_answerable_question(submission_payload["operator_question"])
        checked = audit.audit_result(normalized, final=False, **settings)
        if not checked["ok"]:
            reason = redact_and_truncate(checked.get("safe_error", "handoff audit failed"), 1000).get(
                "text", "handoff audit failed"
            )
            raise ValueError(reason)
        return normalized

    def finalize(self, report, report_sha, disposition):
        head, final = self.manifest["head_sha"], disposition["head"]
        if (
            store.digest(report) != report_sha
            or store.digest(disposition["dispositions_path"]) != disposition["dispositions_sha256"]
        ):
            raise StageStopped("review handoff changed")
        body = report.read_text(encoding="utf-8")
        draft = self.candidate_submission()
        response = json.loads(Path(disposition["dispositions_path"]).read_text(encoding="utf-8"))
        finding_ids = [item["id"] for item in response["dispositions"]]
        verification = final_verification.verify(self, report, report_sha, finding_ids, disposition)
        self.stage = "finalize"
        self.phase(self.stage)
        draft["verification"] = response["verification"]
        if "receipt_summary" in response:
            draft["receipt_summary"] = response["receipt_summary"]
        elif final != head:
            draft.pop("receipt_summary", None)
        action = next(action for action in draft["planned_github_actions"] if action["type"] == "open_pr")
        fixed = [f"- {item['id']}：{item['reason']}" for item in response["dispositions"] if item["status"] == "fixed"]
        deferred = [
            f"- {item['id']}（{item['status']}）：{item['reason']}"
            for item in response["dispositions"]
            if item["status"] in ("deferred", "dismissed")
        ]
        load_anchor, save_anchor = ReviewStages.report_anchor_callbacks(self)
        evaluation_identity = report_checkpoint.report_input_identity(
            self.db_path,
            self.round_id,
            self.manifest,
            report,
            report_sha,
            check_active=self.check_active,
            load_anchor=load_anchor,
            redact=redact_report,
        )
        if evaluation_identity is None:
            # Historical successful reports predate filesystem checkpoints.
            # Their exact durable session registration remains the authority;
            # retain those bytes rather than deriving a new model evaluation.
            with store.connect(self.db_path) as conn:
                saved = store.session(conn, round_id=self.round_id)
            if not saved or saved["report_path"] != str(report) or saved["report_sha256"] != report_sha:
                raise StageStopped("legacy review report registration changed")
            evaluation_identity = {
                "records": [],
                "legacy_report": {
                    "path": str(report),
                    "sha256": report_sha,
                    "round_id": self.round_id,
                },
            }
        evaluation_identity = {
            **evaluation_identity,
            "public_report": {"path": str(report), "sha256": report_sha},
            "dispositions": {
                "path": disposition["dispositions_path"],
                "sha256": disposition["dispositions_sha256"],
            },
            "candidate_sha": head,
            "final_sha": final,
        }

        def render_appendix():
            return "\n".join(
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

        def register_appendix(appendix):
            self.check_active()
            with store.connect(self.db_path) as conn:
                transactions.begin(conn, immediate=True)
                if not attempt_is_active(conn, self.task_id, self.attempt_id):
                    raise StageStopped("review owner changed before appendix registration")
                for kind, path, sha in (
                    ("appendix", appendix["path"], appendix["sha256"]),
                    ("checkpoint", appendix["checkpoint_path"], appendix["checkpoint_sha256"]),
                ):
                    db_write_artifacts.insert_for_register(
                        conn,
                        f"{self.round_id}:review-{kind}:{appendix['input_sha256']}",
                        self.task_id,
                        self.attempt_id,
                        f"pre_pr_review_{kind}",
                        str(path),
                        sha,
                        path.stat().st_size,
                        store.now(),
                    )

        try:
            appendix = report_checkpoint.seal_rendered_report(
                self.db_path,
                self.round_id,
                self.manifest,
                evaluation_identity,
                render_appendix,
                redact=redact_report,
                renderer_version=report_checkpoint.code_version(files.__file__, __file__),
                redaction_version=report_checkpoint.code_version(redaction.__file__),
                check_active=self.check_active,
                register=register_appendix,
                load_anchor=load_anchor,
                save_anchor=save_anchor,
            )
        except report_checkpoint.RedactionBlocked as exc:
            raise StageStopped("review dispositions did not pass public content redaction") from exc
        action["body"] = response["body"] + "\n\n" + appendix["path"].read_text(encoding="utf-8")
        normalized = self.validate_submission(draft)
        normalized_pr = next(action for action in normalized["planned_github_actions"] if action["type"] == "open_pr")
        envelope = self.envelope(draft)
        fingerprint = contract.semantic_fingerprint(envelope)
        try:
            publication_base = self.check_base(final)
        except BaseRefreshRequired as exc:
            exc.disposition = disposition
            raise
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
                "final_verification": verification,
                "round_id": self.round_id,
                "attempt_id": self.attempt_id,
                "snapshot_id": self.manifest["snapshot_id"],
                "candidate_sha": head,
                "finding_ids": finding_ids,
                "head_sha": final,
                "base_ref": self.base,
                "base_sha": publication_base,
                "reviewed_base_sha": self.manifest["base_sha"],
                **({"base_refresh": self.base_refresh} if self.base_refresh else {}),
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
                    else now + timedelta(seconds=pipeline_timeout(self.plan))
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
                try:
                    outcome = self.advance_stages()
                    if outcome is not None:
                        return outcome
                except BaseRefreshRequired as exc:
                    self.refresh_base(exc)
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

    def advance_stages(self):
        """Keep question handoffs inside the same base/ownership recovery boundary."""
        try:
            self.reconcile_running()
            self.prepare()
            self.stage = "review"
            records = self.review()
            report, sha, ids = self.report(records)
            disposition = self.address(report, sha, ids)
            return self.finalize(report, sha, disposition)
        except ReviewDecisionRequired as exc:
            return self.wait_for_decision(exc, locals().get("sha"))
        except final_verification.FinalReviewBlocked as exc:
            decision = final_repair.handle(self, exc)
            if decision:
                return self.wait_for_decision(ReviewDecisionRequired(*decision), sha)
            return None


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
