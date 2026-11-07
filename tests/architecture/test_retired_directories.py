from tests.support import REPO_ROOT
from tests.support.repository_cycle import (
    RepositoryCycleFixture,
)


class RetiredDirectoriesTests(RepositoryCycleFixture):
    def test_old_dispatcher_and_runner_directories_are_removed(self):
        self.assertFalse((REPO_ROOT / "skills" / "dd-task-runner").exists())
        self.assertFalse((REPO_ROOT / "tests" / "dd_github_dispatcher").exists())
