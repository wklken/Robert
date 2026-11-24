"""Mixed certificates cover source, packaging and executable CI dependencies."""

from copy import deepcopy
import json
import subprocess

from robert_agent.adapters.git.review_revision import git
from robert_agent.delivery.application import reviewed_pr
from robert_agent.delivery.base_revisions import compatibility_refresh
from tests.support.review_workspace import ReviewWorkspaceFixture


class MixedBaseDependenciesTests(ReviewWorkspaceFixture):
    def setUp(self):
        super().setUp()
        git(self.upstream, "checkout", "main")
        (self.upstream / "source.py").unlink()
        self.files = {
            "src/backend/pyproject.toml": '[project]\nname = "backend"\nversion = "1.0"\ndependencies = []\n',
            "src/backend/service/__init__.py": '"""Backend package."""\n',
            "src/backend/service/value.py": "value = 1\n",
            "src/backend/service/api.py": "from .value import value\n",
            "src/frontend/package.json": json.dumps(
                {
                    "name": "frontend",
                    "private": True,
                    "type": "module",
                    "scripts": {"check": "node --check src/value.js"},
                }
            ),
            "src/frontend/src/value.js": "export const value = 1;\n",
            "src/frontend/src/view.js": "import { value } from './value.js';\n",
            ".github/workflows/backend.yml": self.workflow("backend", "python -m compileall service"),
            ".github/workflows/frontend.yml": self.workflow("frontend", "node --check src/value.js"),
        }
        self.write(self.files)
        git(self.upstream, "add", ".")
        git(self.upstream, "commit", "-m", "mixed base")
        self.base = git(self.upstream, "rev-parse", "HEAD")
        git(self.upstream, "checkout", "-B", "feature", self.base)
        self.write({"src/backend/service/value.py": "value = 2\n"})
        git(self.upstream, "commit", "-am", "review backend")
        self.head = git(self.upstream, "rev-parse", "HEAD")
        git(self.repo, "fetch", "upstream", self.head)

    @staticmethod
    def workflow(component, command):
        return f"""name: {component}
on:
  pull_request:
    paths: ['src/{component}/**']
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: {command}
        working-directory: src/{component}
"""

    def write(self, files):
        for name, text in files.items():
            target = self.upstream / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)

    def advance(self, files):
        git(self.upstream, "checkout", "main")
        self.write(files)
        git(self.upstream, "add", ".")
        git(self.upstream, "commit", "-m", "advance base")
        current = git(self.upstream, "rev-parse", "HEAD")
        git(self.repo, "fetch", "upstream", current)
        return current

    def certificate(self, current):
        return compatibility_refresh(
            self.repo, self.base, current, self.head, round_id="round", snapshot_id="snapshot", candidate=self.head
        )

    def scope(self, certificate):
        return {
            "repo": "example/mixed",
            "worktree_path": str(self.repo),
            "base_branch": "main",
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

    def test_nested_backend_review_reuses_independent_frontend_scalar_source(self):
        current = self.advance({"src/frontend/src/value.js": "export const value = 3;\n"})
        proof = self.certificate(current)
        self.assertIsNotNone(proof)
        self.assertEqual(proof["kind"], "closed_mixed_dependencies")
        self.assertIn("src/backend/service/api.py", proof["dependency_certificate"]["reviewed_component"])
        self.assertIn(".github/workflows/backend.yml", proof["dependency_certificate"]["reviewed_component"])
        self.assertTrue(reviewed_pr.base_matches(self.scope(proof), current, subprocess.run))
        self.assertEqual(git(self.repo, "rev-parse", self.head), self.head)

    def test_frontend_relative_import_into_backend_or_unknown_package_refuses(self):
        for text in (
            "import { value } from '../../backend/service/value.py';\n",
            "import { value } from 'runtime';\n",
            "const value = import('./value.js');\n",
        ):
            with self.subTest(text=text):
                current = self.advance({"src/frontend/src/value.js": text})
                self.assertIsNone(self.certificate(current))

    def test_package_install_hooks_dev_dependencies_and_build_changes_refuse(self):
        for field, value in (
            ("scripts", {"check": "npm install && node --check src/value.js"}),
            ("devDependencies", {"playwright": "1.0"}),
            ("dependencies", {"api-schema": "file:../backend"}),
        ):
            package = json.loads(self.files["src/frontend/package.json"])
            package[field] = value
            with self.subTest(field=field):
                current = self.advance({"src/frontend/package.json": json.dumps(package)})
                self.assertIsNone(self.certificate(current))

    def test_global_rules_schema_unknown_config_and_dynamic_input_refuse(self):
        for name, text in (
            ("AGENTS.md", "Cross component acceptance\n"),
            ("src/frontend/AGENTS.md", "Apply new acceptance globally\n"),
            ("schema.json", '{"type":"string"}'),
            ("src/frontend/vite.config.js", "export default process.env;\n"),
            ("src/backend/runtime.yml", "frontend: ../frontend\n"),
            ("src/backend/service/__init__.py", "import importlib\n"),
        ):
            with self.subTest(name=name):
                current = self.advance({name: text})
                self.assertIsNone(self.certificate(current))

    def test_backend_gate_or_workflow_path_filter_change_refuses(self):
        for component, command in (
            ("backend", "node ../frontend/src/value.js"),
            ("frontend", "python ../backend/service/api.py"),
        ):
            current = self.advance({f".github/workflows/{component}.yml": self.workflow(component, command)})
            self.assertIsNone(self.certificate(current))
        changed = self.files[".github/workflows/frontend.yml"].replace("src/frontend/**", "src/**")
        current = self.advance({".github/workflows/frontend.yml": changed})
        self.assertIsNone(self.certificate(current))

    def test_publisher_recomputes_all_source_and_descriptor_hashes(self):
        current = self.advance({"src/frontend/src/value.js": "export const value = 3;\n"})
        proof = self.certificate(current)
        self.assertIsNotNone(proof)
        changed = deepcopy(proof)
        evidence = changed["dependency_certificate"]["revisions"][self.base]
        evidence["source_sha256"][".github/workflows/backend.yml"] = "0" * 64
        self.assertFalse(reviewed_pr.base_matches(self.scope(changed), current, subprocess.run))
        newer = self.advance(
            {"src/backend/pyproject.toml": '[project]\nname="backend"\nversion="2.0"\ndependencies=[]\n'}
        )
        self.assertFalse(reviewed_pr.base_matches(self.scope(proof), newer, subprocess.run))

    def test_disjoint_python_caller_is_in_same_component(self):
        current = self.advance({"src/backend/service/consumer.py": "from .api import value\n"})
        self.assertIsNone(self.certificate(current))

    def test_malformed_workflow_and_duplicate_json_are_refused_without_exception(self):
        current = self.advance({".github/workflows/frontend.yml": "jobs: [unterminated\n"})
        self.assertIsNone(self.certificate(current))
        current = self.advance(
            {"src/frontend/package.json": '{"name":"frontend","name":"forged","private":true,"type":"module"}'}
        )
        self.assertIsNone(self.certificate(current))

    def test_reassigned_import_or_invalid_js_binding_requires_review(self):
        for text in (
            "export const value = 1; export const value = 2;\n",
            "import { value } from './view.js'; export const value = 2;\n",
        ):
            current = self.advance({"src/frontend/src/value.js": text})
            self.assertIsNone(self.certificate(current))

    def test_backend_initializer_import_to_frontend_requires_review(self):
        current = self.advance({"src/backend/service/__init__.py": "from frontend import value\n"})
        self.assertIsNone(self.certificate(current))

    def test_static_component_build_inputs_participate_in_certificate(self):
        git(self.upstream, "checkout", "main")
        git(self.upstream, "reset", "--hard", self.base)
        self.write({"src/frontend/Makefile": "check:\n\tnode --check src/value.js\n"})
        git(self.upstream, "add", ".")
        git(self.upstream, "commit", "-m", "static build descriptor")
        self.base = git(self.upstream, "rev-parse", "HEAD")
        git(self.upstream, "checkout", "-B", "feature", self.base)
        self.write({"src/backend/service/value.py": "value = 2\n"})
        git(self.upstream, "commit", "-am", "backend review")
        self.head = git(self.upstream, "rev-parse", "HEAD")
        git(self.repo, "fetch", "upstream", self.head)
        current = self.advance({"src/frontend/src/value.js": "export const value = 3;\n"})
        proof = self.certificate(current)
        self.assertIsNotNone(proof)
        self.assertIn("src/frontend/Makefile", proof["dependency_certificate"]["revisions"][self.base]["source_sha256"])
        current = self.advance({"src/frontend/Makefile": "check:\n\tnpm install\n"})
        self.assertIsNone(self.certificate(current))
