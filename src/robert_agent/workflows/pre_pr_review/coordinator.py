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
from robert_agent.context.native_prompts import address_findings_input, candidate_verification_input
from robert_agent.context.prompts import render_prompt
from robert_agent.delivery import contract
from robert_agent.delivery.base_revisions import compatibility_refresh
from robert_agent.delivery.application import submission, validation, audit, review_publication
from robert_agent.delivery.lock import task_publication_lock
from robert_agent.execution import retry, stage_evidence
from robert_agent.execution.budgets import pipeline_timeout
from robert_agent.execution.stages import StageStopped, StageSuperseded, attempt_is_active, terminate_children
from robert_agent.foundation import question_choices
from robert_agent.foundation.contracts.execution import ModelRunStatus, ReviewReportMode
from robert_agent.paths import default_config_path
from robert_agent.persistence import transactions
from robert_agent.persistence.stores import artifacts as db_write_artifacts
from robert_agent.persistence.rounds import pre_pr_review as store
from robert_agent.persistence.rounds.records import artifact_available, completed_stage_artifact
from robert_agent.security.redaction import redact_and_truncate, redact_report
from robert_agent.security import redaction
from robert_agent.workflows.review import files, workspace, report_checkpoint
from robert_agent.workflows.review.coordinator import ReviewStages
from robert_agent.workflows.pre_pr_review import base_impact, final_verification, handoff_repair, final_repair
from robert_agent.workflows.pre_pr_review.verification import final_verification_checks
from robert_agent.workflows.pre_pr_review import decisions, single_round, targeted, verification_retry


def _append_finding_ids(body, items):
    return body + "\n\n## Finding IDs\n" + "\n".join(f"- `{item['id']}` {item['title']}" for item in items)


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
        "validate_candidate": ("candidate-verification.json",),
        "address_findings": ("dispositions.json",),
        "repair_handoff": ("handoff.json",),
    }
    artifact_kinds = (
        *ReviewStages.artifact_kinds,
        "candidate",
        "candidate_verification",
        "dispositions",
        "final_verification",
    )
    stage = "implement"

    def __init__(self, db_path, task_id, attempt_id, *, observe_base=remote_branch_sha, config_path=None, **kwargs):
        super().__init__(db_path, task_id, attempt_id, **kwargs)
        self.plan = self.request["plan"]
        if self.plan.get("review_policy") not in (None, single_round.POLICY):
            raise StageStopped("unsupported pre-publication review policy")
        self.request = {**self.request, "workers": self.plan["workers"]}
        self.repo = self.request["repo"]
        self.observe_base = observe_base
        self.publication_runner = None
        self.publication_config = config_path or default_config_path()
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
        self.verification_retry = None
        self.address_reply = None
        if reply and review_publication.enabled(self.plan) and reply["decision"].get("stage") == "address_findings":
            self.address_reply = reply["decision"]
        if reply and reply["decision"].get("stage") == "implement" and not self.manifest.get("candidate"):
            decisions.verify_implementation_source(self, reply["decision"])

    def check_active(self):
        super().check_active()
        final_repair.validate_feedback(self)
        verification_retry.verify(self.verification_retry)
        if self.original_request and store.digest(self.original_request["path"]) != self.original_request["sha256"]:
            raise StageStopped("pinned original request changed")
        if self.deadline and datetime.now(timezone.utc) >= self.deadline:
            raise StageStopped("pre-publication review deadline expired")

    def save_manifest(self):
        with store.connect(self.db_path) as conn:
            transactions.begin(conn, immediate=True)
            if not attempt_is_active(conn, self.task_id, self.attempt_id):
                raise StageStopped("candidate owner changed")
            entry = store.session(conn, task_id=self.task_id)
            if (
                single_round.enabled(self)
                and entry["snapshot_id"]
                and entry["snapshot_id"] != self.manifest["snapshot_id"]
                and any(run["stage"] in single_round.PHASES for run in store.runs(conn, self.round_id))
            ):
                raise StageStopped("single-round review snapshot changed; preserve the existing checkpoint")
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
        if single_round.enabled(self) and stage in single_round.PHASES:
            if any(completed_stage_artifact(run) for run in previous):
                raise StageStopped("completed single-round " + stage + " must be reused; no second invocation")
        recovering_required_checks = (
            stage == "address_findings"
            and single_round.enabled(self)
            and (self.verification_retry or self.address_reply)
        )
        failed = [
            run
            for run in previous
            if (
                run["status"] in {ModelRunStatus.FAILED, ModelRunStatus.INTERRUPTED}
                and (run["dispatch"].get("pid") or run["status"] == ModelRunStatus.FAILED)
                or self.address_reply
                and stage == "address_findings"
                and run["artifact"].get("operator_question")
            )
            and (run["prompt_sha256"] == prompt_hash or recovering_required_checks)
        ]
        override = self.request.get("retry_attempt_id") == self.attempt_id and not any(
            run["owner_attempt_id"] == self.attempt_id for run in failed
        )
        if len(failed) >= retry.MAX_ATTEMPTS and (stage == "repair_handoff" or not override):
            raise StageStopped(f"{stage} retry budget exhausted")

    def phase_input_changed(self, stage, run):
        if single_round.enabled(self) and stage in single_round.PHASES:
            return (
                stage == "address_findings"
                and not run["slot_reserved"]
                and (
                    self.verification_retry
                    and run["status"] in {ModelRunStatus.FAILED, ModelRunStatus.INTERRUPTED}
                    or self.address_reply
                    and run["artifact"].get("operator_question")
                )
            )
        reply = self.request.get("operator_reply", {}).get("decision", {})
        return stage != "implement" or reply.get("stage") == "implement" or super().phase_input_changed(stage, run)

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
        if run["stage"] == "validate_candidate":
            return self.assigned_path
        if run["stage"] == "repair_handoff":
            return self.handoff_repair_worktree
        if run["stage"] == "verify_final":
            return self.final_review["worktree_path"]
        if run["stage"] == "address_findings":
            path = self.address_workspace(run["run_id"])
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                if self.address_reply:
                    decisions.verify_address_source(self, self.address_reply)
                start = (
                    self.verification_retry["final_sha"]
                    if self.verification_retry
                    else self.final_feedback["final_sha"]
                    if self.final_feedback
                    else self.address_reply["source_checkpoint"]["worktree_head"]
                    if self.address_reply
                    else self.manifest["head_sha"]
                )
                git(self.repo["repo_root"], "worktree", "add", "--detach", str(path), start)
            return str(path)
        return super().stage_worktree(run)

    def validate_output(self, run, validate, model):
        if single_round.enabled(self) and artifact_available(run) and run["artifact"].get("handoff_repair_path"):
            return handoff_repair.reuse(self, run, validate, model)
        try:
            return super().validate_output(run, validate, model)
        except (ValueError, OSError, RuntimeError) as exc:
            if single_round.enabled(self) and artifact_available(run):
                raise StageStopped("completed single-round artifact changed: " + str(exc)) from exc
            if run["stage"] not in {"implement", "address_findings"} or not handoff_repair.repairable(exc):
                raise
            with store.connect(self.db_path) as conn:
                current = next(item for item in store.runs(conn, self.round_id) if item["run_id"] == run["run_id"])
            return handoff_repair.repair(self, current, validate, model, exc)

    def run_phase(self, stage, worker, prompt_path, validate, *, input_hash=None):
        if single_round.enabled(self) and stage in single_round.PHASES:
            with store.connect(self.db_path) as conn:
                completed = [
                    run
                    for run in store.runs(conn, self.round_id)
                    if run["stage"] == stage and run["worker_id"] == worker["name"] and completed_stage_artifact(run)
                ]
            if len(completed) > 1:
                raise StageStopped("single-round phase has multiple completed artifacts")
            for run in completed:
                if run["prompt_sha256"] != store.digest(prompt_path) or (
                    input_hash is not None and run["artifact"].get("input_sha256") != input_hash
                ):
                    raise StageStopped("completed single-round " + stage + " input changed; preserve its artifact")
        return super().run_phase(stage, worker, prompt_path, validate, input_hash=input_hash)

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
            single_round.preserve_snapshot(self)
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
        if not single_round.review_started(self):
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
            single_round.preserve_snapshot(self)
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
        single_round.preserve_snapshot(self)
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
            review_report_mode=self.plan.get("review_report_mode"),
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
        single_round.preserve_snapshot(self)
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
            review_report_mode=self.plan.get("review_report_mode"),
        )
        self.manifest.update(
            candidate=candidate,
            github_account=self.repo["github_account"],
            decision_owner=self.scope.get("decision_owner"),
        )
        files.atomic_json(Path(self.manifest["context_dir"]) / "manifest.json", self.manifest)
        self.save_manifest()

    def report(self, records):
        deterministic = self.manifest.get("review_report_mode") == ReviewReportMode.DETERMINISTIC_V1
        self.stage = "report" if deterministic else "synthesis"
        self.check_active()
        evaluation_identity = {"records": records}
        requires_followup = True
        load_anchor, save_anchor = ReviewStages.report_anchor_callbacks(self)
        if deterministic:
            merged = files.merge_reviewer_results(self.manifest, records)
            items = merged["items"]
            requires_followup = merged["requires_followup"]
            evaluation_identity.update(files.neutral_report_identity(merged))

            def render_with_ids():
                return files.render_neutral_report(self.manifest, merged)
        elif len(records) == 1:
            for kind in ("result", "report"):
                if files.sha256_file(records[0][kind + "_path"]) != records[0][kind + "_sha256"]:
                    raise StageStopped("selected reviewer artifact hash changed")
            result = files.read_result(records[0]["result_path"], self.manifest)
            items = result["findings"] + result["dismissed_findings"]

            def render_with_ids():
                body = files.render_individual(result, records[0]["worker_id"], records[0]["model"])
                return _append_finding_ids(body, items)
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

            def render_with_ids():
                body = files.render_merged(self.manifest, records, evaluation)
                return _append_finding_ids(body, items)

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

        if single_round.enabled(self):
            with store.connect(self.db_path) as conn:
                saved = store.session(conn, task_id=self.task_id)
            if saved["report_path"]:
                saved_path = Path(saved["report_path"])
                if store.digest(saved_path) != saved["report_sha256"]:
                    raise StageStopped("registered single-round report changed")
                try:
                    pinned = report_checkpoint.report_input_identity(
                        self.db_path,
                        self.round_id,
                        self.manifest,
                        saved_path,
                        saved["report_sha256"],
                        check_active=self.check_active,
                        load_anchor=load_anchor,
                        redact=redact_report,
                    )
                except (ValueError, OSError) as exc:
                    raise StageStopped("registered single-round report changed: " + str(exc)) from exc
                if pinned != evaluation_identity:
                    raise StageStopped("registered single-round report input changed")
                return saved_path, saved["report_sha256"], [item["id"] for item in items], requires_followup
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
        return report["path"], report["sha256"], [item["id"] for item in items], requires_followup

    def no_repair_disposition(self, report_sha):
        """Record zero finding dispositions when the original verification still applies."""
        reply = self.request.get("operator_reply")
        if self.final_feedback or (reply and reply["decision"].get("stage") != "implement"):
            return None
        head = self.manifest["head_sha"]
        candidate = self.manifest["candidate"]
        with store.connect(self.db_path) as conn:
            history = store.runs(conn, self.round_id)
            publication = store.session(conn, round_id=self.round_id)["request"].get("review_publication") or {}
        verification_id = (publication.get("candidate_evidence") or {}).get("validation_run_id")
        # Once verification was needed, keep its registered stage and evidence
        # across retries even if the original implementation inputs match again.
        if any(run["stage"] == "address_findings" for run in history):
            return None
        implementation = next(
            (run for run in history if run["run_id"] == (verification_id or candidate["run_id"])), None
        )
        if verification_id:
            verification = store.candidate_validation(implementation, self.manifest)
            if verification is None:
                return None
        else:
            if (
                not implementation
                or implementation["stage"] != "implement"
                or not artifact_available(implementation)
                or any(
                    implementation["artifact"].get(key) != candidate[key]
                    for key in ("candidate_path", "candidate_sha256")
                )
                or implementation["artifact"].get("head") != head
            ):
                return None
            verification = None
        checkpoint = implementation.get("dispatch", {}).get("result_checkpoint", {})
        if "untracked_sha256" not in checkpoint or checkpoint.get("source_pin_error"):
            return None
        implementation_source = (
            self.manifest
            if verification_id
            else {
                "implementing": True,
                **{key: checkpoint.get(key) for key in ("snapshot_id", "head_sha", "base_sha")},
            }
        )
        try:
            current = stage_evidence.observe_source(
                implementation, implementation_source, Path(self.assigned_path).resolve()
            )
        except (OSError, ValueError, subprocess.CalledProcessError):
            return None
        # Registered worktrees can move during a rebind without changing the
        # verified commit, recorded inputs or local configuration.
        if current != {**checkpoint, "worktree_path": current["worktree_path"]}:
            return None
        draft = self.candidate_submission()
        action = next(action for action in draft["planned_github_actions"] if action["type"] == "open_pr")
        evidence = {
            "source": "candidate_revalidation" if verification_id else "implementation",
            "source_checkpoint_sha256": hashlib.sha256(json.dumps(checkpoint, sort_keys=True).encode()).hexdigest(),
            "candidate_sha": head,
            "final_sha": head,
            "report_sha256": report_sha,
            "dispositions": [],
            "verification": final_verification_checks(verification if verification_id else draft["verification"]),
            "body": action["body"],
        }
        root = Path(self.manifest["artifact_root"])
        path = files.safe_path(root / f"no-repair-{report_sha}.json", root)
        text = json.dumps(evidence, sort_keys=True)
        self.check_active()
        if not path.exists():
            files.atomic_text(path, text)
        digest = hashlib.sha256(text.encode()).hexdigest()
        if store.digest(path) != digest:
            raise StageStopped("registered no-repair disposition changed")
        return {"head": head, "dispositions_path": str(path), "dispositions_sha256": digest}

    def address(self, path, report_sha, ids):
        self.stage = "address_findings"
        self.phase(self.stage)
        candidate_path = self.manifest["candidate"]["candidate_path"]
        head = self.manifest["head_sha"]
        reply = self.request.get("operator_reply")
        generation = hashlib.sha256(json.dumps(reply, sort_keys=True).encode()).hexdigest() if reply else ""
        input_hash = report_sha + ("-" + generation if generation else "")
        self.verification_report_sha = report_sha
        self.verification_retry = verification_retry.inputs(self)
        if self.verification_retry:
            input_hash += "-" + self.verification_retry["input_sha256"]
        if self.original_request or self.final_feedback:
            generation = hashlib.sha256(
                json.dumps([self.original_request, self.final_feedback], sort_keys=True).encode()
            ).hexdigest()
            input_hash += "-" + generation
        if self.verification_retry:
            input_hash = report_sha + "-" + hashlib.sha256(input_hash.encode()).hexdigest()
        stage_version = None
        if single_round.enabled(self):
            stage_version = 3 if review_publication.enabled(self.plan) else 2
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
                    verification_retry=self.verification_retry,
                ),
                stage_version=stage_version,
            ).text,
            stage="address_findings",
        )
        pr_first = review_publication.enabled(self.plan)
        require_choices = pr_first or question_choices.PROMPT_MARKER in prompt.read_text(encoding="utf-8")

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
            if pending and (pr_first or not single_round.enabled(self)):
                if pr_first:
                    tree = self.address_workspace(self.handoff_source_run_id or output.name)
                    partial = clean_head(tree)
                    if partial != value.get("final_sha"):
                        raise ValueError("question worktree differs from the reported repair SHA")
                    git(tree, "merge-base", "--is-ancestor", head, partial)
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
            open_pr["body"] = self.public_body(value["body"])
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
        if decision.stage == "address_findings" and review_publication.enabled(getattr(self, "plan", {})):
            head = self.manifest["head_sha"]
            self.publication().waiting(
                head,
                f"## 第一轮处理待决定\n\n送审提交 `{head}`。等待请求归属人的范围确认，"
                "答复后继续原修复和验证。本草稿保留已发布的送审提交与评审报告。",
            )
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

    @staticmethod
    def public_body(body):
        safe = redaction.redact_text(body)
        if not safe.get("ok"):
            raise StageStopped("review PR body did not pass public content redaction")
        return safe["text"]

    def single_round_disclosure(self, disposition, targeted_review=None):
        if targeted_review:
            return "评审策略：一轮 review、一轮处理，已通过定向复核。触发原因：" + ", ".join(targeted_review["reasons"])
        if disposition.get("run_id") is None:
            return "评审策略：一轮 review。实施验证适用于送审提交，本轮无需整改。"
        return "评审策略：一轮 review、一轮整改；未执行独立的最终模型复核。"

    def finalize(self, report, report_sha, disposition, *, risk_reasons=()):
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
        targeted_review = None
        if single_round.enabled(self):
            if (
                response.get("candidate_sha") != head
                or response.get("final_sha") != final
                or response.get("report_sha256") != report_sha
            ):
                raise StageStopped("single-round final handoff identity changed")
            final_verification_checks(response["verification"])
            completion = single_round.completion(self, report_sha, disposition)
            if risk_reasons:
                artifact = final_verification.verify(
                    self, report, report_sha, finding_ids, disposition, reasons=risk_reasons
                )
                targeted_review = {"reasons": list(risk_reasons), "artifact": artifact}
                completion["targeted_verification"] = targeted_review
            verification = None
        else:
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
            if item["status"] != "fixed"
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
                    *(
                        [self.single_round_disclosure(disposition, targeted_review)]
                        if single_round.enabled(self)
                        else []
                    ),
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
        action["body"] = self.public_body(response["body"]) + "\n\n" + appendix["path"].read_text(encoding="utf-8")
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
                **(
                    {"review_policy": single_round.POLICY, "single_round_completion": completion}
                    if single_round.enabled(self)
                    else {"final_verification": verification}
                ),
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
            if review_publication.enabled(getattr(self, "plan", {})):
                with store.connect(self.db_path) as conn:
                    entry = store.session(conn, round_id=self.round_id)
                if not store.matches_review_completion(entry, proof):
                    raise StageStopped("review completion proof did not match the registered stages")
                self.stage = "publishing_review_completion"
                self.phase(self.stage)
                summary = "\n".join(
                    [
                        "## 第一轮处理结果",
                        "",
                        f"送审提交 `{head}`，最终提交 `{final}`。",
                        self.single_round_disclosure(disposition, targeted_review),
                        "",
                        "### 已修复",
                        *(fixed or ["无。"]),
                        "",
                        "### 未修复及原因",
                        *(deferred or ["无。"]),
                        "",
                        "### 验证",
                        *(f"- {check['status']} · {check['purpose']}" for check in normalized["verification"]),
                    ]
                )
                proof["publication"] = self.publication().finish_locked(
                    normalized,
                    final,
                    proof,
                    report_sha=report_sha,
                    dispositions_sha=disposition["dispositions_sha256"],
                    summary=summary,
                )
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
                transient = isinstance(exc, review_publication.TransientPublicationError)
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
                        fields = {}
                        if transient:
                            entry = store.session(conn, round_id=self.round_id)
                            request = entry["request"]
                            previous = request.get("publication_retry") or {}
                            stages = previous["stages"] if previous.get("attempt_id") == self.attempt_id else {}
                            count = stages.get(self.stage, 0) + 1
                            delay = 5 * 2 ** (min(count, retry.MAX_ATTEMPTS) - 1)
                            next_retry = datetime.now(timezone.utc) + timedelta(seconds=delay)
                            can_retry = (
                                count < retry.MAX_ATTEMPTS and stopped != "attention" and next_retry < self.deadline
                            )
                            request["publication_retry"] = {
                                "attempt_id": self.attempt_id,
                                "stages": {**stages, self.stage: count},
                            }
                            fields["request_json"] = json.dumps(request, sort_keys=True)
                        store.update(
                            conn,
                            self.round_id,
                            status="retry_wait" if can_retry else "attention",
                            next_retry_at=next_retry.isoformat() if can_retry else None,
                            error=f"{self.stage}: {error}",
                            **fields,
                        )
                    else:
                        can_retry = False
                if not can_retry:
                    raise
                if self.manifest.get("implementing"):
                    self.manifest = {}
                self.stopping.wait(delay)
                self.check_active()

    def publication(self):
        return review_publication.Publication(
            self.db_path,
            self.round_id,
            self.task_id,
            self.attempt_id,
            check_active=self.check_active,
            config_path=self.publication_config,
            runner=self.publication_runner,
        )

    def revalidate_candidate(self, draft):
        self.stage = "validate_candidate"
        self.phase(self.stage)
        manifest = self.manifest
        head, base = manifest["head_sha"], manifest["base_sha"]
        prompt = self.prompt(
            f"verify-candidate-{manifest['snapshot_id']}.md",
            render_prompt(candidate_verification_input(manifest, manifest["candidate"])).text,
            stage=self.stage,
        )

        def validate(output, _model):
            path = output / "candidate-verification.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            if (
                value.get("head_sha") != head
                or value.get("base_sha") != base
                or clean_head(self.assigned_path, self.branch) != head
            ):
                raise StageStopped("candidate verification is outside the pinned revision")
            checks = final_verification_checks(value["verification"])
            original_required = {item["command"] for item in draft["verification"] if item.get("required")}
            if not original_required.issubset(item["command"] for item in checks if item.get("required")):
                raise ValueError("candidate verification omitted a previously required check")
            self.validate_submission({**draft, "verification": checks})
            return {
                "candidate_verification_path": str(path),
                "candidate_verification_sha256": store.digest(path),
                "head": head,
                "base_sha": base,
            }

        return self.run_phase(self.stage, self.implementer, prompt, validate)["run_id"]

    def preliminary_publication(self):
        draft = self.candidate_submission()
        manifest = self.manifest
        candidate = manifest["candidate"]
        with store.connect(self.db_path) as conn:
            entry = store.session(conn, round_id=self.round_id)
            history = store.runs(conn, self.round_id)
        implementation = next((run for run in history if run["run_id"] == candidate["run_id"]), None)
        if (
            not implementation
            or implementation["stage"] != "implement"
            or implementation["slot_reserved"]
            or not artifact_available(implementation)
            or implementation["artifact"].get("candidate_sha256") != candidate["candidate_sha256"]
            or implementation["dispatch"].get("result_checkpoint", {}).get("worktree_head")
            != implementation["artifact"].get("head")
        ):
            raise StageStopped("preliminary publication requires registered implementation evidence")
        candidate_evidence = {key: candidate[key] for key in ("candidate_sha256", "run_id")}
        state = entry["request"].get("review_publication") or {}
        if implementation["artifact"].get("head") != manifest["head_sha"]:
            # Recover the exact original gate after the Draft is created, including
            # when the assigned branch already contains the final repair.
            validation_id = (state.get("candidate_evidence") or {}).get("validation_run_id")
            if not state:
                validation_id = self.revalidate_candidate(draft)
                with store.connect(self.db_path) as conn:
                    history = store.runs(conn, self.round_id)
            validation_run = next((run for run in history if run["run_id"] == validation_id), None)
            verification = store.candidate_validation(validation_run, manifest)
            if verification is None:
                raise StageStopped("preliminary publication requires verification for the refreshed SHA")
            draft["verification"] = verification
            candidate_evidence["validation_run_id"] = validation_id

        final_verification_checks(draft["verification"])
        if not state:
            self.check_base(manifest["head_sha"])
        self.stage = "preliminary_publication"
        self.phase(self.stage)
        action = next(action for action in draft["planned_github_actions"] if action["type"] == "open_pr")
        action["body"] += "\n\n本 PR 等待第一轮评审。评审报告和后续处置见评论。"
        if "issue_resolution" in action:
            action.update(issue_resolution="partial", remaining_scope="第一轮评审及必要整改")
        return self.publication().preliminary(
            self.validate_submission(draft),
            manifest["snapshot_id"],
            manifest["head_sha"],
            manifest["base_sha"],
            candidate_evidence=candidate_evidence,
            base_refresh=self.base_refresh,
        )

    def advance_stages(self):
        """Keep question handoffs inside the same base/ownership recovery boundary."""
        pr_first = review_publication.enabled(getattr(self, "plan", {}))
        try:
            self.reconcile_running()
            self.prepare()
            if pr_first:
                self.preliminary_publication()
            self.stage = "review"
            records = self.review()
            report, sha, ids, requires_followup = self.report(records)
            if pr_first:
                self.stage = "publishing_review_report"
                self.phase(self.stage)
                self.publication().report(self.manifest["head_sha"], sha, report.read_text(encoding="utf-8"))
            disposition = None if requires_followup else self.no_repair_disposition(sha)
            if disposition is None:
                disposition = self.address(report, sha, ids)
            if pr_first:
                reasons = targeted.reasons(self, records, disposition)
                return self.finalize(report, sha, disposition, risk_reasons=reasons)
            return self.finalize(report, sha, disposition)
        except ReviewDecisionRequired as exc:
            return self.wait_for_decision(exc, locals().get("sha"))
        except final_verification.FinalReviewBlocked as exc:
            if pr_first:
                head = self.manifest["head_sha"]
                summary = "\n".join(
                    ["## 第一轮处理待决定", "", f"送审提交 `{head}`，定向复核未通过。"]
                    + [f"- {item}" for item in exc.value["blocking_findings"]]
                )
                self.publication().blocked(head, summary)
                raise StageStopped("targeted final review blocked the update; draft and evidence retained") from exc
            if single_round.enabled(self):
                raise StageStopped("single-round policy cannot enter final model review or repair") from exc
            decision = final_repair.handle(self, exc)
            if decision:
                return self.wait_for_decision(ReviewDecisionRequired(*decision), sha)
            return None


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--config")
    args = parser.parse_args(argv)
    coordinator = Coordinator(args.db, args.task_id, args.attempt_id, config_path=args.config)
    signal.signal(signal.SIGTERM, lambda *_args: coordinator.stopping.set())
    coordinator.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
