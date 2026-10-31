from pathlib import Path
import subprocess
import sys
import json
import tempfile
import unittest

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent.worktree import plan_analysis_worktree, plan_review_worktree


class WorktreeSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.upstream = root / 'upstream'
        self.repo = root / 'repo'
        self.managed = root / 'managed'
        self.upstream.mkdir()
        self.git(self.upstream, 'init', '-b', 'main')
        self.git(self.upstream, 'config', 'user.email', 'test@example.invalid')
        self.git(self.upstream, 'config', 'user.name', 'Test')
        (self.upstream / 'data').write_text('v1')
        self.git(self.upstream, 'add', '.')
        self.git(self.upstream, 'commit', '-m', 'initial')
        self.git(self.upstream, 'update-ref', 'refs/pull/42/head', 'HEAD')
        self.git(root, 'clone', str(self.upstream), str(self.repo))
        self.git(self.repo, 'remote', 'rename', 'origin', 'upstream')

    def git(self, cwd, *args):
        return subprocess.run(['git', *args], cwd=cwd, text=True,
                              capture_output=True, check=True).stdout.strip()

    def test_review_rejects_unmanaged_worktree(self):
        human = Path(self.tmp.name) / 'human'
        self.git(self.repo, 'worktree', 'add', '-b', 'review/pr-42-title', str(human), 'HEAD')
        (human / 'data').write_text('human changes')
        result = plan_review_worktree(self.repo, self.managed, 42, 'title', 'main', dry_run=False)
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'blocked_unmanaged_worktree')
        self.assertEqual((human / 'data').read_text(), 'human changes')

    def test_worktree_creation_keeps_control_stdout_valid_json(self):
        result = subprocess.run([sys.executable, '-B', '-m', 'robert_agent.worktree',
            '--repo-root', str(self.repo), '--worktree-root', str(self.managed),
            '--source-number', '205', '--short-slug', 'warning', '--base-branch', 'main'],
            text=True, capture_output=True, check=True)
        payload = json.loads(result.stdout)
        self.assertTrue(payload['ok'])
        self.assertTrue(Path(payload['worktree_path']).is_dir())

    def test_review_rejects_dirty_managed_worktree(self):
        first = plan_review_worktree(self.repo, self.managed, 42, 'title', 'main', dry_run=False)
        tree = Path(first['worktree_path'])
        (tree / 'data').write_text('human changes')
        result = plan_review_worktree(self.repo, self.managed, 42, 'title', 'main', dry_run=False)
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'blocked_dirty_worktree')
        self.assertEqual((tree / 'data').read_text(), 'human changes')

    def test_analysis_refreshes_clean_reused_worktree(self):
        first = plan_analysis_worktree(self.repo, self.managed, 42, 'main', dry_run=False)
        (self.upstream / 'data').write_text('v2')
        self.git(self.upstream, 'commit', '-am', 'advance')
        result = plan_analysis_worktree(self.repo, self.managed, 42, 'main', dry_run=False)
        self.assertTrue(result['ok'])
        self.assertEqual(Path(first['worktree_path'], 'data').read_text(), 'v2')

    def test_analysis_preserves_dirty_reused_worktree(self):
        first = plan_analysis_worktree(self.repo, self.managed, 42, 'main', dry_run=False)
        tree = Path(first['worktree_path'])
        (tree / 'data').write_text('local changes')
        result = plan_analysis_worktree(self.repo, self.managed, 42, 'main', dry_run=False)
        self.assertFalse(result['ok'])
        self.assertEqual((tree / 'data').read_text(), 'local changes')
