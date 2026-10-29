"""Trusted result targets shared by submission and final controller audit."""
import json
from pathlib import Path
import subprocess

from robert_agent import publish, remediation, review_store, vote_store


def _object(value):
    return json.loads(value or "{}")


def for_result(conn, task_id, attempt_id, workstream_id, *, observe=True, command_runner=None):
    command_runner = command_runner or subprocess.run
    row = conn.execute(
        """
        SELECT r.full_name, r.default_base_branch, a.worktree_path, a.branch_name,
               a.metadata_json, t.task_kind, t.metadata_json
        FROM tasks t
        JOIN workstreams w ON w.workstream_id = t.workstream_id
        JOIN repos r ON r.repo_id = w.repo_id
        JOIN attempts a ON a.attempt_id = ? AND a.task_id = t.task_id
        WHERE t.task_id = ?
          AND t.workstream_id = ?
        """,
        (attempt_id, task_id, workstream_id),
    ).fetchone()
    if not row:
        return None
    sources = [
        {
            "source_type": source_type,
            "number": number,
        }
        for source_type, number in conn.execute(
            """
            SELECT DISTINCT gs.source_type, gs.number
            FROM github_sources gs
            WHERE gs.source_id IN (
              SELECT primary_source_id
              FROM workstreams
              WHERE workstream_id = ?
              UNION
              SELECT source_id
              FROM workstream_sources
              WHERE workstream_id = ?
            )
            ORDER BY gs.source_type, gs.number
            """,
            (workstream_id, workstream_id),
        )
    ]
    action_scope = {
        "workstream_id": workstream_id,
        "repo": row[0],
        "base_branch": row[1],
        "worktree_path": row[2] or "",
        "branch_name": row[3] or "",
        "remote": "origin",
        "sources": sources,
    }
    authors = {}
    for (payload_json,) in conn.execute(
        """SELECT ge.payload_json FROM task_events te JOIN github_events ge ON ge.event_id=te.event_id
           WHERE te.task_id=? ORDER BY ge.event_at, ge.event_id""", (task_id,)
    ):
        event = _object(payload_json)
        if event.get("source_type") == "pull_request" and event.get("pr_author_login"):
            authors[f"https://github.com/{row[0]}/pull/{event.get('number')}"] = event["pr_author_login"]
    action_scope["pr_authors"] = authors
    if observe and action_scope["worktree_path"] and action_scope["branch_name"]:
        # Pin the controller-observed commit and fork, never worker supplied values.
        for command, field in ((["git", "rev-parse", "HEAD"], "result_head_sha"),
                               (["git", "remote", "get-url", "origin"], "head_owner")):
            try:
                inspected = command_runner(command, cwd=action_scope["worktree_path"],
                                           capture_output=True, text=True, timeout=5)
            except OSError:
                continue
            if inspected.returncode == 0 and inspected.stdout.strip():
                value = inspected.stdout.strip()
                if field == "head_owner":
                    value = publish._github_remote_owner(value)
                if value:
                    action_scope[field] = value
    attempt_metadata = _object(row[4])
    existing_pr_head_sha = attempt_metadata.get("existing_pr_head_sha")
    if isinstance(existing_pr_head_sha, str) and existing_pr_head_sha:
        action_scope["existing_pr_head_sha"] = existing_pr_head_sha
    task_metadata = _object(row[6])
    review = review_store.session(conn, task_id=task_id)
    if review and review["manifest"] and review["report_path"]:
        action_scope.update(
            review_round_id=review["round_id"], review_head_sha=review["manifest"]["head_sha"],
            review_base_sha=review["manifest"]["base_sha"], review_base_ref=review["manifest"]["base_ref"],
            review_report_path=review["report_path"], review_report_sha256=review["report_sha256"],
        )
    vote = vote_store.session(conn, task_id=task_id)
    if vote and vote['manifest'] and vote['report_path']:
        from robert_agent import vote_files
        manifest_path = str(Path(vote['manifest']['context_dir']) / 'manifest.json')
        target_kind = 'pull' if vote['source_type'] == 'pull_request' else 'issues'
        action_scope.update(vote_round_id=vote['round_id'], vote_snapshot_id=vote['manifest']['snapshot_id'],
            vote_report_path=vote['report_path'], vote_report_sha256=vote['report_sha256'],
            vote_manifest_path=manifest_path, vote_manifest_sha256=vote_files.digest(manifest_path),
            vote_target_url=f"https://github.com/{vote['repo']}/{target_kind}/{vote['number']}",
            vote_publisher=vote['request']['repo']['github_account'])
    episode_id = task_metadata.get("remediation_episode_id")
    if row[5] in {"ci_remediation", "merge_conflict_remediation"} and episode_id:
        episode = remediation.load_episode(conn, episode_id)
        if episode:
            action_scope.update(
                {
                    "task_kind": row[5],
                    "episode_id": episode_id,
                    "pr_number": episode["metadata"].get("pr_number"),
                    "observed_head_sha": episode["observed_head_sha"],
                    "observed_base_sha": episode["observed_base_sha"],
                }
            )
    return action_scope



def observe_head(scope, *, command_runner=None):
    """Read a commit outside database write transactions; failure is never acceptance."""
    command_runner = command_runner or subprocess.run
    if not scope or not scope.get("worktree_path"):
        raise OSError("Assigned worktree is unavailable")
    try:
        inspected = command_runner(["git", "rev-parse", "HEAD"], cwd=scope["worktree_path"],
                                   capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired as exc:
        raise OSError("Cannot observe submission HEAD") from exc
    head = inspected.stdout.strip()
    if inspected.returncode != 0 or not head:
        raise OSError("Cannot observe submission HEAD")
    return head
