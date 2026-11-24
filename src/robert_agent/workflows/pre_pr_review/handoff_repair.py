"""Bounded output-contract repair without rerunning source changes."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess

from robert_agent.adapters.git.review_revision import clean_head
from robert_agent.context.native_prompts import repair_handoff_input
from robert_agent.context.prompts import render_prompt
from robert_agent.delivery import contract
from robert_agent.execution.budgets import MAX_HANDOFF_REPAIR_ATTEMPTS, MAX_HANDOFF_REPAIR_SECONDS
from robert_agent.execution.stages import StageStopped
from robert_agent.persistence.rounds import pre_pr_review as store
from robert_agent.workflows.review import files


def repairable(exc):
    return isinstance(exc, contract.InvalidSubmission) and str(exc) in {
        "Submission contains unsupported or program-owned fields.",
        "Action contains unsupported or program-owned fields.",
        "Verification contains unsupported fields.",
        "Skipped verification requires a reason.",
    }


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise StageStopped("ambiguous duplicate handoff field")
        value[key] = item
    return value


def _invalid_constant(value):
    raise StageStopped("non-finite handoff value: " + value)


def _read(content):
    try:
        value = json.loads(content, object_pairs_hook=_object, parse_constant=_invalid_constant)
    except (ValueError, UnicodeError) as exc:
        raise StageStopped("malformed handoff cannot be safely repaired") from exc
    if not isinstance(value, dict):
        raise StageStopped("handoff repair requires an object")
    return value


def _preserved(value, stage):
    """Remove only fields declared unsupported by the submission contract."""
    value = deepcopy(value)
    if stage == "implement":
        value = {key: item for key, item in value.items() if key in contract.FIELDS}
    for action in value.get("planned_github_actions", []):
        if isinstance(action, dict) and action.get("type") in contract.ACTION_FIELDS:
            for key in set(action) - contract.ACTION_FIELDS[action["type"]]:
                del action[key]
    verification_fields = {
        "command",
        "status",
        "purpose",
        "required",
        "exit_code",
        "skipped_reason",
        "source",
        "command_record_path",
        "url",
    }
    for check in value.get("verification", []):
        if isinstance(check, dict):
            for key in set(check) - verification_fields:
                del check[key]
    return value


def _check_preserved(original, repaired, stage):
    expected = _preserved(original, stage)
    actual = deepcopy(repaired)
    before_checks, after_checks = expected.get("verification"), actual.get("verification")
    if isinstance(before_checks, list) and isinstance(after_checks, list):
        for before, after in zip(before_checks, after_checks):
            if (
                isinstance(before, dict)
                and isinstance(after, dict)
                and before.get("status") == "skipped"
                and not (isinstance(before.get("skipped_reason"), str) and before["skipped_reason"].strip())
            ):
                purpose = before.get("purpose", "")
                # A purpose describing what to test is not evidence of why it
                # was skipped. Only copy an explicit recorded explanation.
                if not isinstance(purpose, str) or not purpose.lower().startswith("skipped:"):
                    raise StageStopped("missing recorded evidence for skipped_reason")
                reason = purpose.partition(":")[2].strip()
                if not reason or after.get("skipped_reason") != reason:
                    raise StageStopped("handoff repair invented skipped_reason")
                before["skipped_reason"] = reason
    # Serialized equality distinguishes boolean/integer and missing/null too.
    if json.dumps(actual, sort_keys=True) != json.dumps(expected, sort_keys=True):
        raise StageStopped("handoff repair changed supported fields or verification evidence")


def _worktrees(coordinator, run):
    writer = coordinator.assigned_path if run["stage"] == "implement" else coordinator.address_workspace(run["run_id"])
    coordinator.handoff_repair_worktree = str(writer)
    paths = {
        str(writer): coordinator.branch if run["stage"] == "implement" else None,
        str(coordinator.assigned_path): coordinator.branch,
    }
    result = []
    for path, branch in sorted(paths.items()):
        configured = Path(path).absolute()
        if not configured.is_dir() or configured.resolve() != configured:
            raise StageStopped("handoff repair worktree path changed")
        result.append({"path": path, "branch": branch, "head": clean_head(path, branch)})
    return result


def repair(coordinator, run, validate, model, exc):
    if not repairable(exc):
        raise StageStopped("handoff error is not safely repairable") from exc
    try:
        name = coordinator.handoff_files[run["stage"]][0]
        source = coordinator.sealed_output(run) / name
        original = _read(source.read_bytes())
        prompt = Path(run["dispatch"]["prompt_path"])
        inputs = {
            "stage": run["stage"],
            "source_run_id": run["run_id"],
            "handoff_path": str(source),
            "handoff_sha256": store.digest(source),
            "prompt_path": str(prompt),
            "prompt_sha256": run["prompt_sha256"],
            "worktrees": _worktrees(coordinator, run),
        }
        if store.digest(prompt) != inputs["prompt_sha256"]:
            raise StageStopped("handoff repair prompt changed")
        # Pin once before launch; a resume cannot bless a newly changed HEAD.
        with store.connect(coordinator.db_path) as conn:
            current = next(item for item in store.runs(conn, coordinator.round_id) if item["run_id"] == run["run_id"])
            dispatch = current["dispatch"]
            pinned = dispatch.get("handoff_repair_checkpoint")
            if pinned is not None and pinned != inputs:
                raise StageStopped("handoff repair source checkpoint changed")
            if pinned is None:
                store.update_run(
                    conn, run["run_id"], dispatch_json=json.dumps({**dispatch, "handoff_repair_checkpoint": inputs})
                )
        inputs["error"] = str(exc)
        key = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
        inputs["input_sha256"] = key
        input_path = coordinator.prompt(
            f"handoff-repair-input-{key}.json", json.dumps(inputs, sort_keys=True, indent=2)
        )
        repair_prompt = coordinator.prompt(
            f"repair-handoff-{key}.md", render_prompt(repair_handoff_input(inputs, input_path)).text
        )
        coordinator.handoff_repair_input = str(input_path)
        input_digest, repair_prompt_digest = store.digest(input_path), store.digest(repair_prompt)

        def check_inputs():
            coordinator.check_active()
            if (
                _worktrees(coordinator, run) != inputs["worktrees"]
                or store.digest(source) != inputs["handoff_sha256"]
                or store.digest(prompt) != inputs["prompt_sha256"]
                or store.digest(input_path) != input_digest
                or store.digest(repair_prompt) != repair_prompt_digest
            ):
                raise StageStopped("handoff repair input or source worktree changed")

        def validate_repair(output, _model):
            try:
                check_inputs()
                repaired = files.safe_path(output / "handoff.json", coordinator.manifest["artifact_root"])
                content = repaired.read_bytes()
                _check_preserved(original, _read(content), run["stage"])
                corrected = files.safe_path(
                    Path(coordinator.manifest["artifact_root"])
                    / "corrected-handoffs"
                    / key
                    / hashlib.sha256(content).hexdigest(),
                    coordinator.manifest["artifact_root"],
                )
                corrected.mkdir(parents=True, exist_ok=True)
                target = files.safe_path(corrected / name, coordinator.manifest["artifact_root"])
                if not target.exists():
                    target.write_bytes(content)
                if target.read_bytes() != content:
                    raise StageStopped("corrected handoff changed")
                coordinator.handoff_source_run_id = run["run_id"]
                try:
                    artifact = validate(corrected, model)
                finally:
                    coordinator.handoff_source_run_id = None
                check_inputs()
                if target.read_bytes() != content or repaired.read_bytes() != content:
                    raise StageStopped("corrected handoff changed during validation")
                return {
                    **artifact,
                    "handoff_repair_path": str(repaired),
                    "handoff_repair_sha256": hashlib.sha256(content).hexdigest(),
                }
            except StageStopped:
                raise
            except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
                raise StageStopped("handoff repair validation refused: " + str(error)) from error

        profile = {
            **coordinator.implementer,
            "timeout_seconds": min(coordinator.implementer["timeout_seconds"], MAX_HANDOFF_REPAIR_SECONDS),
        }
        for attempt in range(MAX_HANDOFF_REPAIR_ATTEMPTS):
            check_inputs()
            try:
                artifact = coordinator.run_phase(
                    "repair_handoff", profile, repair_prompt, validate_repair, input_hash=key
                )
                check_inputs()
                return {
                    **{
                        field: value
                        for field, value in artifact.items()
                        if field not in {"run_id", "worker_id", "input_sha256"}
                    },
                    "model": model,
                }
            except StageStopped:
                raise
            except (OSError, RuntimeError) as error:
                check_inputs()
                if attempt + 1 == MAX_HANDOFF_REPAIR_ATTEMPTS:
                    raise StageStopped("handoff repair transient retry budget exhausted") from error
    except StageStopped:
        raise
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        raise StageStopped("handoff repair refused: " + str(error)) from error
