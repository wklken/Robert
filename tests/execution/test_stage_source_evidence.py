"""Source completeness is a prerequisite for resume and artifact adoption."""

import json
from unittest.mock import patch

from robert_agent.adapters.git.review_revision import git
from robert_agent.execution import stage_evidence
from robert_agent.execution.stages import StageStopped
from robert_agent.persistence.rounds import review as store
from tests.support.review_coordinator import ReviewCoordinatorFixture
from tests.support.review_workspace import ReviewWorkspaceFixture


class SourceEvidenceTests(ReviewWorkspaceFixture):
    def test_local_edition_symlink_pins_link_and_target_while_external_links_are_refused(self):
        (self.repo / ".gitignore").write_text("edition.py\n")
        link = self.repo / "edition.py"
        link.symlink_to(self.repo / "source.py")
        run = {"run_id": "run-1", "prompt_sha256": "prompt-sha"}
        manifest = {"head_sha": self.head}
        checkpoint = stage_evidence.observe_source(run, manifest, self.repo)
        self.assertIn("edition.py", checkpoint["untracked_sha256"])
        (self.repo / "source.py").write_text("changed edition source\n")
        self.assertNotEqual(stage_evidence.observe_source(run, manifest, self.repo), checkpoint)
        link.unlink()
        link.symlink_to("source.py")
        self.assertNotEqual(stage_evidence.observe_source(run, manifest, self.repo), checkpoint)
        link.unlink()
        link.symlink_to(self.upstream / "source.py")
        with self.assertRaisesRegex(stage_evidence.SourcePinUnavailable, "symlink"):
            stage_evidence.observe_source(run, manifest, self.repo)

    def test_ignored_environment_does_not_exhaust_source_pin_or_hide_configuration(self):
        (self.repo / ".gitignore").write_text(".venv/\nnode_modules/\n.mypy_cache/\nruntime-config.json\n")
        for directory in (".venv/lib", "component/node_modules", ".mypy_cache"):
            root = self.repo / directory
            root.mkdir(parents=True)
            for number in range(1001):
                (root / str(number)).write_text("generated dependency\n")
        config = self.repo / "runtime-config.json"
        config.write_text("first configuration\n")
        run = {"run_id": "run-1", "prompt_sha256": "prompt-sha"}
        manifest = {"snapshot_id": "snapshot-1", "head_sha": self.head, "base_sha": self.base}
        checkpoint = stage_evidence.observe_source(run, manifest, self.repo)
        self.assertEqual(set(checkpoint["untracked_sha256"]), {".gitignore", "runtime-config.json"})
        config.write_text("changed configuration\n")
        self.assertNotEqual(stage_evidence.observe_source(run, manifest, self.repo), checkpoint)

    def test_unignored_environment_files_are_still_pinned(self):
        directory = self.repo / ".venv"
        directory.mkdir()
        (directory / "runtime-config.json").write_text("local source input\n")
        checkpoint = stage_evidence.observe_source(
            {"run_id": "run-1", "prompt_sha256": "prompt-sha"}, {"head_sha": self.head}, self.repo
        )
        self.assertIn(".venv/runtime-config.json", checkpoint["untracked_sha256"])

    def test_ignored_runtime_configuration_changes_exact_source_observation(self):
        (self.repo / ".gitignore").write_text("runtime-config.json\n")
        config = self.repo / "runtime-config.json"
        config.write_text('{"enabled": true}\n')
        run = {"run_id": "run-1", "prompt_sha256": "prompt-sha"}
        manifest = {"snapshot_id": "snapshot-1", "head_sha": self.head, "base_sha": self.base}
        original = stage_evidence.observe_source(run, manifest, self.repo)
        config.write_text('{"enabled": false}\n')
        self.assertNotEqual(stage_evidence.observe_source(run, manifest, self.repo), original)


class StageSourceEvidenceTests(ReviewCoordinatorFixture):
    def driver(self):
        self.cycle([self.event()])
        driver = self.coordinator()
        driver.prepare()
        driver.request["workers"] = driver.request["workers"][:1]
        return driver

    def ignored_config(self, driver):
        # Keep the frozen review snapshot intact while exercising the separate
        # writer worktree used by implementation/address stages.
        tree = self.root / "writer"
        git(self.git_fixture.repo, "worktree", "add", "--detach", str(tree), driver.manifest["head_sha"])
        driver.stage_worktree = lambda run: str(tree)
        (self.git_fixture.repo / ".git" / "info" / "exclude").write_text("runtime-config.json\n.local-runtime/\n")
        config = tree / "runtime-config.json"
        config.write_text('{"enabled": true}\n')
        return tree, config

    def new_run(self, driver):
        worker = driver.request["workers"][0]
        with store.connect(self.db) as conn:
            return store.new_run(
                conn,
                driver.round_id,
                "review",
                worker["name"],
                driver.attempt_id,
                driver.manifest["artifact_root"],
                driver.manifest["prompt_sha256"],
                worker["default_model"],
            )

    def test_incomplete_pin_falls_back_to_fresh_stage_without_native_resume(self):
        driver = self.driver()
        tree, _ = self.ignored_config(driver)
        root = tree / ".local-runtime" / "bin"
        root.mkdir(parents=True)
        (root / "python").symlink_to("/usr/bin/python3")
        run = self.new_run(driver)
        checkpoint = driver.source_checkpoint(run)
        self.assertIn("source_pin_error", checkpoint)
        recorded = {
            "ok": True,
            "pid": 1073741824,
            "process_exited": True,
            "returncode": 1,
            "worktree_path": str(tree),
            "launch_checkpoint": checkpoint,
            "result_checkpoint": checkpoint,
            "failure_kind": "interrupted",
        }
        with store.connect(self.db) as conn:
            store.update_run(conn, run["run_id"], status="interrupted", dispatch_json=json.dumps(recorded))
        records = driver.review()
        self.assertEqual(len(self.calls), 1)
        with store.connect(self.db) as conn:
            fresh = next(r for r in store.runs(conn, driver.round_id) if r["run_id"] == records[0]["run_id"])
        self.assertNotIn("native_resume", fresh["dispatch"])
        self.assertEqual(fresh["dispatch"]["resume_fallback_reason"], "source_checkpoint_incomplete")

    def test_complete_pin_changed_ignored_config_refuses_native_resume_before_launch(self):
        driver = self.driver()
        tree, config = self.ignored_config(driver)
        run = self.new_run(driver)
        checkpoint = driver.source_checkpoint(run)
        recorded = {
            "ok": True,
            "pid": 1073741824,
            "process_exited": True,
            "returncode": 1,
            "worktree_path": str(tree),
            "launch_checkpoint": checkpoint,
            "result_checkpoint": checkpoint,
        }
        with store.connect(self.db) as conn:
            store.update_run(conn, run["run_id"], status="interrupted", dispatch_json=json.dumps(recorded))
        config.write_text('{"enabled": false}\n')
        with self.assertRaisesRegex(StageStopped, "source checkpoint changed"):
            driver.review()
        self.assertEqual(self.calls, [])

    def test_native_child_incomplete_observation_at_spawn_cannot_use_complete_parent_pin(self):
        driver = self.driver()
        tree, _ = self.ignored_config(driver)
        parent = self.new_run(driver)
        checkpoint = driver.source_checkpoint(parent)
        recorded = {
            "ok": True,
            "pid": 1073741824,
            "process_exited": True,
            "returncode": 1,
            "worktree_path": str(tree),
            "launch_checkpoint": checkpoint,
            "result_checkpoint": checkpoint,
        }
        with store.connect(self.db) as conn:
            store.update_run(conn, parent["run_id"], status="interrupted", dispatch_json=json.dumps(recorded))
        child = self.new_run(driver)
        child["dispatch"] = {"parent_run_id": parent["run_id"], "native_resume": {"cwd": str(tree)}}
        source_checkpoint = driver.source_checkpoint

        def install_environment(run):
            if run["run_id"] == child["run_id"]:
                root = tree / ".local-runtime" / "bin"
                root.mkdir(parents=True)
                (root / "python").symlink_to("/usr/bin/python3")
            return source_checkpoint(run)

        driver.source_checkpoint = install_environment
        worker = driver.request["workers"][0]
        with self.assertRaisesRegex(StageStopped, "launch source checkpoint incomplete"):
            driver.launch_with_slot(child, worker, driver.manifest["prompt_path"], {})
        self.assertEqual(self.calls, [])
        with store.connect(self.db) as conn:
            saved = next(r for r in store.runs(conn, driver.round_id) if r["run_id"] == child["run_id"])
        self.assertFalse(saved["slot_reserved"])

    def failed_registration(self, driver):
        with patch.object(driver, "register", side_effect=ValueError("registration unavailable")):
            with self.assertRaisesRegex(RuntimeError, "registration unavailable"):
                driver.review()
        with store.connect(self.db) as conn:
            return store.runs(conn, driver.round_id)[-1]

    def test_incomplete_pin_cannot_adopt_even_unchanged_complete_artifact(self):
        driver = self.driver()
        tree, _ = self.ignored_config(driver)
        root = tree / ".local-runtime" / "bin"
        root.mkdir(parents=True)
        (root / "python").symlink_to("/usr/bin/python3")
        original = self.failed_registration(driver)
        self.assertIn("source_pin_error", original["dispatch"]["result_checkpoint"])
        with self.assertRaisesRegex(StageStopped, "incomplete"):
            driver.review()
        self.assertEqual(len(self.calls), 1)

    def test_known_success_revalidates_output_without_relaunch_when_pin_is_incomplete(self):
        driver = self.driver()
        tree, _ = self.ignored_config(driver)
        root = tree / ".local-runtime" / "bin"
        root.mkdir(parents=True)
        (root / "python").symlink_to("/usr/bin/python3")
        original = driver.review()
        self.assertEqual(driver.review(), original)
        self.assertEqual(len(self.calls), 1)

    def test_result_pin_stays_incomplete_when_environment_disappears_before_registration(self):
        driver = self.driver()
        tree, _ = self.ignored_config(driver)
        root = tree / ".local-runtime" / "bin"
        root.mkdir(parents=True)
        interpreter = root / "python"
        interpreter.symlink_to("/usr/bin/python3")
        checkpoint = driver.checkpoint_result

        def clean_environment(run, **kwargs):
            checkpoint(run, **kwargs)
            interpreter.unlink(missing_ok=True)

        driver.checkpoint_result = clean_environment
        records = driver.review()
        self.assertEqual(len(self.calls), 1)
        with store.connect(self.db) as conn:
            saved = next(r for r in store.runs(conn, driver.round_id) if r["run_id"] == records[0]["run_id"])
        self.assertIn("source_pin_error", saved["dispatch"]["result_checkpoint"])

    def test_complete_pin_changed_ignored_config_refuses_adoption_before_launch(self):
        driver = self.driver()
        _, config = self.ignored_config(driver)
        original = self.failed_registration(driver)
        config.write_text('{"enabled": false}\n')
        with self.assertRaisesRegex(StageStopped, "source checkpoint changed"):
            driver.review()
        self.assertEqual(len(self.calls), 1)
        with store.connect(self.db) as conn:
            saved = next(r for r in store.runs(conn, driver.round_id) if r["run_id"] == original["run_id"])
        self.assertNotIn("artifact_adoption", saved["dispatch"])
