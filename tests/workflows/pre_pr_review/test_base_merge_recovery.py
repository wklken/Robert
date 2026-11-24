"""Only a pinned, exact base merge can survive an interrupted snapshot save."""

import json
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import patch

from robert_agent.execution.stages import StageStopped, attempt_is_active
from robert_agent.persistence.rounds import pre_pr_review as store
from robert_agent.persistence.schema import pre_pr_review as schema
from robert_agent.workflows.pre_pr_review import coordinator as module
from robert_agent.workflows.review import workspace
from tests.support import database as fixture_database
from tests.support.review_workspace import ReviewWorkspaceFixture


class BaseMergeRecoveryTests(ReviewWorkspaceFixture):
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
        self.prompt = self.root / "original.md"
        self.prompt.write_text("Implement the original request\n")
        self.event = {"event_fingerprint": "original-event"}
        self.candidate = {
            "head": self.head,
            "candidate_path": str(self.prompt),
            "candidate_sha256": store.digest(self.prompt),
        }
        manifest = workspace.prepare_candidate_snapshot(
            self.repo,
            self.tree.parent,
            self.root / "artifacts",
            self.round_id,
            "example/backend",
            "main",
            self.base,
            self.head,
            self.event,
        )
        manifest["candidate"] = self.candidate
        with store.connect(self.db) as conn:
            store.save_snapshot(conn, self.round_id, manifest)
            store.update(conn, self.round_id, proof_json=json.dumps({"old": "publication proof"}))
        self.driver = SimpleNamespace(
            db_path=self.db,
            task_id=self.task_id,
            attempt_id=self.attempt_id,
            round_id=self.round_id,
            manifest=manifest,
            request={
                "retried_from_task": "original-task",
                "prompt_path": str(self.prompt),
                "prompt_sha256": store.digest(self.prompt),
                "event": self.event,
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
            phase=lambda _: None,
            prepare_context=lambda: None,
        )
        for name in (
            "prepare",
            "preserve_completed_repair",
            "merge_current_base",
            "verify_base_merge",
            "save_manifest",
            "check_base",
            "address_workspace",
        ):
            setattr(self.driver, name, MethodType(getattr(module.Coordinator, name), self.driver))
        self.driver.check_active = self.check_active
        self.git(self.upstream, "checkout", "main")
        (self.upstream / "base_feature.py").write_text("upstream_feature = True\n")
        self.git(self.upstream, "add", ".")
        self.git(self.upstream, "commit", "-m", "new upstream runtime")
        self.current_base = self.git(self.upstream, "rev-parse", "HEAD")
        self.git(self.repo, "fetch", "upstream", self.current_base)
        self.driver.observe_base = lambda *_: self.current_base

    def check_active(self):
        with store.connect(self.db) as conn:
            if not attempt_is_active(conn, self.task_id, self.attempt_id):
                raise StageStopped("owner changed")

    def entry(self):
        with store.connect(self.db) as conn:
            return store.session(conn, task_id=self.task_id)

    def completed_merge(self, first=None):
        first = first or self.head
        self.git(self.tree, "merge", "--no-ff", "--no-edit", self.current_base)
        self.assertEqual(self.git(self.tree, "show", "-s", "--format=%P", "HEAD").split(), [first, self.current_base])
        return self.git(self.tree, "rev-parse", "HEAD")

    def test_explicit_retrigger_adopts_exact_operator_merge_and_rebuilds_snapshot(self):
        merged = self.completed_merge()
        old = self.entry()["snapshot_id"]
        self.driver.prepare()
        saved = self.entry()
        self.assertEqual(saved["manifest"]["head_sha"], merged)
        self.assertEqual(saved["manifest"]["base_sha"], self.current_base)
        self.assertNotEqual(saved["snapshot_id"], old)
        self.assertEqual(saved["proof"], {})
        self.assertNotIn("base_merge", saved["request"])
        self.assertEqual(saved["request"]["base_refresh_count"], 1)
        self.assertTrue((Path(saved["manifest"]["worktree_path"]) / "base_feature.py").exists())

    def test_merge_checkpoint_survives_commit_before_snapshot_save(self):
        native_git = module.git

        def interrupted_git(path, *args, **kwargs):
            result = native_git(path, *args, **kwargs)
            if args[:3] == ("merge", "--no-ff", "--no-edit"):
                self.assertIn("base_merge", self.entry()["request"])
                raise StageStopped("crash after commit")
            return result

        with patch.object(module, "git", side_effect=interrupted_git):
            with self.assertRaisesRegex(StageStopped, "crash after commit"):
                self.driver.prepare()
        checkpoint = self.entry()["request"]["base_merge"]
        merged = self.git(self.tree, "rev-parse", "HEAD")
        self.driver.request.pop("retried_from_task", None)
        with store.connect(self.db) as conn:
            request = self.entry()["request"]
            request.pop("retried_from_task", None)
            store.update(conn, self.round_id, request_json=json.dumps(request))
        self.driver.prepare()
        self.assertEqual(self.entry()["manifest"]["head_sha"], merged)
        self.assertEqual(self.entry()["request"]["base_refresh_count"], 1)
        self.assertEqual(checkpoint["candidate_sha"], self.head)

    def test_completed_merge_without_explicit_retrigger_is_rejected(self):
        self.completed_merge()
        with store.connect(self.db) as conn:
            store.update(conn, self.round_id, request_json="{}")
        self.driver.request.pop("retried_from_task")
        with self.assertRaises(StageStopped):
            self.driver.prepare()

    def test_explicit_retrigger_rejects_additional_source_commit(self):
        self.completed_merge()
        (self.tree / "extra.py").write_text("unauthorized = True\n")
        self.git(self.tree, "add", ".")
        self.git(self.tree, "commit", "-m", "extra source")
        with self.assertRaises(StageStopped):
            self.driver.prepare()

    def test_explicit_retrigger_preserves_authenticated_repair_before_completed_merge(self):
        report = self.root / "report.md"
        report.write_text("Registered review findings\n")
        with store.connect(self.db) as conn:
            store.update(conn, self.round_id, report_path=str(report), report_sha256=store.digest(report))
            run = store.new_run(conn, self.round_id, "address_findings", "writer", self.attempt_id, self.root, "prompt")
        address = self.driver.address_workspace(run["run_id"])
        address.parent.mkdir(parents=True)
        self.git(self.repo, "worktree", "add", "--detach", str(address), self.head)
        (address / "source.py").write_text("value = 4\n")
        self.git(address, "commit", "-am", "completed authenticated repair")
        repaired = self.git(address, "rev-parse", "HEAD")
        dispositions = self.root / "dispositions.json"
        dispositions.write_text(
            json.dumps(
                {
                    "candidate_sha": self.head,
                    "final_sha": repaired,
                    "report_sha256": store.digest(report),
                }
            )
        )
        with store.connect(self.db) as conn:
            store.update_run(
                conn,
                run["run_id"],
                status="succeeded",
                artifact_json=json.dumps(
                    {
                        "head": repaired,
                        "dispositions_path": str(dispositions),
                        "dispositions_sha256": store.digest(dispositions),
                    }
                ),
            )
        self.git(self.tree, "merge", "--ff-only", repaired)
        merged = self.completed_merge(repaired)
        original = dispositions.read_bytes()
        dispositions.write_text("changed evidence")
        with self.assertRaisesRegex(StageStopped, "completed repair evidence changed"):
            self.driver.prepare()
        dispositions.write_bytes(original)
        self.driver.prepare()
        self.assertEqual(self.entry()["manifest"]["head_sha"], merged)
        self.assertEqual((self.tree / "source.py").read_text(), "value = 4\n")
        self.assertEqual((self.tree / "base_feature.py").read_text(), "upstream_feature = True\n")
        self.assertEqual(self.entry()["manifest"]["candidate"]["candidate_sha256"], self.candidate["candidate_sha256"])
        self.assertEqual(self.entry()["proof"], {})

    def test_explicit_retrigger_rejects_forged_parents_tree_and_dirty_worktree(self):
        merged = self.completed_merge()
        tree = self.git(self.tree, "rev-parse", "HEAD^{tree}")
        reversed_merge = self.git(
            self.tree, "commit-tree", tree, "-p", self.current_base, "-p", self.head, "-m", "merge"
        )
        self.git(self.tree, "update-ref", "refs/heads/assigned", reversed_merge)
        with self.assertRaisesRegex(StageStopped, "exact intended base merge"):
            self.driver.prepare()
        self.git(self.tree, "update-ref", "refs/heads/assigned", merged)
        (self.tree / "source.py").write_text("value = 99\n")
        with self.assertRaises(StageStopped):
            self.driver.prepare()
        self.git(self.tree, "commit", "--amend", "--no-edit", "-a")
        self.assertEqual(
            self.git(self.tree, "show", "-s", "--format=%P", "HEAD").split(), [self.head, self.current_base]
        )
        with self.assertRaisesRegex(StageStopped, "exact intended base merge"):
            self.driver.prepare()
        self.assertNotIn("base_merge", self.entry()["request"])

    def test_budget_is_bounded_and_saved_checkpoint_cannot_change(self):
        with store.connect(self.db) as conn:
            store.update(
                conn, self.round_id, request_json=json.dumps({"base_refresh_count": module.retry.MAX_ATTEMPTS})
            )
        with self.assertRaisesRegex(StageStopped, "budget exhausted"):
            self.driver.prepare()
        self.assertEqual(self.git(self.tree, "rev-parse", "HEAD"), self.head)
        with store.connect(self.db) as conn:
            store.update(conn, self.round_id, request_json="{}")
        native_git = module.git

        def stop_before_merge(path, *args, **kwargs):
            if args[:3] == ("merge", "--no-ff", "--no-edit"):
                raise StageStopped("before merge")
            return native_git(path, *args, **kwargs)

        with patch.object(module, "git", side_effect=stop_before_merge):
            with self.assertRaisesRegex(StageStopped, "before merge"):
                self.driver.prepare()
        saved = self.entry()["request"]
        with store.connect(self.db) as conn:
            request = json.loads(json.dumps(saved))
            request["base_merge"]["tree_sha"] = "0" * 40
            store.update(conn, self.round_id, request_json=json.dumps(request))
        with self.assertRaisesRegex(StageStopped, "checkpoint changed"):
            self.driver.prepare()
        self.assertEqual(self.git(self.tree, "rev-parse", "HEAD"), self.head)
        with store.connect(self.db) as conn:
            store.update(conn, self.round_id, request_json=json.dumps(saved))
        self.driver.prepare()
        self.assertEqual(self.entry()["request"]["base_refresh_count"], 1)
        self.assertNotIn("base_merge", self.entry()["request"])
