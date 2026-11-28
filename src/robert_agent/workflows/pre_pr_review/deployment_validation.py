"""Validate and seal local deployment evidence for the final PR revision."""

import json
from pathlib import Path
import re
import subprocess

from robert_agent.adapters.git.review_revision import clean_head, git
from robert_agent.context.native_prompts import deployment_validation_input
from robert_agent.context.prompts import render_prompt
from robert_agent.context.skills import NAME_RE
from robert_agent.execution.stages import StageStopped, StageSuperseded
from robert_agent.foundation.commands import ControlDeadlineExceeded
from robert_agent.persistence.rounds import pre_pr_review as store
from robert_agent.security.redaction import redact_report


STAGE = "validate_deployment"
RESULT = "deployment-validation.json"
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
_WORKLOAD = re.compile(r"[a-z0-9][a-z0-9.-]{0,127}")


class DeploymentValidationBlocked(StageStopped):
    """A draft can retain its review while deployment validation is unresolved."""


def _single_line(value):
    return isinstance(value, str) and len(value) <= 500 and bool(value.strip()) and value.splitlines() == [value]


def _read_result(path, identity, *, docs_only):
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or set(value)
        != {*identity, "schema_version", "environment", "status", "images", "assertions", "cleanup", "summary"}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or any(value[key] != expected for key, expected in identity.items())
        or value["environment"] != "dev/bk-e2e"
    ):
        raise ValueError("invalid deployment result schema or revision")
    status, images, assertions, cleanup = (value[key] for key in ("status", "images", "assertions", "cleanup"))
    if (
        status not in ("passed", "skipped", "failed", "blocked")
        or cleanup not in ("verified", "not_needed", "incomplete")
        or not isinstance(images, dict)
        or not isinstance(assertions, list)
        or not _single_line(value["summary"])
    ):
        raise ValueError("invalid deployment result fields")
    if any(
        not _WORKLOAD.fullmatch(name) or not isinstance(image, str) or not _IMAGE_ID.fullmatch(image)
        for name, image in images.items()
    ):
        raise ValueError("deployment evidence requires observed workload image IDs")
    for assertion in assertions:
        if (
            not isinstance(assertion, dict)
            or set(assertion) != {"action", "expected", "observed", "passed"}
            or type(assertion["passed"]) is not bool
            or not all(_single_line(assertion[key]) for key in ("action", "expected", "observed"))
        ):
            raise ValueError("invalid deployed behavior assertion")
    if status == "passed" and (
        not images or not assertions or not all(item["passed"] for item in assertions) or cleanup != "verified"
    ):
        raise ValueError("passing deployment requires running images, passing assertions and verified cleanup")
    if status == "skipped" and (not docs_only or images or assertions or cleanup != "not_needed"):
        raise ValueError("only Markdown documentation changes may skip deployment")
    return value


def _summary(value):
    redacted = redact_report(value["summary"])
    return redacted["text"] if redacted["ok"] else "[redacted]"


def verify(driver, disposition, settings):
    driver.stage = STAGE
    driver.phase(STAGE)
    manifest = driver.manifest
    head = disposition["head"]
    tree = driver.address_workspace(disposition["run_id"]) if disposition.get("run_id") else driver.assigned_path
    identity = {"round_id": driver.round_id, "snapshot_id": manifest["snapshot_id"], "head_sha": head}
    try:
        if clean_head(tree) != head:
            raise StageSuperseded("deployment validation worktree changed")
        skill_path = Path(settings["skill_path"])
        skill_digest = store.digest(skill_path)
        skill = skill_path.read_text(encoding="utf-8")
        name = NAME_RE.search(skill)
        if not name or name["name"].strip() != "fast-local-k8s-context" or store.digest(skill_path) != skill_digest:
            raise ValueError("configured manifest is not fast-local-k8s-context")
        paths = git(tree, "diff", "--no-renames", "--name-only", "-z", manifest["merge_base_sha"], head, "--").split(
            "\0"
        )
        changed = [path for path in paths if path]
        docs_only = bool(changed) and all(
            path.endswith(".md") and (path.startswith("docs/") or "/" not in path) for path in changed
        )
        prompt = driver.prompt(
            f"validate-deployment-{head}.md",
            render_prompt(
                deployment_validation_input(
                    manifest,
                    identity,
                    tree,
                    skill_path,
                    skill_digest,
                    docs_only=docs_only,
                    timeout_seconds=settings["worker"]["timeout_seconds"],
                )
            ).text,
        )
        driver.deployment_input = {"worktree": tree, "head_sha": head, "skill_sha256": skill_digest}

        def validate(output, _model):
            if clean_head(tree) != head or store.digest(skill_path) != skill_digest:
                raise StageSuperseded("deployment validation source or skill changed")
            path = output / RESULT
            value = _read_result(path, identity, docs_only=docs_only)
            if value["status"] in ("failed", "blocked"):
                raise ValueError(f"deployment {value['status']} (cleanup {value['cleanup']}): {_summary(value)}")
            return {
                **identity,
                "status": value["status"],
                "deployment_validation_path": str(path),
                "deployment_validation_sha256": store.digest(path),
            }

        worker = {**settings["worker"], "fallback_model": None}
        return driver.run_phase(STAGE, worker, prompt, validate)
    except ControlDeadlineExceeded:
        raise
    except subprocess.SubprocessError as exc:
        raise DeploymentValidationBlocked(f"Minikube validation for {head}: Git inspection failed") from exc
    except (OSError, ValueError, RuntimeError) as exc:
        raise DeploymentValidationBlocked(f"Minikube validation for {head}: {exc}") from exc


def disclosure(evidence):
    path = Path(evidence["deployment_validation_path"])
    if store.digest(path) != evidence["deployment_validation_sha256"]:
        raise StageStopped("registered deployment evidence changed")
    value = json.loads(path.read_text(encoding="utf-8"))
    if value["status"] == "skipped":
        return f"Minikube 验证跳过（`{value['head_sha']}`，仅 Markdown 文档）。{_summary(value)}"
    images = "、".join(f"`{name}` `{image}`" for name, image in sorted(value["images"].items()))
    return (
        f"Minikube 验证通过（`{value['head_sha']}`，{len(value['assertions'])} 项行为断言，已验证恢复）。"
        f"运行镜像 {images}。{_summary(value)}"
    )
