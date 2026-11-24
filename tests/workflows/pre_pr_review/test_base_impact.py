"""A compatible base must preserve the original frozen review generation."""

from copy import deepcopy
import json
import hashlib
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import Mock, patch

from robert_agent.execution.stages import StageStopped
from robert_agent.persistence.rounds import pre_pr_review as store
from robert_agent.persistence.schema import pre_pr_review as schema
from robert_agent.workflows.pre_pr_review import coordinator as module
from robert_agent.workflows.review import workspace
from tests.support import database as fixture_database
from tests.support.review_workspace import ReviewWorkspaceFixture


class BaseImpactTests(ReviewWorkspaceFixture):
    def setUp(self):
        super().setUp()
        self.db = self.root / "review.sqlite3"
        fixture = Path(__file__).parents[2] / "fixtures/persistence/pre_orm_5e0e952.sql"
        with fixture_database.connect(self.db) as conn:
            conn.executescript(fixture.read_text())
            schema.initialize(conn)
            self.task_id, self.attempt_id = conn.execute("SELECT task_id,attempt_id FROM attempts").fetchone()
            store.create(conn, self.task_id, {"retried_from_task": "original-task"})
            self.round_id = store.session(conn, task_id=self.task_id)["round_id"]
        self.tree = self.root / "worktrees" / "assigned"
        self.tree.parent.mkdir()
        self.git(self.repo, "worktree", "add", str(self.tree), "-b", "assigned", self.head)
        self.git(self.repo, "config", "user.name", "Test")
        self.git(self.repo, "config", "user.email", "test@example.invalid")
        original = self.root / "original.md"
        original.write_text("Implement the original request\n")
        pinned_path = self.root / "artifacts" / self.round_id / f"original-request-{store.digest(original)}.md"
        pinned_path.parent.mkdir(parents=True)
        pinned_path.write_bytes(original.read_bytes())
        pinned = {"path": str(pinned_path), "sha256": store.digest(original)}
        candidate = {"head": self.head, "candidate_path": str(original), "candidate_sha256": store.digest(original)}
        event = {"event_fingerprint": "original-event"}
        manifest = workspace.prepare_candidate_snapshot(
            self.repo,
            self.tree.parent,
            self.root / "artifacts",
            self.round_id,
            "example/backend",
            "main",
            self.base,
            self.head,
            event,
            original_request=pinned,
        )
        manifest["candidate"] = candidate
        manifest = self.freeze(manifest)
        with store.connect(self.db) as conn:
            store.save_snapshot(conn, self.round_id, manifest)
        self.driver = SimpleNamespace(
            db_path=self.db,
            task_id=self.task_id,
            attempt_id=self.attempt_id,
            round_id=self.round_id,
            manifest=manifest,
            request={
                "retried_from_task": "original-task",
                "prompt_path": str(original),
                "prompt_sha256": store.digest(original),
                "event": event,
            },
            repo={
                "repo_root": str(self.repo),
                "worktree_root": str(self.tree.parent),
                "full_name": "example/backend",
                "github_account": "robot",
            },
            assigned_path=str(self.tree),
            branch="assigned",
            base="main",
            root=self.root / "artifacts",
            scope={},
            original_request=None,
            final_feedback=None,
            base_refresh=None,
            phase=Mock(),
            prepare_context=Mock(),
            check_active=Mock(),
            run_phase=Mock(side_effect=AssertionError("compatible base started a model")),
        )
        for name in (
            "prepare",
            "preserve_completed_repair",
            "merge_current_base",
            "verify_base_merge",
            "save_manifest",
            "save_candidate_snapshot",
            "check_base",
            "accept_base_refresh",
            "refresh_base",
            "address_workspace",
        ):
            setattr(self.driver, name, MethodType(getattr(module.Coordinator, name), self.driver))
        self.current = self.base
        self.driver.observe_base = lambda *_: self.current

    def entry(self):
        with store.connect(self.db) as conn:
            return store.session(conn, task_id=self.task_id)

    def advance(self, name, text):
        self.git(self.upstream, "checkout", "main")
        target = self.upstream / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        self.git(self.upstream, "add", name)
        self.git(self.upstream, "commit", "-m", "advance base")
        self.current = self.git(self.upstream, "rev-parse", "HEAD")

    def test_docs_base_advance_reuses_frozen_head_snapshot_and_budget_without_launch(self):
        before = deepcopy(self.driver.manifest)
        self.advance("docs/unrelated.md", "Unrelated documentation\n")
        self.driver.prepare()
        self.assertEqual(self.git(self.tree, "rev-parse", "HEAD"), self.head)
        self.assertEqual(self.driver.manifest, before)
        saved = self.entry()
        self.assertEqual(saved["snapshot_id"], before["snapshot_id"])
        self.assertEqual(saved["request"].get("base_refresh_count", 0), 0)
        self.assertNotIn("base_merge", saved["request"])
        self.driver.run_phase.assert_not_called()
        self.assertEqual(self.driver.base_refresh["reviewed_base_sha"], self.base)
        self.assertEqual(self.driver.base_refresh["base_sha"], self.current)
        self.assertEqual(self.driver.base_refresh["round_id"], self.round_id)
        self.assertEqual(self.driver.base_refresh["snapshot_id"], before["snapshot_id"])
        self.assertEqual(self.driver.base_refresh["candidate_sha"], self.head)
        self.assertEqual(saved["request"]["base_compatibility"], self.driver.base_refresh)

    def test_closed_source_no_impact_keeps_frozen_generation(self):
        before = deepcopy(self.driver.manifest)
        self.advance("unrelated.py", "independent = 3\n")
        self.driver.prepare()
        self.assertEqual(self.driver.manifest, before)
        self.assertEqual(self.git(self.tree, "rev-parse", "HEAD"), self.head)
        self.assertEqual(self.entry()["request"].get("base_refresh_count", 0), 0)
        self.assertEqual(self.driver.base_refresh["kind"], "closed_python_dependencies")
        self.driver.run_phase.assert_not_called()

    def test_source_hash_is_verified_before_upstream_observation_or_merge(self):
        self.advance("runtime.yml", "enabled: true\n")
        (Path(self.driver.manifest["worktree_path"]) / "source.py").write_text("value = 99\n")
        self.driver.observe_base = Mock(side_effect=AssertionError("observed before original source verified"))
        with self.assertRaisesRegex(ValueError, "dirty|hash changed"):
            self.driver.prepare()
        self.driver.observe_base.assert_not_called()
        self.assertEqual(self.git(self.tree, "rev-parse", "HEAD"), self.head)
        self.assertEqual(self.entry()["request"].get("base_refresh_count", 0), 0)

    def test_frozen_context_is_verified_before_base_mutation(self):
        self.advance("runtime.yml", "enabled: true\n")
        Path(self.driver.manifest["context_dir"], "diff.patch").write_text("forged context")
        with self.assertRaisesRegex(ValueError, "context file hash changed"):
            self.driver.prepare()
        self.assertEqual(self.git(self.tree, "rev-parse", "HEAD"), self.head)

    def test_original_request_is_not_repaired_or_replaced_on_base_advance(self):
        self.advance("runtime.yml", "enabled: true\n")
        pinned = Path(self.driver.manifest["original_request"]["path"])
        pinned.write_text("forged original input")
        with self.assertRaisesRegex(StageStopped, "pinned original request changed"):
            self.driver.prepare()
        self.assertEqual(pinned.read_text(), "forged original input")
        self.assertEqual(self.git(self.tree, "rev-parse", "HEAD"), self.head)

    def test_candidate_handoff_is_verified_before_branch_mutation(self):
        self.advance("runtime.yml", "enabled: true\n")
        Path(self.driver.manifest["candidate"]["candidate_path"]).write_text("forged handoff")
        with self.assertRaisesRegex(StageStopped, "recorded implementation prompt changed"):
            self.driver.prepare()
        self.assertEqual(self.git(self.tree, "rev-parse", "HEAD"), self.head)

    def test_durable_certificate_tampering_cannot_be_replaced_by_new_observation(self):
        self.advance("docs/unrelated.md", "Documentation\n")
        self.driver.prepare()
        request = self.entry()["request"]
        request["base_compatibility"]["merge_tree_sha"] = "0" * 40
        with store.connect(self.db) as conn:
            store.update(conn, self.round_id, request_json=json.dumps(request))
        self.advance("docs/newer.md", "More documentation\n")
        with self.assertRaisesRegex(StageStopped, "certificate changed"):
            self.driver.prepare()
        self.assertEqual(self.git(self.tree, "rev-parse", "HEAD"), self.head)
        self.assertEqual(self.entry()["request"]["base_compatibility"]["merge_tree_sha"], "0" * 40)

    def test_publication_check_after_base_advances_requires_affected_review(self):
        self.advance("docs/unrelated.md", "Documentation\n")
        self.driver.prepare()
        snapshot = self.driver.manifest["snapshot_id"]
        self.advance("consumer.py", "from source import value\n")
        with self.assertRaises(module.BaseRefreshRequired):
            self.driver.check_base(self.head)
        self.assertEqual(self.git(self.tree, "rev-parse", "HEAD"), self.head)
        self.driver.refresh_base(module.BaseRefreshRequired(self.current))
        self.assertNotEqual(self.driver.manifest["snapshot_id"], snapshot)
        self.assertEqual(self.entry()["request"]["base_refresh_count"], 1)
        self.assertNotIn("base_compatibility", self.entry()["request"])

    def test_configuration_advance_generates_new_review_snapshot(self):
        self.advance("runtime.yml", "enabled: true\n")
        snapshot = self.driver.manifest["snapshot_id"]
        self.driver.prepare()
        self.assertNotEqual(self.driver.manifest["snapshot_id"], snapshot)
        self.assertEqual(self.driver.manifest["base_sha"], self.current)
        self.assertEqual(self.entry()["request"]["base_refresh_count"], 1)
        self.assertNotIn("base_compatibility", self.entry()["request"])

    def test_dirty_assigned_writer_never_receives_a_compatibility_waiver(self):
        self.advance("docs/unrelated.md", "Documentation\n")
        (self.tree / "source.py").write_text("value = 99\n")
        with self.assertRaisesRegex(StageStopped, "dirty_worktree"):
            self.driver.prepare()
        self.assertNotIn("base_compatibility", self.entry()["request"])
        self.assertEqual((self.tree / "source.py").read_text(), "value = 99\n")

    def test_compatible_budget_exhaustion_does_not_consume_refresh_or_merge(self):
        with store.connect(self.db) as conn:
            store.update(
                conn, self.round_id, request_json=json.dumps({"base_refresh_count": module.retry.MAX_ATTEMPTS})
            )
        self.advance("docs/unrelated.md", "Documentation\n")
        self.driver.prepare()
        self.assertEqual(self.entry()["request"]["base_refresh_count"], module.retry.MAX_ATTEMPTS)
        self.assertEqual(self.git(self.tree, "rev-parse", "HEAD"), self.head)

    def test_adopted_failed_repair_preserves_commit_without_rewriting_failure_history(self):
        report = self.root / "report.md"
        report.write_text("Registered report")
        with store.connect(self.db) as conn:
            store.update(conn, self.round_id, report_path=str(report), report_sha256=store.digest(report))
            run = store.new_run(conn, self.round_id, "address_findings", "writer", self.attempt_id, self.root, "prompt")
        address = self.driver.address_workspace(run["run_id"])
        address.parent.mkdir(parents=True)
        self.git(self.repo, "worktree", "add", "--detach", str(address), self.head)
        (address / "source.py").write_text("value = 4\n")
        self.git(address, "commit", "-am", "authenticated repair")
        final = self.git(address, "rev-parse", "HEAD")
        dispositions = self.root / "dispositions.json"
        dispositions.write_text(
            json.dumps({"candidate_sha": self.head, "final_sha": final, "report_sha256": store.digest(report)})
        )
        artifact = {
            "head": final,
            "dispositions_path": str(dispositions),
            "dispositions_sha256": store.digest(dispositions),
        }
        receipt = {
            "run_id": run["run_id"],
            "prompt_sha256": run["prompt_sha256"],
            "original_status": "failed",
            "original_error": "interrupted after output",
            "artifact_sha256": hashlib.sha256(json.dumps(artifact, sort_keys=True).encode()).hexdigest(),
        }
        with store.connect(self.db) as conn:
            store.update_run(
                conn,
                run["run_id"],
                status="failed",
                error=receipt["original_error"],
                artifact_json=json.dumps(artifact),
                dispatch_json=json.dumps({"artifact_adoption": receipt}),
            )
        self.advance("docs/unrelated.md", "Documentation\n")
        self.driver.prepare()
        self.assertEqual(self.git(self.tree, "rev-parse", "HEAD"), self.head)
        self.assertEqual(self.driver.base_refresh["final_sha"], final)
        self.advance("consumer.py", "from source import value\n")
        self.driver.refresh_base(module.BaseRefreshRequired(self.current))
        self.assertEqual((self.tree / "source.py").read_text(), "value = 4\n")
        with store.connect(self.db) as conn:
            saved = next(item for item in store.runs(conn, self.round_id) if item["run_id"] == run["run_id"])
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["error"], receipt["original_error"])
        self.assertEqual(saved["dispatch"]["artifact_adoption"], receipt)
        self.assertTrue(module.Coordinator.preserve_phase_artifact(self.driver, saved))
        altered = deepcopy(saved)
        altered["dispatch"]["artifact_adoption"]["artifact_sha256"] = "0" * 64
        self.assertFalse(module.Coordinator.preserve_phase_artifact(self.driver, altered))

    def test_exact_base_merge_recovery_remains_pinned_and_bounded(self):
        self.advance("consumer.py", "from source import value\n")
        native_git = module.git

        def stop_after_merge(path, *args, **kwargs):
            result = native_git(path, *args, **kwargs)
            if args[:3] == ("merge", "--no-ff", "--no-edit"):
                raise StageStopped("after merge")
            return result

        with patch.object(module, "git", side_effect=stop_after_merge):
            with self.assertRaisesRegex(StageStopped, "after merge"):
                self.driver.prepare()
        merged = self.git(self.tree, "rev-parse", "HEAD")
        checkpoint = self.entry()["request"]["base_merge"]
        self.driver.prepare()
        self.assertEqual(self.driver.manifest["head_sha"], merged)
        self.assertEqual(self.entry()["request"]["base_refresh_count"], 1)
        self.assertEqual(checkpoint["candidate_sha"], self.head)
        self.assertNotIn("base_merge", self.entry()["request"])

    def test_durable_certificate_tamper_blocks_affected_merge_fallback(self):
        self.advance("docs/unrelated.md", "Documentation\n")
        self.driver.prepare()
        request = self.entry()["request"]
        request["base_compatibility"]["merge_tree_sha"] = "0" * 40
        with store.connect(self.db) as conn:
            store.update(conn, self.round_id, request_json=json.dumps(request))
        self.advance("consumer.py", "from source import value\n")
        with self.assertRaisesRegex(StageStopped, "certificate changed"):
            self.driver.prepare()
        self.assertEqual(self.git(self.tree, "rev-parse", "HEAD"), self.head)
        self.assertNotIn("base_merge", self.entry()["request"])
