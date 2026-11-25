"""Select one independent final check when first-round evidence warrants it."""

from pathlib import PurePosixPath

from robert_agent.adapters.git.review_revision import git
from robert_agent.workflows.review import files


_SENSITIVE_PARTS = {
    "auth",
    "authorization",
    "credentials",
    "security",
    "secrets",
    "migrations",
    "persistence",
    "permissions",
    "sandbox",
    "supervision",
}
_SENSITIVE_STEMS = {"auth", "authn", "authz", "locking", "lease", "fencing", "policy", "schema", "permissions"}


def reasons(driver, records, disposition):
    merged = files.merge_reviewer_results(driver.manifest, records)
    result = []
    if any(
        source["finding"]["severity"] in {"critical", "high"} for item in merged["items"] for source in item["sources"]
    ):
        result.append("critical_or_high_finding")
    if any(
        reviewer["requires_judgment"] or reviewer["merge_recommendation"] == "needs significant rework"
        for reviewer in merged["reviewers"]
    ):
        result.append("material_judgment_or_coverage_gap")
    head, final = driver.manifest["head_sha"], disposition["head"]
    coverage = [reviewer["coverage"] for reviewer in merged["reviewers"]]
    reviewed = {name for item in coverage for name in item["reviewed_paths"]}
    unreviewed = {name for item in coverage for name in item["unreviewed_paths"]} - reviewed
    if unreviewed:
        changed = git(
            driver.manifest["worktree_path"],
            "diff",
            "--name-only",
            "--no-renames",
            "-z",
            driver.manifest["merge_base_sha"],
            head,
            "--",
        ).split("\0")
        result.extend(f"unreviewed_changed_path: {name}" for name in sorted(unreviewed.intersection(changed)))
    if head != final:
        tree = driver.address_workspace(disposition["run_id"])
        changed = git(tree, "diff", "--name-only", "--no-renames", "-z", head, final, "--").split("\0")
        paths = [PurePosixPath(name.lower()) for name in changed if name]
        if any(set(path.parts[:-1]) & _SENSITIVE_PARTS or path.stem in _SENSITIVE_STEMS for path in paths):
            result.append("sensitive_repair_paths")
    return result
