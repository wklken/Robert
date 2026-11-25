"""Generated environments allow ordinary progress and exact recovery pins."""

from pathlib import Path

from robert_agent.persistence.rounds import pre_pr_review as store
from tests.support.pre_pr_review import PrePRReviewFixture


class PrePRSourceEvidenceTests(PrePRReviewFixture):
    def start_with_ignored_environment(self):
        driver = self.start(["claude"])
        exclude = self.git_fixture.repo / ".git" / "info" / "exclude"
        exclude.write_text(exclude.read_text() + "\nnode_modules/\n.venv/\n")
        return driver

    def assert_implementation_registered(self, driver, result):
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.launches["implement"], 1)
        with store.connect(self.db) as conn:
            run = next(r for r in store.runs(conn, driver.round_id) if r["stage"] == "implement")
        self.assertEqual(run["status"], "succeeded")
        self.assertTrue(run["artifact"])
        self.assertNotIn("source_pin_error", run["dispatch"]["result_checkpoint"])

    def test_1001_ignored_node_modules_files_allow_normal_launch_and_completion(self):
        driver = self.start_with_ignored_environment()
        root = Path(driver.assigned_path) / "node_modules"
        root.mkdir()
        for number in range(1001):
            (root / f"dependency-{number}.js").write_text("export const value = 1;\n")
        self.assert_implementation_registered(driver, driver.run())

    def test_1001_ignored_virtualenv_files_allow_normal_launch_and_completion(self):
        driver = self.start_with_ignored_environment()
        root = Path(driver.assigned_path) / ".venv" / "lib"
        root.mkdir(parents=True)
        for number in range(1001):
            (root / f"dependency-{number}.py").write_text("value = 1\n")
        self.assert_implementation_registered(driver, driver.run())

    def test_virtualenv_python_symlink_created_by_successful_writer_allows_completion(self):
        driver = self.start_with_ignored_environment()
        launch = driver.launcher

        def install_environment(**kwargs):
            result = launch(**kwargs)
            if "ROBERT_PRE_PR_CANDIDATE_DIR" in kwargs["trusted_environment"]:
                root = Path(kwargs["worktree_path"]) / ".venv" / "bin"
                root.mkdir(parents=True)
                (root / "python").symlink_to("/usr/bin/python3")
            return result

        driver.launcher = install_environment
        self.assert_implementation_registered(driver, driver.run())
