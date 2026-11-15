"""Prepare native effect metadata from controller-observed local Git facts."""

from hashlib import sha256
import subprocess

from robert_agent.adapters.git import native_publication
from robert_agent.delivery import native_contract, tapd_policy
from robert_agent.foundation.commands import ControlDeadlineExceeded
from robert_agent.foundation.contracts.delivery import AuditStatus, GitHubActionType, NATIVE_RESULT_CONTRACT, OutputType
from robert_agent.persistence.queries import source_github_publication
from robert_agent.persistence.rounds.source_evidence import context_digest, json_object

_CHECK_COMMANDS = {
    ("python3", "-m", "unittest"): "unit_tests",
    ("python", "-m", "unittest"): "unit_tests",
    ("pytest",): "unit_tests",
    ("go", "test", "./..."): "unit_tests",
    ("npm", "test"): "unit_tests",
    ("npm", "run", "build"): "build",
    ("ruff", "check"): "lint",
    ("ruff", "format", "--check"): "format",
    ("mypy",): "type_check",
}


def _verification(entries):
    """Project only closed categories, labeled as Worker reports."""
    facts = set()
    for entry in entries:
        command = entry.get("command")
        if not isinstance(command, list) or not all(isinstance(token, str) for token in command):
            continue
        name = _CHECK_COMMANDS.get(tuple(command))
        outcome = entry.get("status")
        if name and outcome in {"passed", "failed", "skipped"}:
            facts.add((name, outcome))
    return tuple(tapd_policy.VerificationResult(*value) for value in sorted(facts))


def _sensitive_values(source, snapshot, config):
    """Read source literals for conservative filename checks, never for rendering."""
    ticket = snapshot.get("ticket") or {}
    discussion = snapshot.get("discussion") or ()
    values = [
        source.get("host"),
        source.get("workspace_id"),
        source.get("external_id"),
        config.get("bot_name"),
        config.get("reply_name"),
        ticket.get("title"),
        ticket.get("body"),
        *(ticket.get("owners") or ()),
        *(ticket.get("labels") or ()),
    ]
    for comment in discussion:
        values.extend(comment.get(key) for key in ("creator", "display_author", "normalized_text", "body_html"))
    return tuple(value for value in values if isinstance(value, str) and value.strip())


def prepare_github(result, scope, purpose, snapshot, tapd):
    """Observe Git and return immutable public facts, before obtaining a database writer."""
    if not isinstance(purpose, dict) or scope is None:
        return None
    binding = purpose.get("git_pin")
    message = purpose.get("commit_message")
    output = result["output_type"]
    expected_mode = {OutputType.NEW_PR: "new_branch", OutputType.UPDATE_EXISTING_PR: "existing_pr"}.get(output)
    if (
        not isinstance(binding, dict)
        or message != tapd_policy.NATIVE_COMMIT_MESSAGE
        or expected_mode is None
        or purpose.get("mode") != expected_mode
        or scope.get("branch_name") != purpose.get("branch_name")
        or (scope.get("repo") or "").casefold() != binding.get("repository")
        or (expected_mode == "new_branch" and scope.get("base_branch") != binding.get("base_branch"))
    ):
        return None
    try:
        facts = native_publication.inspect(
            scope["worktree_path"], purpose, binding, expected_head=result.get("submission_head_sha")
        )
        changes = [tapd_policy.CodeChange(path, operation) for operation, path in facts.changes]
        checks = _verification(result.get("verification", []))
        projection = tapd_policy.project_github_output(
            changes=changes,
            verification=checks,
            approved_paths=(fact.path for fact in changes),
            forbidden_text=_sensitive_values(result["native_source"], snapshot, tapd),
        )
    except ControlDeadlineExceeded:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.CalledProcessError):
        return None
    return {
        "version": 1,
        "purpose": purpose,
        "head_sha": facts.head,
        "commits": list(facts.commits),
        "changed_paths": [[operation, path] for operation, path in facts.changes],
        "checks": [[item.check, item.outcome] for item in checks],
        "selectors": [
            {key: action[key] for key in ("provider", "type", "target_id")}
            for action in result["planned_actions"]
            if action["provider"] == "github"
        ],
        "title": projection.title,
        "body": projection.body,
        "commit_message": message,
    }


def prepare_tapd(result, repository_root, github_proof, *, readonly_snapshot=None):
    """Check the original TAPD reply and return its immutable audit evidence without rewriting it."""
    replies = [action["body"] for action in result["planned_actions"] if action["provider"] == "tapd"]
    if len(replies) != 1:
        return None
    body = replies[0]
    if not isinstance(body, str):
        return None
    paths = tapd_policy.mapped_text_paths(body)
    if len(paths) > 64:
        return None
    try:
        if paths and not repository_root:
            return None
        if github_proof:
            purpose = github_proof["purpose"]
            revision = github_proof["head_sha"]
        else:
            revision = "HEAD"
        approved = native_publication.mapped_files(repository_root, paths, revision=revision)
        if github_proof and approved != paths:
            deleted = {path for op, path in github_proof["changed_paths"] if op == "deleted"}
            approved |= native_publication.mapped_files(
                repository_root, (paths - approved) & deleted, revision=purpose["git_pin"]["initial_head"]
            )
    except ControlDeadlineExceeded:
        raise
    except (OSError, ValueError, TypeError, KeyError, subprocess.CalledProcessError):
        return None
    if not tapd_policy.safe_mapped_text(body, approved):
        return None
    proof = {"version": 1, "body_sha256": sha256(body.encode()).hexdigest(), "mapped_paths": sorted(approved)}
    if (
        not github_proof
        and isinstance(readonly_snapshot, dict)
        and readonly_snapshot.get("root") == str(repository_root)
    ):
        if recheck_readonly(readonly_snapshot):
            proof["readonly_snapshot"] = readonly_snapshot
    return proof


def recheck_readonly(snapshot):
    """Compare only an originally recorded checkout; never create a recovery pin."""
    if not isinstance(snapshot, dict):
        return False
    try:
        return snapshot == native_publication.readonly_snapshot(snapshot["root"], snapshot["repository"])
    except ControlDeadlineExceeded:
        raise
    except (OSError, KeyError, TypeError, ValueError, subprocess.SubprocessError):
        return False


def _candidate_metadata(action, fields):
    saved = json_object(action.get("metadata_json"))
    if (
        saved is None
        or set(saved) - {"publish"} != fields
        or ("publish" in saved and not isinstance(saved["publish"], dict))
    ):
        return None
    return {key: saved[key] for key in fields}


def worker_tapd_body(action, result_metadata):
    """Return the exact original Worker body and mapped paths from accepted audit evidence."""
    metadata = json_object(result_metadata) if isinstance(result_metadata, str) else result_metadata
    saved = _candidate_metadata(action, {"body", "target_id"})
    audit = metadata.get("audit") if isinstance(metadata, dict) else None
    normalized = metadata.get("normalized_payload") if isinstance(metadata, dict) else None
    if (
        not isinstance(audit, dict)
        or metadata.get("result_contract") != NATIVE_RESULT_CONTRACT
        or audit.get("status") != AuditStatus.ACCEPTED
        or audit.get("ok") is not True
        or action.get("audit_status") != AuditStatus.ACCEPTED
        or action.get("provider") != "tapd"
        or action.get("action_type") != GitHubActionType.COMMENT
        or not isinstance(normalized, dict)
        or normalized.get("task_id") != action.get("task_id")
        or saved is None
        or not isinstance(saved["target_id"], str)
        or not isinstance(audit.get("planned_actions"), list)
        or {"provider": "tapd", "type": GitHubActionType.COMMENT, "target_id": saved["target_id"]}
        not in audit["planned_actions"]
    ):
        return None
    proof = audit.get("native_tapd")
    body = saved["body"]
    if (
        not isinstance(proof, dict)
        or proof.get("version") != 1
        or not isinstance(body, str)
        or proof.get("body_sha256") != sha256(body.encode()).hexdigest()
    ):
        return None
    paths = proof.get("mapped_paths")
    if (
        not isinstance(paths, list)
        or len(paths) > 64
        or not all(isinstance(path, str) for path in paths)
        or paths != sorted(tapd_policy.mapped_text_paths(body))
        or not tapd_policy.safe_mapped_text(body, paths)
    ):
        return None
    return body, tuple(paths)


def recheck_github(scope):
    """Reprove pinned worktree, branch, remotes and code facts after accepted audit."""
    try:
        proof = scope["native_github"]
        purpose = proof["purpose"]
        binding = purpose["git_pin"]
        head = scope["result_head_sha"]
        if (
            not _projection_matches(proof)
            or proof["head_sha"] != head
            or proof["commit_message"] != tapd_policy.NATIVE_COMMIT_MESSAGE
            or proof["commit_message"] != purpose["commit_message"]
            or purpose["branch_name"] != scope["branch_name"]
            or binding["repository"] != scope["repo"].casefold()
            or binding["base_branch"] != scope["base_branch"]
            or scope["remote"] != "origin"
        ):
            return False
        facts = native_publication.inspect(scope["worktree_path"], purpose, binding, expected_head=head)
        return (
            list(facts.commits) == proof["commits"]
            and [list(change) for change in facts.changes] == proof["changed_paths"]
        )
    except ControlDeadlineExceeded:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.CalledProcessError):
        return False


def _projection_matches(proof):
    try:
        if proof.get("version") != 1 or not proof["changed_paths"]:
            return False
        changes = [tapd_policy.CodeChange(path, op) for op, path in proof["changed_paths"]]
        checks = [tapd_policy.VerificationResult(*check) for check in proof["checks"]]
        rendered = tapd_policy.project_github_output(
            changes=changes,
            verification=checks,
            approved_paths=(change.path for change in changes),
        )
        return (proof["title"], proof["body"], proof["commit_message"]) == (
            rendered.title,
            rendered.body,
            rendered.commit_message,
        )
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def delivered_worker_tapd_body(conn, root_id, action, result_metadata, output_type, preceding):
    """Render exact original TAPD text plus code facts and one proved PR link."""
    original = worker_tapd_body(dict(action), result_metadata)
    if original is None:
        return None
    body, paths = original
    expected = native_contract.selectors_for(output_type)
    actual = [
        {
            "provider": row["provider"],
            "type": row["action_type"],
            "target_id": (json_object(row["metadata_json"]) or {}).get("target_id"),
        }
        for row in (*preceding, action)
    ]
    if not expected or actual != expected:
        return None
    if not preceding:
        return body
    metadata = json_object(result_metadata) if isinstance(result_metadata, str) else result_metadata
    audit = metadata.get("audit") or {}
    action_scope = audit.get("action_scope") or {}
    github = action_scope.get("native_github")
    if not isinstance(github, dict) or not _projection_matches(github):
        return None
    if any(row["provider"] != "github" for row in preceding):
        return None
    pr = source_github_publication.confirmed_pr(conn, root_id, preceding[-1]["action_id"])
    if pr is None or pr[1].get("audit_scope_sha256") != context_digest({**action_scope, "output_type": output_type}):
        return None
    try:
        linked = tapd_policy.append_confirmed_pr_urls(body, paths, (pr[0].url,))
        return f"{linked}\n\n{github['body']}"
    except (KeyError, ValueError):
        return None


def github_effect_metadata(action, scope):
    """Overlay accepted Git facts on an original selector without mutating it."""
    if (
        not isinstance(scope, dict)
        or action.get("provider") != "github"
        or action.get("audit_status") != AuditStatus.ACCEPTED
    ):
        return None
    proof, target = scope.get("native_github"), json_object(action.get("target_json"))
    saved = _candidate_metadata(action, {"target_id"})
    kind = action.get("action_type")
    if (
        not isinstance(proof, dict)
        or not _projection_matches(proof)
        or saved is None
        or not isinstance(saved["target_id"], str)
        or {"provider": "github", "type": kind, "target_id": saved["target_id"]} not in (proof.get("selectors") or ())
        or kind not in {GitHubActionType.PUSH_EXISTING_PR, GitHubActionType.OPEN_PR}
        or not isinstance(target, dict)
        or target.get("repository") != scope.get("repo")
        or target.get("host") != "github.com"
        or target.get("provider") != "github"
    ):
        return None
    purpose = proof.get("purpose") or {}
    binding = purpose.get("git_pin") or {}
    pr = purpose.get("pr") or {}
    if purpose.get("mode") == "existing_pr":
        if (
            kind != GitHubActionType.PUSH_EXISTING_PR
            or target.get("kind") != "pull_request"
            or (target.get("external_id") != str(pr.get("number")))
        ):
            return None
    elif purpose.get("mode") != "new_branch" or target.get("kind") != "repository":
        return None
    try:
        return {
            "target_id": saved["target_id"],
            "repo": scope["repo"],
            "remote": scope["remote"],
            "worktree_path": scope["worktree_path"],
            "branch": scope["branch_name"],
            "head": scope["branch_name"],
            "head_repository": binding["push_repository"],
            "head_sha": proof["head_sha"],
            "base": binding["base_branch"],
            "base_sha": binding["base_sha"],
            **({"pr_number": pr["number"], "expected_remote_sha": binding["initial_head"]} if pr else {}),
            **({"title": proof["title"], "body": proof["body"]} if kind == GitHubActionType.OPEN_PR else {}),
        }
    except (KeyError, TypeError):
        return None
