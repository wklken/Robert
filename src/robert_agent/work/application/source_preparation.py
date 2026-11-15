"""Prepare a repository Worker from one fenced native TAPD intake round."""

from contextlib import closing
from dataclasses import asdict
import json
from pathlib import Path
import re
import subprocess
from uuid import uuid4

from robert_agent.adapters.git import native_publication
from robert_agent.configuration import routes as route_config
from robert_agent.context import attempt as attempt_context, prompts, skills
from robert_agent.context.worker_prompt import worker_prompt_input
from robert_agent.delivery import native_contract
from robert_agent.delivery.tapd_policy import NATIVE_COMMIT_MESSAGE
from robert_agent.execution import ownership, workspace
from robert_agent.foundation import values
from robert_agent.foundation.commands import ControlDeadlineExceeded
from robert_agent.foundation.contracts.delivery import NATIVE_RESULT_CONTRACT, OutputType
from robert_agent.foundation.contracts.execution import AttemptStatus
from robert_agent.foundation.contracts.work import (
    TERMINAL_TASK_LIFECYCLES,
    TaskKind,
    TaskLifecycle,
    WorkstreamLifecycle,
)
from robert_agent.persistence import database, transactions
from robert_agent.persistence.queries import source_pr_lineage, source_pr_selection, source_tasks
from robert_agent.persistence.stores import attempts, source_sessions, task_events, tasks, work_items, workstreams
from robert_agent.work.source_pr_selection import ExistingPRTarget
from robert_agent.work.application import admission, commands, preparation, tapd_intake


_OUTPUTS = {
    "classification": OutputType.CLASSIFICATION_RESULT,
    "evidence": OutputType.LOCAL_RESULT,
    "answer": OutputType.COMMENT_ANALYSIS,
    "question": OutputType.WAITING_FOR_USER,
    "development": OutputType.NEW_PR,
}


def _binding(
    conn, config, claim, *, round_id, revision, repo_id, role, request_id, pr_target, pr_selection, mixed_proof, now
):
    active = source_sessions.lock_round(conn, claim, round_id, now=now)
    if active is None:
        return None
    capability = active["capability"] if role in {"classification", "development"} else "readonly"
    if role == "development" and capability != "development":
        return None
    authorized = admission.native_round_authority(
        conn,
        config,
        claim,
        round_id=round_id,
        revision=revision,
        repo_id=repo_id,
        capability=capability,
        now=now,
        mixed_proof=mixed_proof,
    )
    if authorized is None:
        return None
    active, members, repo = authorized
    if mixed_proof is None and tapd_intake.round_input_status(conn, active, revision=revision) not in {
        "current",
        "frozen",
    }:
        return None
    event_rows = source_tasks.round_trigger_events(conn, round_id)
    member_ids = {member["request_id"] for member in members}
    frozen = json.loads(active["input_snapshot_json"])
    requests = frozen.get("requests") or []
    if {event["request_id"] for event in event_rows} != member_ids or {
        entry["request_id"] for entry in requests
    } != member_ids:
        return None
    primary = request_id or requests[0]["request_id"]
    if primary not in member_ids:
        return None
    if pr_selection is not None:
        decision = source_pr_selection.bound_decision(
            conn,
            source_id=claim.source_id,
            round_id=round_id,
            repo_id=repo_id,
            fingerprint=pr_selection,
        )
        if (
            role != "development"
            or decision is None
            or decision["target"] != (asdict(pr_target) if pr_target else None)
            or any(
                decision[key] != repo[field]
                for key, field in (
                    ("repository", "full_name"),
                    ("publisher", "github_account"),
                    ("default_base_branch", "default_base_branch"),
                )
            )
        ):
            return None
    elif pr_target is not None and not _confirmed_pr(conn, claim.source_id, repo_id, repo, pr_target):
        return None
    return active, repo, primary, capability, event_rows, frozen


def _confirmed_pr(conn, source_id, repo_id, repo, target):
    if (
        type(target.number) is not int
        or target.number <= 0
        or not all(
            isinstance(value, str) and bool(value)
            for value in (
                target.workstream_id,
                target.action_id,
                target.branch_name,
                target.base_branch,
                target.head_sha,
                target.base_sha,
            )
        )
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,180}", target.branch_name)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,180}", target.base_branch)
        or not all(re.fullmatch(r"[0-9a-f]{40}", token) for token in (target.head_sha, target.base_sha))
    ):
        return False
    linked = source_pr_lineage.confirmed_pr_lineage(
        conn, source_id=source_id, repo_id=repo_id, workstream_id=target.workstream_id, action_id=target.action_id
    )
    if linked is not None:
        return (
            linked.state == "open"
            and linked.repository.casefold() == repo["full_name"].casefold()
            and linked.number == target.number
            and linked.head_branch == target.branch_name
            and linked.base_branch == target.base_branch
            and isinstance(target.head_repository, str)
            and linked.head_repository == target.head_repository.casefold()
        )
    # Earlier associations lack a c1 native preparation. Its compatibility
    # guard may never authorize an action which claims new canonical lineage.
    if source_pr_lineage.has_native_creation(conn, target.workstream_id):
        return False
    row = source_tasks.confirmed_pr(
        conn, source_id=source_id, repo_id=repo_id, workstream_id=target.workstream_id, action_id=target.action_id
    )
    expected_url = f"https://github.com/{repo['full_name']}/pull/{target.number}"
    if (
        row is None
        or row["host"] != "github.com"
        or row["project_key"].casefold() != repo["full_name"].casefold()
        or row["number"] != target.number
        or not isinstance(row["state"], str)
        or row["state"].casefold() != "open"
        or row["target_url"] != expected_url
        or (row["html_url"] and row["html_url"] != expected_url)
    ):
        return False
    receipt = values.json_object(row["receipt_json"])
    return (
        receipt.get("action_id") == target.action_id
        and receipt.get("target_url") == expected_url
        and str(receipt.get("external_id")) == str(target.number)
        and bool(receipt.get("confirmed_at"))
    )


def prepare_source_task(
    db_path,
    data_dir,
    config,
    claim,
    *,
    round_id,
    revision,
    repo_id,
    role,
    request_id=None,
    pr_target=None,
    pr_selection=None,
    route_resolution=None,
    mixed_proof=None,
):
    """Create one actual native task, attempt and prompt after fenced repository prep."""
    if role not in _OUTPUTS or (pr_target is not None and role not in {"classification", "development"}):
        return None
    if pr_target is not None and not isinstance(pr_target, ExistingPRTarget):
        return None
    args = dict(
        round_id=round_id,
        revision=revision,
        repo_id=repo_id,
        role=role,
        request_id=request_id,
        pr_target=pr_target,
        pr_selection=pr_selection,
        mixed_proof=mixed_proof,
    )
    with closing(database.connect(db_path, foreign_keys=True)) as conn, conn:
        transactions.begin_immediate(conn)
        initial = _binding(conn, config, claim, **args, now=values.utc_now())
    if initial is None:
        return None
    round_row, repo, primary, capability, event_rows, frozen = initial
    output = OutputType.UPDATE_EXISTING_PR if role == "development" and pr_target else _OUTPUTS[role]
    route_id = native_contract.route_id_for(output)
    if route_resolution is not None and route_resolution.get("selected_route") != route_id:
        return None
    selected = route_config.resolve_route_config(config, repo, route_config.ROUTE_POLICIES[route_id])
    installed = skills.discover_skill_names((config.get("skills") or {}).get("search_paths", ()))
    status = skills.route_skill_status(selected["required_skills"], selected["recommended_skills"], installed)
    if not status["runnable"]:
        return None
    route = {**selected, "route_id": route_id, "confidence": "high"}
    try:
        root, tree_root = workspace.native_repository_paths(repo)
    except (OSError, RuntimeError, ValueError):
        return None
    purpose = {
        "mode": ("existing_pr" if pr_target else "new_branch") if role == "development" else "readonly",
        "repo_root": root,
        "worktree_root": tree_root,
    }
    if pr_selection is not None:
        purpose.update(pr_selection_required=True, pr_selection=pr_selection)
    if purpose["mode"] == "new_branch":
        purpose["workspace_key"] = uuid4().hex
    pr = (
        {
            "number": pr_target.number,
            "branch_name": pr_target.branch_name,
            "head_sha": pr_target.head_sha,
            "base_branch": pr_target.base_branch,
            "base_sha": pr_target.base_sha,
            "head_repository": pr_target.head_repository,
        }
        if pr_target
        else None
    )
    if pr_target and role == "development":
        if not isinstance(pr_target.head_repository, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", pr_target.head_repository
        ):
            return None
        purpose["pr"] = {"workstream_id": pr_target.workstream_id, "action_id": pr_target.action_id, **pr}
    path_repo = {**repo, "repo_root": root, "worktree_root": tree_root}
    try:
        if role == "development":
            native_publication.verify_upstream(root, repo["full_name"])
        worktree = workspace.prepare_source_worktree(path_repo, purpose, pr=pr)
        purpose["branch_name"] = worktree.get("branch_name")
        if not worktree.get("ok") or not workspace.validate_source_workspace(
            repo, purpose, worktree, require_git_pin=False
        ):
            return None
        if purpose["mode"] == "readonly":
            try:
                purpose["readonly_snapshot"] = native_publication.readonly_snapshot(root, repo["full_name"])
            except ControlDeadlineExceeded:
                raise
            except (OSError, ValueError, subprocess.SubprocessError):
                # A normal answer remains possible; later recovery must not invent its original proof.
                pass
        if role == "development":
            purpose["commit_message"] = NATIVE_COMMIT_MESSAGE
            purpose["git_pin"] = native_publication.pin(
                worktree["worktree_path"],
                purpose,
                repo["full_name"],
                pr["base_branch"] if pr else repo["default_base_branch"],
            )
    except ControlDeadlineExceeded:
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    task_id, attempt_id = values.new_id("task"), values.new_id("attempt")
    manifest = {
        "round_id": round_id,
        "request_id": primary,
        "repo_id": repo_id,
        "capability": capability,
        "role": "answer" if role == "question" else role,
        "allowed_actions": native_contract.selectors_for(output),
    }
    now = values.utc_now()
    prompt_path = Path(data_dir).resolve() / "artifacts" / task_id / f"{attempt_id}.prompt.md"
    with closing(database.connect(db_path, foreign_keys=True)) as conn, conn:
        transactions.begin_immediate(conn)
        if _binding(conn, config, claim, **args, now=now) != initial:
            return None
        checkpoint = "prepare_source_task"
        transactions.savepoint(conn, checkpoint)
        try:
            item = commands.ensure_tapd_work_item(conn, repo_id=repo_id, source_id=claim.source_id, now=now)
            stream_id = pr_target.workstream_id if pr_target else item["workstream_id"]
            stream = source_tasks.stream_ready(conn, stream_id)
            if (
                item["canceled_at"]
                or stream is None
                or stream["lifecycle"] == "canceled"
                or (stream["task_lifecycle"] is not None and stream["task_lifecycle"] not in TERMINAL_TASK_LIFECYCLES)
            ):
                transactions.finish_isolation(conn, checkpoint, rollback=True)
                return None
            task_info = {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "workstream_id": stream_id,
                "route_id": route_id,
                "prompt_path": prompt_path,
                "blocked": False,
            }
            runtime = {
                **attempt_context.runtime_context(db_path, path_repo, worktree),
                "origin_type": "tapd",
                "native_result": manifest,
                "native_input_revision": round_row["input_version"],
                "native_commit_message": purpose.get("commit_message"),
                "native_source_snapshot": frozen,
                "native_classification_routes": native_contract.classification_routes(
                    capability, existing_pr=bool(pr_target)
                ),
            }
            event_input = [dict(row) for row in event_rows]
            rendered = prompts.render_prompt(
                worker_prompt_input(task_info, route, event_input, runtime_context=runtime)
            )
            tasks.insert_for_create_task_attempt_and_prompt(
                conn,
                task_id,
                stream_id,
                TaskKind.HUMAN_REQUEST.value,
                TaskLifecycle.QUEUED.value,
                None,
                route_id,
                output,
                now,
                now,
                json.dumps({"review_input_policy": "none"}, sort_keys=True),
            )
            preparation.upsert_route_decision(conn, task_id, route, now)
            if not source_sessions.bind_task(
                conn, claim, round_id=round_id, request_id=primary, task_id=task_id, capability=capability, now=now
            ) or not ownership.claim_task(conn, task_id, now):
                transactions.finish_isolation(conn, checkpoint, rollback=True)
                return None
            trigger_ids = {row["event_id"] for row in event_rows}
            for event in event_rows:
                task_events.bind_task_event(conn, task_id, event["event_id"], "trigger", now, ignore_duplicate=True)
            for comment in frozen.get("discussion", []):
                if comment["event_id"] not in trigger_ids:
                    task_events.bind_task_event(
                        conn, task_id, comment["event_id"], "context", now, ignore_duplicate=True
                    )
            attempts.insert_for_create_task_attempt_and_prompt(
                conn,
                attempt_id,
                task_id,
                AttemptStatus.PREPARED.value,
                worktree["worktree_path"],
                worktree.get("branch_name"),
                now,
                now,
                json.dumps(
                    {
                        "result_contract": NATIVE_RESULT_CONTRACT,
                        "native_result": manifest,
                        "native_workspace": purpose,
                        "prompt_contract": rendered.metadata,
                        **({"native_pr_selection": pr_selection} if pr_selection else {}),
                        **({"native_route_resolution": route_resolution} if route_resolution else {}),
                        **({"existing_pr_head_sha": pr_target.head_sha} if pr_target and role == "development" else {}),
                    },
                    sort_keys=True,
                    ensure_ascii=False,
                ),
            )
            attempt_context.atomic_replace_files({prompt_path: rendered.text.encode("utf-8")})
            preparation.insert_file_artifact(conn, task_id, attempt_id, "prompt", prompt_path, now)
            if item["completed_at"] is not None and not work_items.reactivate_tapd_work_item(
                conn, work_item_id=item["work_item_id"], source_id=claim.source_id, repo_id=repo_id, now=now
            ):
                transactions.finish_isolation(conn, checkpoint, rollback=True)
                prompt_path.unlink(missing_ok=True)
                return None
            if stream_id != item["workstream_id"] and not workstreams.set_tapd_control_lifecycle(
                conn,
                workstream_id=item["workstream_id"],
                source_id=claim.source_id,
                repo_id=repo_id,
                lifecycle=WorkstreamLifecycle.ACTIVE,
                now=now,
            ):
                transactions.finish_isolation(conn, checkpoint, rollback=True)
                prompt_path.unlink(missing_ok=True)
                return None
            workstreams.update_for_create_task(conn, task_id, now, stream_id)
            if not work_items.claim_tapd_projection(
                conn,
                work_item_id=item["work_item_id"],
                source_id=claim.source_id,
                repo_id=repo_id,
                round_id=round_id,
                task_id=task_id,
                now=now,
            ):
                transactions.finish_isolation(conn, checkpoint, rollback=True)
                prompt_path.unlink(missing_ok=True)
                return None
        except BaseException:
            transactions.finish_isolation(conn, checkpoint, rollback=True)
            prompt_path.unlink(missing_ok=True)
            raise
        transactions.release(conn, checkpoint)
    return {**task_info, "repo_id": repo_id, "repo": repo, "worktree_result": worktree}
