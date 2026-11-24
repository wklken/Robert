"""Base certificates require complete dependencies and exact publication binding."""

from copy import deepcopy
import subprocess

from robert_agent.adapters.git.review_revision import git
from robert_agent.delivery.application import reviewed_pr
from robert_agent.delivery.base_revisions import compatibility_refresh
from tests.support.review_workspace import ReviewWorkspaceFixture


class BaseCompatibilityTests(ReviewWorkspaceFixture):
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

    def certificate(self, current):
        return compatibility_refresh(
            self.repo,
            self.base,
            current,
            self.head,
            round_id="round",
            snapshot_id="snapshot",
            candidate=self.head,
        )

    def scope(self, certificate):
        return {
            "repo": "example/backend",
            "worktree_path": str(self.repo),
            "base_branch": "main",
            "branch_name": "feature",
            "pre_pr_review": {
                "round_id": "round",
                "snapshot_id": "snapshot",
                "candidate_sha": self.head,
                "head_sha": self.head,
                "reviewed_base_sha": self.base,
                "base_sha": certificate["base_sha"],
                "base_refresh": certificate,
            },
        }

    def test_closed_unrelated_source_has_complete_dependency_certificate(self):
        current = self.advance("unrelated.py", "independent = 3\n")
        proof = self.certificate(current)
        self.assertEqual(proof["kind"], "closed_python_dependencies")
        self.assertEqual(proof["dependency_certificate"]["reviewed_component"], ["source.py"])
        self.assertEqual(proof["upstream_paths"], ["unrelated.py"])
        self.assertTrue(reviewed_pr.base_matches(self.scope(proof), current, subprocess.run))

    def test_caller_dependency_is_affected_despite_disjoint_paths(self):
        current = self.advance("consumer.py", "from source import value\nbase_feature = True\n")
        self.assertIsNone(self.certificate(current))

    def test_dependency_chain_to_caller_is_affected(self):
        self.advance("intermediate.py", "from source import value\n")
        current = self.advance("consumer.py", "from intermediate import value\n")
        self.assertIsNone(self.certificate(current))

    def test_missing_imported_symbol_refuses_disjoint_source(self):
        self.advance("unrelated.py", "independent = 3\n")
        current = self.advance("consumer.py", "from unrelated import missing\n")
        self.assertIsNone(self.certificate(current))

    def test_disjoint_dependency_cycle_requires_review(self):
        self.advance("left.py", "import right\nvalue = 1\n")
        current = self.advance("right.py", "import left\nvalue = 2\n")
        self.assertIsNone(self.certificate(current))

    def test_configuration_rules_and_unknown_dynamic_inputs_require_review(self):
        for path, content in (
            ("pyproject.toml", "[project]\nname = 'runtime'\n"),
            ("runtime.yml", "enabled: true\n"),
            ("AGENTS.md", "New repository rules\n"),
            ("docs/AGENTS.md", "New review rules\n"),
            ("docs/runtime.yml", "enabled: true\n"),
            ("consumer.py", "print('side effect')\n"),
            ("dynamic.py", "import os\n"),
        ):
            with self.subTest(path=path):
                current = self.advance(path, content)
                self.assertIsNone(self.certificate(current))

    def test_infinite_literal_and_python_metadata_require_review(self):
        current = self.advance("unrelated.py", "value = 1e999\n")
        self.assertIsNone(self.certificate(current))
        current = self.advance("unrelated.py", "__path__ = 'runtime'\n")
        self.assertIsNone(self.certificate(current))

    def test_tampered_certificate_is_rejected_even_when_base_equals_saved_proof(self):
        current = self.advance("docs/unrelated.md", "Documentation\n")
        certificate = self.certificate(current)
        self.assertTrue(reviewed_pr.base_matches(self.scope(certificate), current, subprocess.run))
        for field in (
            "round_id",
            "snapshot_id",
            "candidate_sha",
            "reviewed_base_sha",
            "base_sha",
            "final_sha",
            "merge_tree_sha",
            "kind",
            "upstream_paths",
        ):
            with self.subTest(field=field):
                changed = deepcopy(certificate)
                changed[field] = ["forged.py"] if field == "upstream_paths" else "forged"
                scope = self.scope(changed)
                scope["pre_pr_review"]["base_sha"] = current
                self.assertFalse(reviewed_pr.base_matches(scope, current, subprocess.run))
        changed = deepcopy(certificate)
        changed["extra"] = "forged"
        self.assertFalse(reviewed_pr.base_matches(self.scope(changed), current, subprocess.run))

    def test_tampered_dependency_hash_is_rejected(self):
        current = self.advance("unrelated.py", "independent = 3\n")
        proof = self.certificate(current)
        proof["dependency_certificate"]["revisions"][self.head]["source_sha256"]["source.py"] = "0" * 64
        self.assertFalse(reviewed_pr.base_matches(self.scope(proof), current, subprocess.run))

    def test_base_advances_after_certificate_docs_then_caller_requires_review(self):
        current = self.advance("docs/unrelated.md", "Documentation\n")
        scope = self.scope(self.certificate(current))
        newer = self.advance("docs/another.md", "More documentation\n")
        self.assertTrue(reviewed_pr.base_matches(scope, newer, subprocess.run))
        newer = self.advance("consumer.py", "from source import value\n")
        self.assertFalse(reviewed_pr.base_matches(scope, newer, subprocess.run))

    def test_historic_docs_certificate_is_recomputed_and_unknown_version_is_refused(self):
        current = self.advance("docs/unrelated.md", "Documentation\n")
        certificate = self.certificate(current)
        legacy = {
            key: value
            for key, value in certificate.items()
            if key not in {"version", "kind", "round_id", "snapshot_id", "candidate_sha"}
        }
        scope = self.scope(certificate)
        scope["pre_pr_review"] = {"head_sha": self.head, "base_sha": current, "base_refresh": legacy}
        self.assertTrue(reviewed_pr.base_matches(scope, current, subprocess.run))
        legacy["merge_tree_sha"] = "0" * 40
        self.assertFalse(reviewed_pr.base_matches(scope, current, subprocess.run))
        certificate["version"] = 2
        self.assertFalse(reviewed_pr.base_matches(self.scope(certificate), current, subprocess.run))

    def test_missing_compatibility_certificate_refuses_advanced_base_even_if_current_equals_proof(self):
        current = self.advance("docs/unrelated.md", "Documentation\n")
        scope = self.scope(self.certificate(current))
        scope["pre_pr_review"].pop("base_refresh")
        scope["pre_pr_review"]["base_refresh_count"] = 0
        self.assertFalse(reviewed_pr.base_matches(scope, current, subprocess.run))

    def test_forged_refresh_count_never_substitutes_for_exact_certificate(self):
        current = self.advance("docs/unrelated.md", "Documentation\n")
        scope = self.scope(self.certificate(current))
        scope["pre_pr_review"]["base_refresh_count"] = -1
        scope["pre_pr_review"]["base_refresh"]["merge_tree_sha"] = "0" * 40
        self.assertFalse(reviewed_pr.base_matches(scope, current, subprocess.run))

    def test_proof_and_certificate_scope_must_agree(self):
        current = self.advance("docs/unrelated.md", "Documentation\n")
        certificate = self.certificate(current)
        for key in ("round_id", "snapshot_id", "candidate_sha", "reviewed_base_sha", "head_sha"):
            with self.subTest(key=key):
                scope = self.scope(deepcopy(certificate))
                scope["pre_pr_review"][key] = "forged"
                self.assertFalse(reviewed_pr.base_matches(scope, current, subprocess.run))

    def test_invalid_leading_indentation_is_not_normalized_into_valid_source(self):
        current = self.advance("unrelated.py", "    independent = 3\n")
        self.assertIsNone(self.certificate(current))

    def test_bool_protocol_version_does_not_match_integer_certificate_version(self):
        current = self.advance("docs/unrelated.md", "Documentation\n")
        certificate = self.certificate(current)
        certificate["version"] = True
        self.assertFalse(reviewed_pr.base_matches(self.scope(certificate), current, subprocess.run))

    def test_historic_certificate_must_match_explicit_reviewed_base_when_present(self):
        current = self.advance("docs/unrelated.md", "Documentation\n")
        certificate = self.certificate(current)
        legacy = {
            key: value
            for key, value in certificate.items()
            if key not in {"version", "kind", "round_id", "snapshot_id", "candidate_sha"}
        }
        scope = self.scope(certificate)
        scope["pre_pr_review"]["base_refresh"] = legacy
        scope["pre_pr_review"]["reviewed_base_sha"] = "forged"
        self.assertFalse(reviewed_pr.base_matches(scope, current, subprocess.run))
