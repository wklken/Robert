"""Native PR review stages using the shared supervised model executor."""

from robert_agent.foundation.contracts.delivery import GitHubActionType, OutputType
from robert_agent.foundation.contracts.execution import RoundStatus
from robert_agent.persistence.rounds.review_stop import encode_round_stop

from robert_agent.persistence.stores import artifacts as db_write_artifacts
from robert_agent.persistence.stores import attempts as db_write_attempts
from robert_agent.persistence.queries import artifacts as db_read_artifacts
from robert_agent.persistence import transactions
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import signal

from robert_agent.security import redaction
from robert_agent.context.native_prompts import review_prepare_input, review_synthesize_input
from robert_agent.context.prompts import render_prompt
from robert_agent.delivery.application import validation as result_validation
from robert_agent.workflows.review import files as review_files
from robert_agent.workflows.review import workspace as review_workspace
from robert_agent.workflows.review import preparation as review_preparation
from robert_agent.workflows.review import report_checkpoint
from robert_agent.persistence.rounds import review as store
from robert_agent.adapters.github.pull_requests import read_pr
from robert_agent.execution.stages import (
    StageStopped,
    StageSuperseded,
    StageCoordinator,
    attempt_is_active,
    terminate_children,
)
from robert_agent.execution.rounds import SLOT_STORES
from robert_agent.delivery.application import submission as worker_result
from robert_agent.work import decision_owner
from robert_agent.foundation import review_recipient
from robert_agent.work import review_identity


def same_revision(manifest, pr):
    return (
        pr.get("state") == "open"
        and pr.get("head", {}).get("sha") == manifest["head_sha"]
        and pr.get("base", {}).get("sha") == manifest["base_sha"]
        and pr.get("base", {}).get("ref") == manifest["base_ref"]
    )


class ReviewSubmissionRejected(RuntimeError):
    def __init__(self, diagnostic):
        self.diagnostic = diagnostic
        super().__init__(
            f"review result was rejected: {diagnostic['reason_code']} "
            f"at {diagnostic['field_path']}: {diagnostic['safe_error']}"
        )


class ReviewStages(StageCoordinator):
    artifact_files = review_files
    slot_stores = SLOT_STORES
    voter_stage = "review"
    handoff_files = {
        "prepare": ("context-summary.md", "source-index.json"),
        "review": ("result.json",),
        "synthesis": ("evaluation.json",),
    }
    artifact_kinds = ("result", "report", "evaluation", "context_summary", "source_index")
    workflow_name = "review"

    def report_anchor_callbacks(self):
        """Persist a report's trusted seal before its independent DB registration."""

        def load(input_sha):
            artifact_id = f"{self.round_id}:report-seal-anchor:{input_sha}"
            with self.store.connect(self.db_path) as conn:
                anchor = db_read_artifacts.report_checkpoint_anchor(conn, artifact_id)
            if anchor is None:
                return None
            expected_path = (
                Path(self.manifest["artifact_root"]).resolve() / "report-checkpoints" / input_sha / "seal.json"
            )
            if anchor["artifact_type"] != "review_report_seal_anchor" or anchor["path"] != str(expected_path):
                raise StageStopped("trusted report seal registration changed")
            return {"path": anchor["path"], "sha256": anchor["sha256"]}

        def save(report):
            self.check_active()
            checkpoint = report["checkpoint_path"]
            with self.store.connect(self.db_path) as conn:
                transactions.begin(conn, immediate=True)
                if not attempt_is_active(conn, self.task_id, self.attempt_id):
                    raise StageStopped("review owner changed before seal registration")
                if (
                    review_files.sha256_file(checkpoint) != report["checkpoint_sha256"]
                    or review_files.sha256_file(report["path"]) != report["sha256"]
                ):
                    raise StageStopped("report seal changed before trusted registration")
                db_write_artifacts.insert_for_register(
                    conn,
                    f"{self.round_id}:report-seal-anchor:{report['input_sha256']}",
                    self.task_id,
                    self.attempt_id,
                    "review_report_seal_anchor",
                    str(checkpoint),
                    report["checkpoint_sha256"],
                    checkpoint.stat().st_size,
                    self.store.now(),
                )
            if load(report["input_sha256"]) != {"path": str(checkpoint), "sha256": report["checkpoint_sha256"]}:
                raise StageStopped("trusted report seal registration changed")

        return load, save

    def prepare_context(self):
        review_workspace.verify_snapshot(self.manifest)
        if not self.manifest.get("frozen"):
            self.check_active()
            saved = review_files._read_json(Path(self.manifest["context_dir"]) / "manifest.json")
            if saved.get("frozen"):
                # Recover a freeze committed before its database registration.
                self.manifest = review_workspace.freeze_context(self.manifest)
                self.save_manifest()
                return
            with self.store.connect(self.db_path) as conn:
                legacy_prepare = any(run["stage"] == "prepare" for run in self.store.runs(conn, self.round_id))
            if not legacy_prepare:
                review_preparation.write_context(self.manifest)
                self.check_active()
                self.manifest = review_workspace.freeze_context(self.manifest)
                self.save_manifest()
                return
            prompt = self.prompt(
                "prepare-prompt.md", render_prompt(review_prepare_input(self.manifest)).text, stage="prepare"
            )

            def validate(output, model):
                summary = review_files.safe_path(output / "context-summary.md", self.manifest["artifact_root"])
                index = review_files.safe_path(output / "source-index.json", self.manifest["artifact_root"])
                if not summary.read_text(encoding="utf-8").strip():
                    raise ValueError("primary context summary is empty")
                source_index = json.loads(index.read_text(encoding="utf-8"))
                if (
                    not isinstance(source_index, dict)
                    or not isinstance(source_index.get("files"), list)
                    or not isinstance(source_index.get("reading_notes"), str)
                    or not source_index["reading_notes"].strip()
                ):
                    raise ValueError("invalid primary source index")
                tree = Path(self.manifest["worktree_path"])
                for item in source_index["files"]:
                    if (
                        not isinstance(item, dict)
                        or not isinstance(item.get("reason"), str)
                        or not item["reason"].strip()
                    ):
                        raise ValueError("source index entry requires a reason")
                    name = review_files._source_path(item.get("path"))
                    if not review_files.safe_path(tree / name, tree).is_file():
                        raise ValueError("source index entry references missing source")
                return {
                    "context_summary_path": str(summary),
                    "context_summary_sha256": store.digest(summary),
                    "source_index_path": str(index),
                    "source_index_sha256": store.digest(index),
                }

            result = self.run_phase("prepare", self.request["workers"][0], prompt, validate)
            for kind, name in [("context_summary", "context-summary.md"), ("source_index", "source-index.json")]:
                shutil.copyfile(result[kind + "_path"], Path(self.manifest["context_dir"]) / name)
            self.manifest = review_workspace.freeze_context(self.manifest)
            self.save_manifest()

    def review(self):
        self.phase(RoundStatus.REVIEWING.value)

        def review_one(worker):
            def validate(output, model, worker=worker):
                result_path = output / "result.json"
                result = review_files.read_result(result_path, self.manifest)
                report_path = review_files.safe_path(output / "report.md", self.manifest["artifact_root"])
                report = review_files.render_individual(result, worker["name"], model)
                if report_path.exists() and report_path.read_text(encoding="utf-8") != report:
                    raise ValueError("derived review report changed")
                report_path.write_text(report, encoding="utf-8")
                return {
                    "model": model,
                    "result_path": str(result_path),
                    "result_sha256": store.digest(result_path),
                    "report_path": str(report_path),
                    "report_sha256": store.digest(report_path),
                }

            try:
                return self.run_phase("review", worker, self.manifest["prompt_path"], validate), None
            except StageStopped:
                raise
            except (ValueError, OSError, RuntimeError) as exc:
                return None, f"{worker['name']}: {exc}"

        results = self.parallel(self.request["workers"], review_one)
        records = [record for record, error in results if record]
        failures = [error for record, error in results if error]
        if failures:
            raise RuntimeError("; ".join(failures))
        return records

    def evaluate(self, records):
        self.phase(RoundStatus.SYNTHESIZING.value)
        inputs = review_files.write_synthesis_inputs(self.manifest, records)
        prompt_input = review_synthesize_input(
            self.manifest,
            inputs,
            severities=review_files.SEVERITIES,
            recommendations=review_files.RECOMMENDATIONS,
            dispositions=review_files.DISPOSITIONS,
        )
        prompt = self.prompt(
            f"synthesis-prompt-{inputs['sha256']}.md",
            render_prompt(prompt_input).text,
            stage="synthesis",
        )

        def validate(output, model):
            if store.digest(inputs["path"]) != inputs["sha256"]:
                raise ValueError("synthesis inputs changed")
            path = output / "evaluation.json"
            review_files.read_evaluation(path, self.manifest, records)
            return {"evaluation_path": str(path), "evaluation_sha256": store.digest(path)}

        result = self.run_phase("synthesis", self.request["workers"][0], prompt, validate, input_hash=inputs["sha256"])
        self.evaluation_identity = {**result, "records": records}
        return review_files.read_evaluation(result["evaluation_path"], self.manifest, records)


class Coordinator(ReviewStages):
    store = store
    workspace = review_workspace

    def __init__(self, db_path, task_id, attempt_id, *, fetch_pr=read_pr, **kwargs):
        super().__init__(db_path, task_id, attempt_id, **kwargs)
        self.fetch_pr = fetch_pr

    def _check_canonical_source(self, pr):
        canonical = self.request.get("canonical_identity")
        if not canonical:
            return
        error = None
        if review_identity.revision(pr) != {key: canonical[key] for key in ("head_sha", "base_sha", "base_ref")}:
            error = "PR revision changed after canonical admission"
        acceptance = canonical["acceptance"]
        if pr.get("title", "") != acceptance["title"] or pr.get("body", "") != acceptance["source_body"]:
            error = "PR acceptance changed after canonical admission"
        if error:
            raise StageSuperseded(
                encode_round_stop(
                    {
                        "reason_code": "review_revision_changed",
                        "safe_error": error,
                        "round_id": self.round_id,
                        "task_id": self.task_id,
                        "attempt_id": self.attempt_id,
                        "canonical_sha256": hashlib.sha256(review_identity.encoded(canonical).encode()).hexdigest(),
                    }
                )
            )

    def _check_canonical_snapshot(self):
        if not self.request.get("canonical_identity"):
            return
        review_workspace.verify_snapshot(self.manifest)
        path = review_files.safe_path(
            Path(self.manifest["context_dir"]) / "pr-info.json", self.manifest["artifact_root"]
        )
        self._check_canonical_source(json.loads(path.read_text(encoding="utf-8")))

    def prepare(self):
        self.phase(RoundStatus.PREPARING.value)
        self.reconcile_running()
        repo = self.request["repo"]
        if not self.manifest:
            # Fetch races are bounded; no models have started and no bundle is sealed.
            for retry in range(2):
                pr = self.fetch_pr(repo["full_name"], self.session["pr_number"])
                if pr.get("state") != "open":
                    raise StageSuperseded("PR is closed")
                self._check_canonical_source(pr)
                expected_author = self.request["event"].get("pr_author_login", "")
                if pr.get("user", {}).get("login", "").casefold() != expected_author.casefold():
                    raise ValueError("PR author does not match the authorized review request")
                try:
                    self.manifest = review_workspace.prepare_snapshot(
                        repo["repo_root"],
                        repo["worktree_root"],
                        self.data_dir
                        / "artifacts"
                        / "reviews"
                        / hashlib.sha256(repo["full_name"].encode()).hexdigest()[:16],
                        self.round_id,
                        pr,
                        [self.request["event"]],
                    )
                    if not self.manifest.get("frozen"):
                        self.manifest["github_account"] = repo["github_account"]
                        with store.connect(self.db_path) as conn:
                            self.manifest["decision_owner"] = review_recipient.decision_owner(
                                decision_owner.for_workstream(
                                    conn, self.session["workstream_id"], repo["github_account"]
                                ),
                                pr.get("user", {}).get("login"),
                                repo["github_account"],
                            )
                        self.manifest["worker_config_sha256"] = hashlib.sha256(
                            json.dumps(self.request["workers"], sort_keys=True).encode()
                        ).hexdigest()
                        review_files.atomic_json(Path(self.manifest["context_dir"]) / "manifest.json", self.manifest)
                    break
                except ValueError as exc:
                    if retry or "changed while fetching" not in str(exc):
                        raise
            self.save_manifest()
        elif self.request.get("canonical_identity"):
            self._check_canonical_source(self.fetch_pr(repo["full_name"], self.session["pr_number"]))
        self._check_canonical_snapshot()
        self.prepare_context()
        current_pr = self.fetch_pr(repo["full_name"], self.session["pr_number"])
        self._check_canonical_source(current_pr)
        self._check_canonical_snapshot()
        if not same_revision(self.manifest, current_pr):
            raise StageSuperseded("PR head/base changed after context preparation")

    def synthesize(self, records):
        evaluation = self.evaluate(records)
        load_anchor, save_anchor = self.report_anchor_callbacks()

        def render():
            return (
                review_files.render_merged(self.manifest, records, evaluation)
                + f"\n<!-- robert-review round={self.round_id} -->\n"
            )

        def register_report(descriptor):
            path = descriptor["path"]
            review_workspace.verify_snapshot(self.manifest)
            self.check_active()
            with store.connect(self.db_path) as conn:
                if not attempt_is_active(conn, self.task_id, self.attempt_id):
                    raise StageStopped("review attempt is no longer current")
                store.update(
                    conn,
                    self.round_id,
                    status=RoundStatus.READY.value,
                    report_path=str(path),
                    report_sha256=descriptor["sha256"],
                )
                db_write_artifacts.insert_for_synthesize(
                    conn,
                    self.round_id + ":merged",
                    self.task_id,
                    self.attempt_id,
                    "review_report",
                    str(path),
                    descriptor["sha256"],
                    path.stat().st_size,
                    store.now(),
                )

        try:
            descriptor = report_checkpoint.seal_rendered_report(
                self.db_path,
                self.round_id,
                self.manifest,
                self.evaluation_identity,
                render,
                redact=redaction.redact_text,
                renderer_version=report_checkpoint.code_version(review_files.__file__, __file__),
                redaction_version=report_checkpoint.code_version(redaction.__file__),
                check_active=self.check_active,
                register=register_report,
                load_anchor=load_anchor,
                save_anchor=save_anchor,
            )
        except report_checkpoint.RedactionBlocked as exc:
            redacted = exc.result
            finding = redacted["findings"][0]
            raise ReviewSubmissionRejected(
                result_validation.failure(
                    {
                        "violations": ["redaction_blocked"],
                        "field_path": "planned_github_actions[0].body",
                        "findings": redacted["findings"],
                        "safe_error": f"{finding['rule']} at line {finding['line']}, column {finding['column']}",
                    }
                )
            ) from exc
        path = descriptor["path"]
        body = path.read_text(encoding="utf-8")
        review_workspace.verify_snapshot(self.manifest)
        self.check_active()
        action = {
            "type": GitHubActionType.SUBMIT_PR_REVIEW.value,
            "target_url": f"https://github.com/{self.session['repo']}/pull/{self.session['pr_number']}",
            "body": body,
        }
        payload = worker_result.build_result(
            self.task_id,
            self.attempt_id,
            OutputType.MULTI_AGENT_REVIEW_REPORT.value,
            [item["trigger_fingerprint"] for item in self.triggers()],
            [],
            f"Review report: {path}",
            [action],
            used_skills=[],
            receipt_summary=evaluation.get("receipt_summary"),
        )
        recorded = worker_result.record_result(self.db_path, payload)
        if not recorded.get("ok"):
            if recorded.get("status") == "failed_validation":
                raise ReviewSubmissionRejected(recorded)
            raise RuntimeError("review result was rejected: " + recorded.get("status", "unknown"))
        with store.connect(self.db_path) as conn:
            db_write_attempts.update_for_synthesize(conn, self.attempt_id)
        return path

    def triggers(self):
        with store.connect(self.db_path) as conn:
            associations = store.trigger_associations(conn, self.round_id)
        return associations or [{"trigger_fingerprint": self.session["trigger_fingerprint"]}]

    def run(self):
        try:
            self.prepare()
            return self.synthesize(self.review())
        except Exception as exc:
            with store.connect(self.db_path) as conn:
                status = RoundStatus.SUPERSEDED.value if isinstance(exc, StageSuperseded) else RoundStatus.FAILED.value
                if isinstance(exc, ReviewSubmissionRejected):
                    status = RoundStatus.ATTENTION.value
                    db_write_attempts.update_for_run(conn, json.dumps(exc.diagnostic), self.attempt_id)
                if isinstance(exc, StageStopped):
                    status = (
                        RoundStatus.CANCELED.value
                        if not attempt_is_active(conn, self.task_id, self.attempt_id)
                        else RoundStatus.ATTENTION.value
                    )
                store.update(conn, self.round_id, status=status, error=str(exc))
            with store.connect(self.db_path) as conn:
                terminate_children(conn, self.task_id, store=store)
            raise


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--attempt-id", required=True)
    args = parser.parse_args(argv)

    coordinator = Coordinator(args.db, args.task_id, args.attempt_id)

    def stop(_signum, _frame):
        # Finish any in-flight spawn and persist its identity before cleanup.
        coordinator.stopping.set()

    signal.signal(signal.SIGTERM, stop)
    coordinator.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
