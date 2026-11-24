"""Base movement requires proof of documentation-only, conflict-free changes."""

import json
import hashlib
import subprocess

from robert_agent.adapters.git.review_revision import git
from robert_agent.delivery.application import reviewed_pr
from robert_agent.delivery.base_revisions import documentation_refresh
from tests.support.review_workspace import ReviewWorkspaceFixture


class ReviewedBaseRevisionTests(ReviewWorkspaceFixture):
    def advance(self, path, content):
        git(self.upstream, "checkout", "main")
        target = self.upstream / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        git(self.upstream, "add", path)
        git(self.upstream, "commit", "-m", "advance base")
        current = git(self.upstream, "rev-parse", "HEAD")
        git(self.repo, "fetch", "upstream", current)
        return current

    def scope(self):
        return {
            "repo": "example/backend",
            "worktree_path": str(self.repo),
            "base_branch": "main",
            "branch_name": "feature",
            "pre_pr_review": {"base_sha": self.base, "head_sha": self.head},
        }

    def test_docs_advance_can_be_verified_at_publication(self):
        current = self.advance("docs/unrelated.md", "Documentation only\n")
        proof = documentation_refresh(self.repo, self.base, current, self.head)
        self.assertEqual(proof["upstream_paths"], ["docs/unrelated.md"])
        self.assertEqual(proof["base_sha"], current)
        self.assertTrue(reviewed_pr.base_matches(self.scope(), current, subprocess.run))

    def test_disjoint_source_configuration_and_rules_require_review(self):
        for path in ("consumer.py", "pyproject.toml", "docs/AGENTS.md", "docs/runtime.yml"):
            with self.subTest(path=path):
                current = self.advance(path, "changed\n")
                self.assertIsNone(documentation_refresh(self.repo, self.base, current, self.head))
                self.assertFalse(reviewed_pr.base_matches(self.scope(), current, subprocess.run))

    def test_changed_source_is_not_waived(self):
        current = self.advance("source.py", "value = 3\n")
        self.assertIsNone(documentation_refresh(self.repo, self.base, current, self.head))

    def test_remote_pr_matches_a_verified_documentation_advance(self):
        current = self.advance("docs/unrelated.md", "Documentation only\n")
        scope = self.scope()
        body = "Reviewed result"
        scope["pre_pr_review"]["body_sha256"] = hashlib.sha256(body.encode()).hexdigest()
        detail = {
            "number": 42,
            "body": body,
            "head": {"sha": self.head, "ref": "feature", "repo": {"full_name": "example/backend"}},
            "base": {"sha": current, "ref": "main", "repo": {"full_name": "example/backend"}},
        }

        def runner(command, **kwargs):
            if command[0] == "gh":
                return subprocess.CompletedProcess(command, 0, stdout=json.dumps(detail))
            return subprocess.run(command, **kwargs)

        self.assertTrue(reviewed_pr.matches_remote_pr(scope, {"body": body}, 42, runner))
