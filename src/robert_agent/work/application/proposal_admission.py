"""Admit a Web-approved proposal through ordinary task preparation."""

from pathlib import Path
import json
import secrets
import subprocess

from robert_agent.adapters.git import proposal_evidence
from robert_agent.adapters.tapd import client as tapd_client
from robert_agent.execution.application import dispatch
from robert_agent.execution import workspace
from robert_agent.foundation import commands, values
from robert_agent.persistence import records, transactions
from robert_agent.persistence.queries import proposals as queries, workstreams
from robert_agent.persistence.stores import proposals as stores, github_events, github_sources
from robert_agent.work import proposal_feedback, proposal_requests, streams
from robert_agent.work.application import preparation, proposal_sources, source_events, commands as work_commands
from robert_agent.workflows.routing import effective_route


def _evidence_current(repo, report):
    current = proposal_evidence.pinned_commit(repo)
    approved = report.get("commit")
    if approved == current:
        return True
    paths = sorted({entry["path"] for entry in report["evidence"]})
    result = commands.run_command(
        [
            "git",
            "--no-replace-objects",
            "--literal-pathspecs",
            "-C",
            repo["repo_root"],
            "diff",
            "--quiet",
            approved,
            current,
            "--",
            *paths,
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise ValueError("approved code evidence is unavailable")
    return result.returncode == 0


def _block(conn, row, reason, now):
    stores.revise(
        conn,
        row["proposal_id"],
        row["version"],
        status="blocked",
        version=row["version"] + 1,
        blocked_reason=reason,
        fingerprint="",
        updated_at=now,
    )


def _event(conn, row, decision, source):
    event = {
        "provider": source["provider"],
        "source_type": source["source_type"],
        "number": source.get("number"),
        "source_key": source["source_key"],
        "workstream_id": source["source_key"],
        "repo": source["repository"],
        "repo_full_name": source["repository"],
        "url": source["url"],
        "title": source["title"],
        "state": "open",
        "source_author_login": source.get("author"),
        "source_body": source["description"],
        "actor_kind": "operator",
        "actor_login": decision["actor"],
        "event_type": "proposal_approved",
        "proposal_id": row["proposal_id"],
        "proposal_action": decision["action"],
        "proposal_discussion": source.get("discussion", []),
        "proposal_revision": row["revision"],
        "proposal_decision_version": decision["version"],
        "event_fingerprint": f"proposal-approved:{row['proposal_id']}:{decision['version']}",
        "event_at": decision["created_at"],
        "body": proposal_requests.request_body(row),
        "body_observed": True,
        "body_authorization_complete": True,
    }
    if source["provider"] == "tapd":
        for key in ("project_key", "external_id"):
            event[key] = source[key]
        public = records.first(workstreams.find_public_ref_by_workstream_id(conn, event["workstream_id"]))
        event["public_ref"] = public[0] if public and public[0] else "rbt-" + secrets.token_hex(6)
        prs = workstreams.find_open_derived_pull_requests(conn, row["repo_id"], source["source_key"])
        if len(prs) > 1:
            raise ValueError("原单据有多个打开的派生 PR，请先处理冲突。")
        if prs:
            stream_id, number, url, raw = prs[0]
            metadata = values.json_object(raw)
            if not metadata.get("head") or not metadata.get("base"):
                raise ValueError("关联 PR 的分支回执不完整，请复核。")
            event.update(
                workstream_id=stream_id,
                has_open_dd_pr=True,
                existing_pr_number=number,
                existing_pr_url=url,
                existing_pr_head_branch=metadata["head"].split(":", 1)[-1],
                base_branch=metadata["base"],
            )
    return event


def _record_tapd(conn, row, event, source, snapshot, now):
    source_id = values.github_source_id(source["source_key"])
    github_sources.upsert_tapd_source(
        conn,
        source_id,
        row["repo_id"],
        source["source_key"],
        source["source_type"],
        source["project_key"],
        source["external_id"],
        source["url"],
        source["title"],
        "open",
        now,
        json.dumps({"tapd": snapshot}),
    )
    github_events.insert_tapd_event_if_absent(
        conn,
        values.github_event_id(event["event_fingerprint"]),
        row["repo_id"],
        source_id,
        event["event_fingerprint"],
        event["event_type"],
        event["actor_login"],
        "context_only",
        event["event_at"],
        json.dumps(event, sort_keys=True),
    )


def _same_approved_source(approved, current):
    if not current:
        return False
    ignored = {"discussion"}
    if approved.get("routing_unconfirmed"):
        ignored |= {"repository_candidates", "routing_unconfirmed", "labels"}
    return {key: value for key, value in approved.items() if key not in ignored} == {
        key: value for key, value in current.items() if key not in ignored
    }


def prepare(conn, *, config, repo, repo_id, now, dispatch_queue, dispatch_budget, prompt_paths, dry_run=False):
    if dry_run:
        return set()
    admitted = set()
    rows = proposal_requests.pending(conn, config, repo_ids=[repo_id])
    for row in rows:
        if min(dispatch_budget["remaining"], dispatch_budget.get("repo_remaining", {}).get(repo_id, 1)) <= 0:
            break
        decision = queries.get_decision(conn, row["proposal_id"], version=row["version"])
        if not decision:
            _block(conn, row, "缺少提案审批，请重新确认。", now)
            continue
        # Observations are durable before network/Git waits release the writer.
        conn.commit()
        try:
            source, report = proposal_requests.approved_source(row, decision), values.json_object(row["report_json"])
            current, snapshot = proposal_sources.refresh(config, repo, source)
            if not _same_approved_source(source, current):
                reason = "原单据、讨论、指派或仓库归属已变化，请复核。"
            elif not _evidence_current(repo, report):
                reason = "提案引用的代码已变化，请复核。"
            else:
                reason = None
        except commands.ControlDeadlineExceeded:
            raise
        except (ValueError, OSError, subprocess.SubprocessError, tapd_client.TapdError):
            reason = "来源或代码证据无法确认，等待重新分析后复核。"
        transactions.begin_immediate(conn)
        current_rows = queries.current(conn, proposal_id=row["proposal_id"])
        if not current_rows or current_rows[0]["version"] != row["version"] or current_rows[0]["status"] != "approved":
            continue
        if reason:
            _block(conn, row, reason, now)
            continue
        try:
            event = _event(conn, row, decision, source)
        except ValueError as error:
            _block(conn, row, str(error), now)
            continue
        binding = queries.work_binding(conn, event["workstream_id"])
        if (binding and binding["active_task_id"]) or (
            config["proposer"].get("schedule", {}).get("enabled")
            and queries.formal_work_exists(conn, event["workstream_id"])
        ):
            _block(conn, row, "原单据已有执行中的工作，请从工作看板查看后复核。", now)
            continue
        event = proposal_requests.reauthorize(conn, config, event)
        if not event["drives_execution"]:
            _block(conn, row, "提案执行授权已失效。", now)
            continue
        if source["provider"] == "tapd":
            _record_tapd(conn, row, event, source, snapshot, now)
        source_events.insert_source_and_event(conn, repo_id, event, now)
        investigating = decision["action"] == "investigate"
        route_id = "new-pr" if not investigating and report["kind"] == "implementation" else "comment-analysis"
        route = effective_route(config, repo, event, route_id)
        route["confidence"] = "high"
        # Ordinary workspace preparation performs its own network work. Its
        # result is checked again against the durable proposal before effects.
        conn.commit()
        try:
            tree = workspace.prepare_worktree(repo, event, route, False)
            workspace_error = tree is not None and not tree.get("ok", True)
        except commands.ControlDeadlineExceeded:
            raise
        except (ValueError, OSError, subprocess.SubprocessError):
            tree, workspace_error = None, True
        transactions.begin_immediate(conn)
        refreshed = queries.current(conn, proposal_id=row["proposal_id"])
        if not refreshed or refreshed[0]["status"] != "approved" or refreshed[0]["version"] != row["version"]:
            continue
        if workspace_error:
            _block(conn, row, "执行工作区准备失败，请修复后重新分析提案。", now)
            continue
        if ((binding := queries.work_binding(conn, event["workstream_id"])) and binding["active_task_id"]) or (
            config["proposer"].get("schedule", {}).get("enabled")
            and queries.formal_work_exists(conn, event["workstream_id"])
        ):
            _block(conn, row, "原单据已有执行中的工作，请从工作看板查看后复核。", now)
            continue
        if not proposal_requests.reauthorize(conn, config, event)["drives_execution"]:
            _block(conn, row, "提案执行授权已失效。", now)
            continue
        info = preparation.create_task_attempt_and_prompt(
            conn,
            Path(config["data_dir"]),
            config["db_path"],
            repo,
            repo_id,
            event,
            route,
            streams.plan_event(event),
            now,
            worktree_result=tree,
            related_events=[event],
        )
        if not info or info.get("blocked"):
            _block(conn, row, "原单据当前无法创建执行，请复核工作项状态。", now)
            continue
        item = work_commands.resolve_work_item_for_workstream(conn, info["workstream_id"])
        stores.revise(
            conn,
            row["proposal_id"],
            row["version"],
            status="investigating" if investigating else "admitted",
            version=row["version"] + 1,
            workstream_id=info["workstream_id"],
            work_item_id=item["work_item_id"],
            source_key=source["source_key"],
            **({"investigation_task_id": info["task_id"]} if investigating else {"task_id": info["task_id"]}),
            updated_at=now,
        )
        if not investigating and proposal_feedback.can_comment(config["proposer"], source):
            proposal_feedback.record_comment(
                conn,
                f"proposal-comment:{row['proposal_id']}:admitted",
                "admitted",
                row["proposal_id"],
                row["revision"],
                source,
                now,
            )
            if source.get("routing_unconfirmed"):
                proposal_feedback.record_comment(
                    conn,
                    f"proposal-comment:{row['proposal_id']}:tag",
                    "tag_sync",
                    row["proposal_id"],
                    row["revision"],
                    source,
                    now,
                )
        dispatch.queue_dispatch(dispatch_queue, info, tree, dispatch_budget, repo_id=repo_id, repo=repo)
        prompt_paths.append(str(info["prompt_path"]))
        admitted.add(info["workstream_id"])
    return admitted
