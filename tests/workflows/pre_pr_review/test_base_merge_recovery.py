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
            "save_candidate_snapshot",
            "check_base",
            "accept_base_refresh",
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

    def test_explicit_retrigger_adopts_earlier_merge_before_newer_upstream(self):
        first_base = self.current_base
        first_merge = self.completed_merge()
        self.git(self.upstream, "checkout", "main")
        (self.upstream / "second_feature.py").write_text(
            "import importlib\nsecond_feature = importlib.import_module('runtime')\n"
        )
        self.git(self.upstream, "add", "second_feature.py")
        self.git(self.upstream, "commit", "-m", "newer upstream while retry waits")
        self.current_base = self.git(self.upstream, "rev-parse", "HEAD")
        snapshots = []
        prepare_snapshot = module.workspace.prepare_candidate_snapshot

        def record_snapshot(*args, **kwargs):
            manifest = prepare_snapshot(*args, **kwargs)
            snapshots.append(manifest)
            return manifest

        with patch.object(module.workspace, "prepare_candidate_snapshot", side_effect=record_snapshot):
            self.driver.prepare()
        self.assertEqual(snapshots[0]["base_sha"], first_base)
        self.assertEqual(snapshots[0]["head_sha"], first_merge)
        entry = self.entry()
        self.assertEqual(entry["manifest"]["base_sha"], self.current_base)
        self.assertEqual(entry["request"]["base_refresh_count"], 2)
        self.assertNotIn("base_merge", entry["request"])
        merged = self.git(self.tree, "rev-parse", "HEAD")
        self.assertEqual(
            self.git(self.tree, "show", "-s", "--format=%P", merged).split(), [first_merge, self.current_base]
        )
        self.assertTrue((self.tree / "second_feature.py").exists())
        self.assertEqual(entry["proof"], {})
        snapshot = entry["snapshot_id"]
        self.driver.prepare()
        self.assertEqual(self.entry()["snapshot_id"], snapshot)
        self.assertEqual(self.entry()["request"]["base_refresh_count"], 2)

    def test_newer_base_reconciles_saved_merge_before_effect(self):
        self.assert_newer_base_reconciles_saved_merge(after_effect=False)

    def test_newer_base_reconciles_saved_merge_after_effect(self):
        self.assert_newer_base_reconciles_saved_merge(after_effect=True)

    def assert_newer_base_reconciles_saved_merge(self, *, after_effect):
        native_git = module.git

        def interrupted_git(path, *args, **kwargs):
            if args[:3] == ("merge", "--no-ff", "--no-edit"):
                if after_effect:
                    native_git(path, *args, **kwargs)
                raise StageStopped("saved first base merge")
            return native_git(path, *args, **kwargs)

        with patch.object(module, "git", side_effect=interrupted_git):
            with self.assertRaisesRegex(StageStopped, "saved first base merge"):
                self.driver.prepare()
        first_base = self.current_base
        saved = self.entry()["request"]["base_merge"]
        self.assertEqual(saved["base_sha"], first_base)
        self.git(self.upstream, "checkout", "main")
        (self.upstream / "second_feature.py").write_text("def new_base():\n    return True\n")
        self.git(self.upstream, "add", "second_feature.py")
        self.git(self.upstream, "commit", "-m", "second upstream advance")
        self.current_base = self.git(self.upstream, "rev-parse", "HEAD")
        snapshots = []
        prepare_snapshot = module.workspace.prepare_candidate_snapshot

        def record_snapshot(*args, **kwargs):
            manifest = prepare_snapshot(*args, **kwargs)
            snapshots.append(manifest)
            return manifest

        with patch.object(module.workspace, "prepare_candidate_snapshot", side_effect=record_snapshot):
            self.driver.prepare()
        self.assertEqual(snapshots[0]["base_sha"], first_base)
        first_merge = snapshots[0]["head_sha"]
        self.assertEqual(self.git(self.tree, "show", "-s", "--format=%P", first_merge).split(), [self.head, first_base])
        self.assertEqual(self.git(self.tree, "rev-parse", first_merge + "^{tree}"), saved["tree_sha"])
        entry = self.entry()
        self.assertEqual(entry["manifest"]["base_sha"], self.current_base)
        self.assertNotIn("base_merge", entry["request"])
        self.assertEqual(entry["request"]["base_refresh_count"], 2)
        snapshot = entry["snapshot_id"]
        merged = self.git(self.tree, "rev-parse", "HEAD")
        self.driver.prepare()
        self.assertEqual(self.entry()["snapshot_id"], snapshot)
        self.assertEqual(self.entry()["request"]["base_refresh_count"], 2)
        self.assertEqual(self.git(self.tree, "rev-parse", "HEAD"), merged)
        self.assertEqual(
            self.git(self.tree, "show", "-s", "--format=%P", "HEAD").split(), [first_merge, self.current_base]
        )

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

    def register_repair(self, *, conflicting_file=False):
        report = self.root / "report.md"
        report.write_text("Registered review findings\n")
        with store.connect(self.db) as conn:
            store.update(conn, self.round_id, report_path=str(report), report_sha256=store.digest(report))
            run = store.new_run(conn, self.round_id, "address_findings", "writer", self.attempt_id, self.root, "prompt")
        address = self.driver.address_workspace(run["run_id"])
        address.parent.mkdir(parents=True)
        self.git(self.repo, "worktree", "add", "--detach", str(address), self.head)
        (address / "source.py").write_text("value = 4\n")
        if conflicting_file:
            (address / "shared.py").write_text("shared = 'repair'\n")
            self.git(address, "add", "shared.py")
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
        return repaired, dispositions

    def test_explicit_retrigger_preserves_authenticated_repair_before_completed_merge(self):
        repaired, dispositions = self.register_repair()
        self.git(self.tree, "merge", "--ff-only", repaired)
        merged = self.completed_merge(repaired)
        self.git(self.upstream, "checkout", "main")
        (self.upstream / "second_feature.py").write_text(
            "import importlib\nvalue = importlib.import_module('runtime')\n"
        )
        self.git(self.upstream, "add", "second_feature.py")
        self.git(self.upstream, "commit", "-m", "upstream advances after authenticated repair merge")
        self.current_base = self.git(self.upstream, "rev-parse", "HEAD")
        original = dispositions.read_bytes()
        dispositions.write_text("changed evidence")
        with self.assertRaisesRegex(StageStopped, "completed repair evidence changed"):
            self.driver.prepare()
        dispositions.write_bytes(original)
        self.driver.prepare()
        final = self.git(self.tree, "rev-parse", "HEAD")
        self.assertEqual(self.entry()["manifest"]["head_sha"], final)
        self.assertEqual(self.git(self.tree, "show", "-s", "--format=%P", final).split(), [merged, self.current_base])
        self.assertEqual((self.tree / "source.py").read_text(), "value = 4\n")
        self.assertEqual((self.tree / "base_feature.py").read_text(), "upstream_feature = True\n")
        self.assertEqual(self.entry()["manifest"]["candidate"]["candidate_sha256"], self.candidate["candidate_sha256"])
        self.assertEqual(self.entry()["proof"], {})

    def test_operator_merge_of_original_candidate_retains_registered_repair(self):
        self.assert_original_merge_repair_recovery()

    def test_operator_repair_merge_recovers_after_commit_before_snapshot(self):
        self.assert_original_merge_repair_recovery(interrupt="commit")

    def test_operator_repair_merge_recovers_after_checkpoint_before_commit(self):
        self.assert_original_merge_repair_recovery(interrupt="before_commit")

    def test_operator_repair_merge_recovers_after_snapshot_before_save(self):
        self.assert_original_merge_repair_recovery(interrupt="snapshot")

    def test_operator_repair_conflict_keeps_branch_and_evidence(self):
        self.register_repair(conflicting_file=True)
        self.git(self.upstream, "checkout", "main")
        (self.upstream / "shared.py").write_text("shared = 'upstream'\n")
        self.git(self.upstream, "add", "shared.py")
        self.git(self.upstream, "commit", "-m", "upstream adds conflicting repair file")
        self.current_base = self.git(self.upstream, "rev-parse", "HEAD")
        self.git(self.repo, "fetch", "upstream", self.current_base)
        merged = self.completed_merge()
        before = self.entry()
        with self.assertRaisesRegex(StageStopped, "conflicts with the registered repair"):
            self.driver.prepare()
        self.assertEqual(self.git(self.tree, "rev-parse", "HEAD"), merged)
        self.assertEqual(self.entry()["request"], before["request"])
        self.assertEqual(self.entry()["proof"], before["proof"])

    def assert_original_merge_repair_recovery(self, interrupt=None):
        repaired, dispositions = self.register_repair()
        first_base = self.current_base
        merged = self.completed_merge()
        self.git(self.upstream, "checkout", "main")
        (self.upstream / "second_feature.py").write_text(
            "import importlib\nvalue = importlib.import_module('runtime')\n"
        )
        self.git(self.upstream, "add", "second_feature.py")
        self.git(self.upstream, "commit", "-m", "newer upstream")
        self.current_base = self.git(self.upstream, "rev-parse", "HEAD")
        if interrupt:
            native_git = module.base_impact.git

            def interrupted_git(path, *args, **kwargs):
                is_merge = args[:3] == ("merge", "--no-ff", "--no-edit")
                if is_merge and interrupt == "before_commit":
                    self.assertIn("base_merge", self.entry()["request"])
                    raise StageStopped("repair interrupted")
                result = native_git(path, *args, **kwargs)
                if is_merge and interrupt == "commit":
                    self.assertIn("base_merge", self.entry()["request"])
                    raise StageStopped("repair interrupted")
                return result

            if interrupt == "snapshot":
                guard = patch.object(self.driver, "save_manifest", side_effect=StageStopped("repair interrupted"))
            else:
                guard = patch.object(module.base_impact, "git", side_effect=interrupted_git)
            with guard, self.assertRaisesRegex(StageStopped, "repair interrupted"):
                self.driver.prepare()
            saved = self.entry()
            self.assertEqual(saved["request"]["base_merge"]["base_sha"], first_base)
            self.assertEqual(saved["request"]["base_merge"]["repair_sha"], repaired)
            self.driver.manifest = saved["manifest"]
            self.driver.request.update(saved["request"])
            original = dispositions.read_bytes()
            dispositions.write_text("forged evidence")
            with self.assertRaisesRegex(StageStopped, "completed repair evidence changed"):
                self.driver.prepare()
            dispositions.write_bytes(original)
            checkpoint = saved["request"]["base_merge"]
            changed = dict(saved["request"])
            changed["base_merge"] = {**checkpoint, "repair_parent_sha": self.head}
            with store.connect(self.db) as conn:
                store.update(conn, self.round_id, request_json=json.dumps(changed))
            with self.assertRaises(StageStopped):
                self.driver.prepare()
            with store.connect(self.db) as conn:
                store.update(conn, self.round_id, request_json=json.dumps(saved["request"]))
        self.driver.prepare()
        head = self.git(self.tree, "rev-parse", "HEAD")
        repair_merge = self.git(self.tree, "rev-parse", head + "^1")
        self.assertEqual(self.git(self.tree, "show", "-s", "--format=%P", repair_merge).split(), [merged, repaired])
        self.assertEqual((self.tree / "source.py").read_text(), "value = 4\n")
        self.assertEqual(self.entry()["manifest"]["base_sha"], self.current_base)
        self.assertEqual(self.entry()["manifest"]["head_sha"], head)
        self.assertEqual(self.entry()["request"]["base_refresh_count"], 2)
        self.assertNotIn("base_merge", self.entry()["request"])
        self.assertEqual(self.entry()["proof"], {})
        self.driver.prepare()
        self.assertEqual(self.git(self.tree, "rev-parse", "HEAD"), head)
        self.assertEqual(self.entry()["request"]["base_refresh_count"], 2)

    def test_explicit_retrigger_rejects_merge_from_an_unrelated_base(self):
        self.git(self.upstream, "checkout", "-b", "unrelated", self.base)
        (self.upstream / "unrelated.py").write_text("unrelated = True\n")
        self.git(self.upstream, "add", "unrelated.py")
        self.git(self.upstream, "commit", "-m", "unrelated base")
        unrelated = self.git(self.upstream, "rev-parse", "HEAD")
        self.git(self.repo, "fetch", "upstream", unrelated)
        self.git(self.tree, "merge", "--no-ff", "--no-edit", unrelated)
        before = self.entry()
        with self.assertRaisesRegex(StageStopped, "outside the current upstream history"):
            self.driver.prepare()
        self.assertEqual(self.entry()["request"], before["request"])
        self.assertEqual(self.entry()["proof"], before["proof"])

    def test_earlier_merge_recovery_remains_replayable_after_snapshot_interruption(self):
        first_base = self.current_base
        first_merge = self.completed_merge()
        self.git(self.upstream, "checkout", "main")
        (self.upstream / "second_feature.py").write_text(
            "import importlib\nvalue = importlib.import_module('runtime')\n"
        )
        self.git(self.upstream, "add", "second_feature.py")
        self.git(self.upstream, "commit", "-m", "newer upstream while retry waits")
        self.current_base = self.git(self.upstream, "rev-parse", "HEAD")
        with patch.object(
            module.workspace, "prepare_candidate_snapshot", side_effect=StageStopped("snapshot interrupted")
        ):
            with self.assertRaisesRegex(StageStopped, "snapshot interrupted"):
                self.driver.prepare()
        saved = self.entry()["request"]["base_merge"]
        self.assertEqual(saved["base_sha"], first_base)
        self.assertEqual(saved["candidate_sha"], self.head)
        self.assertEqual(self.git(self.tree, "rev-parse", "HEAD"), first_merge)
        self.driver.prepare()
        self.assertEqual(self.entry()["manifest"]["base_sha"], self.current_base)
        self.assertEqual(self.entry()["request"]["base_refresh_count"], 2)
        self.assertNotIn("base_merge", self.entry()["request"])

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

    def test_missing_merge_identity_does_not_charge_refresh_or_change_writer(self):
        before = self.entry()
        head = self.git(self.tree, "rev-parse", "HEAD")
        with patch.object(
            module,
            "check_merge_environment",
            side_effect=module.MergePreflightError("merge Git committer identity unavailable"),
        ):
            with self.assertRaisesRegex(StageStopped, "committer identity unavailable"):
                self.driver.merge_current_base(self.candidate, self.current_base)
        after = self.entry()
        self.assertEqual(after["request"], before["request"])
        self.assertEqual(after["proof"], before["proof"])
        self.assertEqual(self.git(self.tree, "rev-parse", "HEAD"), head)
