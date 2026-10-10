from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent import publish, audit_result


def completed(stdout=''):
    return SimpleNamespace(returncode=0, stdout=stdout, stderr='')


class PublicationRecoveryTests(unittest.TestCase):
    def test_retry_recognizes_successful_push_after_transaction_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'db'
            scope = {'repo': 'x/y', 'worktree_path': tmp, 'branch_name': 'codex/fix',
                     'remote': 'origin', 'existing_pr_head_sha': 'old-sha',
                     'result_head_sha': 'result-sha',
                     'sources': [{'source_type': 'pull_request', 'number': 1}]}
            with closing(sqlite3.connect(db)) as conn, conn:
                conn.executescript('''
                    CREATE TABLE worker_results(result_id TEXT,metadata_json TEXT);
                    CREATE TABLE github_actions(action_id TEXT,result_id TEXT,task_id TEXT,
                        action_type TEXT,target_url TEXT,external_id TEXT,audit_status TEXT,
                        publish_status TEXT,created_at TEXT,metadata_json TEXT);
                    CREATE TABLE notifications(notification_id TEXT,task_id TEXT,notification_type TEXT,
                        channel TEXT,status TEXT,created_at TEXT,metadata_json TEXT);
                ''')
                conn.execute('INSERT INTO worker_results VALUES (?,?)',
                             ('r', json.dumps({'audit': {'status': 'accepted', 'action_scope': scope}})))
                for aid, kind, payload in [('1', 'push_existing_pr', {'worktree_path': tmp, 'branch': 'codex/fix'}),
                        ('2', 'comment', {'body': 'Done <!-- robert-comment r -->'})]:
                    conn.execute('INSERT INTO github_actions VALUES (?,?,?,?,?,?,?,?,?,?)',
                        (aid, 'r', 't', kind, 'https://github.com/x/y/pull/1', '', 'accepted',
                         'not_published', aid, json.dumps(payload)))
            remote = ['old-sha']
            crash = [True]
            pushes = []

            def runner(command, **kwargs):
                if command[:2] == ['git', 'ls-remote']:
                    return completed(remote[0] + '\trefs/heads/codex/fix\n')
                if command[:2] == ['git', 'push']:
                    pushes.append(command)
                    remote[0] = 'result-sha'
                    return completed()
                if crash[0]:
                    raise RuntimeError('crash after push')
                if command[:2] == ['gh', 'api']:
                    return completed('[]')
                if command[:3] == ['gh', 'pr', 'comment']:
                    return completed('https://github.com/x/y/pull/1#issuecomment-1')
                raise AssertionError(command)

            with self.assertRaisesRegex(RuntimeError, 'crash after push'):
                publish.publish_ready_actions(db, run_command=runner)
            crash[0] = False
            result = publish.publish_ready_actions(db, run_command=runner)
            self.assertTrue(result['ok'], result)
            self.assertEqual(result['published_count'], 2)
            self.assertEqual(result['deduplicated_count'], 1)
            self.assertEqual(len(pushes), 1)
            self.assertEqual(pushes[0][-1], 'result-sha:codex/fix')

    def test_leased_push_without_result_revision_fails_before_writing(self):
        scope = {"existing_pr_head_sha": "old-sha"}
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.execute("CREATE TABLE worker_results(result_id TEXT, metadata_json TEXT)")
            conn.execute("INSERT INTO worker_results VALUES (?,?)",
                         ("r", json.dumps({"audit": {"status": "accepted", "action_scope": scope}})))
            action = {"result_id": "r", "target_url": "", "metadata_json": json.dumps(
                {"worktree_path": "/assigned", "branch": "codex/fix"})}
            writes = []

            def runner(command, **kwargs):
                writes.append(command)
                return completed("old-sha\trefs/heads/codex/fix")

            result = publish._publish_push_existing_pr(action, runner, conn)
            self.assertFalse(result["ok"])
            self.assertEqual(writes, [])

    def test_legacy_new_pr_push_without_result_revision_fails(self):
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.execute("CREATE TABLE worker_results(result_id TEXT, metadata_json TEXT)")
            conn.execute("INSERT INTO worker_results VALUES (?,?)",
                         ("r", json.dumps({"audit": {"status": "accepted", "action_scope": {"repo": "x/y"}}})))
            action = {"result_id": "r", "target_url": "", "metadata_json": json.dumps(
                {"worktree_path": "/assigned", "branch": "codex/fix"})}
            from unittest.mock import Mock
            runner = Mock(return_value=completed())
            self.assertFalse(publish._publish_push_existing_pr(action, runner, conn)["ok"])
            runner.assert_not_called()

    def test_marker_cannot_override_missing_fork_owner(self):
        pr = {'number': 9, 'url': 'https://github.com/x/y/pull/9',
              'baseRefName': 'main', 'headRefName': 'codex/fix',
              'headRepositoryOwner': None, 'body': '<!-- robert-workstream ws -->'}
        def runner(command, **kwargs):
            return completed(json.dumps([pr] if command[2] == 'list' else pr))
        result = publish._find_existing_open_pr(
            'x/y', 'expected-owner:codex/fix', '<!-- robert-workstream ws -->', runner,
            base='main', head_owner='expected-owner')
        self.assertEqual(result['status'], 'not_found')

    def test_open_pr_audit_rejects_different_fork_owner(self):
        scope = {'repo': 'x/y', 'base_branch': 'main', 'branch_name': 'codex/fix',
                 'worktree_path': '/assigned', 'remote': 'origin', 'head_owner': 'expected-owner',
                 'sources': [{'source_type': 'issue', 'number': 1}]}
        action = {'type': 'open_pr', 'repo': 'x/y', 'base': 'main',
                  'head': 'other-owner:codex/fix', 'title': 'Fix',
                  'body': 'Fix <!-- robert-workstream ws -->'}
        self.assertIsNotNone(audit_result.action_scope_violation([action], scope))
        action['head'] = 'expected-owner:codex/fix'
        self.assertIsNone(audit_result.action_scope_violation([action], scope))

    def test_marker_cannot_override_mismatched_fork_owner(self):
        pr = {'number': 9, 'url': 'https://github.com/x/y/pull/9',
              'baseRefName': 'main', 'headRefName': 'codex/fix',
              'headRepositoryOwner': {'login': 'other-owner'},
              'body': '<!-- robert-workstream ws -->'}

        def runner(command, **kwargs):
            return completed(json.dumps([pr] if command[2] == 'list' else pr))

        result = publish._find_existing_open_pr(
            'x/y', 'expected-owner:codex/fix', '<!-- robert-workstream ws -->', runner,
            base='main', head_owner='expected-owner')
        self.assertEqual(result['status'], 'not_found')
