from contextlib import closing
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from robert_agent import common, daemon, publish, run_once, worktree
from tests import test_publication_reconcile as publication_fixture
from robert_agent.validate_config import DAEMON_DEFAULTS


class DeadlineRemediationTests(unittest.TestCase):
    def test_exited_group_at_term_or_kill_still_returns_reaped_timeout(self):
        for at_kill in (False, True):
            with self.subTest(at_kill=at_kill):
                process = mock.Mock(pid=43214321)
                waits = [subprocess.TimeoutExpired(['control'], 1)]
                if at_kill:
                    waits.append(subprocess.TimeoutExpired(['control'], .1))
                waits.append(('', ''))
                process.communicate.side_effect = waits
                signals = [None, ProcessLookupError()] if at_kill else [ProcessLookupError()]
                with mock.patch.object(daemon.subprocess, 'Popen', return_value=process), mock.patch.object(
                    daemon.os, 'killpg', side_effect=signals
                ) as kill, mock.patch.object(run_once, '_live_process_group', return_value=False):
                    result = daemon.run_child_json(['control'], timeout_seconds=1)
                self.assertEqual(result['status'], 'child_timeout')
                self.assertTrue(result['child']['executor_id'])
                self.assertTrue(result['child']['process_group_stopped'])
                self.assertEqual(process.communicate.call_count, 3 if at_kill else 2)
                self.assertEqual(kill.call_args.args[1], signal.SIGKILL if at_kill else signal.SIGTERM)

    def test_absent_signal_target_does_not_replace_group_liveness_proof(self):
        process = mock.Mock(pid=43214321)
        process.communicate.side_effect = [subprocess.TimeoutExpired(['control'], 1), ('', '')]
        with mock.patch.object(daemon.subprocess, 'Popen', return_value=process), mock.patch.object(
            daemon.os, 'killpg', side_effect=ProcessLookupError()
        ), mock.patch.object(run_once, '_live_process_group', return_value=True):
            result = daemon.run_child_json(['control'], timeout_seconds=1)
        self.assertFalse(result['child']['process_group_stopped'])
        self.assertTrue(result['child']['executor_id'])

    def _prepare(self, root, runner):
        return worktree.prepare_existing_pr_worktree(
            root, root / 'worktrees', source_number=7, head_branch='topic',
            head_sha='head', base_branch='main', base_sha='base', dry_run=False, runner=runner,
        )

    def test_pr_worktree_expired_deadline_invokes_no_injected_command(self):
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, '', ''))
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            worktree, '_existing_worktree_for_branch', return_value=None
        ), mock.patch.dict(os.environ, {'_ROBERT_CONTROL_DEADLINE': str(time.monotonic() - 1)}):
            with self.assertRaises(subprocess.TimeoutExpired):
                self._prepare(Path(tmp), runner)
        runner.assert_not_called()

    def test_pr_worktree_new_and_reuse_commands_share_remaining_budget(self):
        for reuse in (False, True):
            with self.subTest(reuse=reuse), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                calls = []
                def runner(command, **kwargs):
                    calls.append(kwargs)
                    stdout = ''
                    if command[1] == 'rev-parse':
                        stdout = 'head' if command[-1].endswith('-head') else 'base'
                    return subprocess.CompletedProcess(command, 0, stdout, '')
                with mock.patch.object(worktree, '_existing_worktree_for_branch',
                                       return_value=root / 'worktrees' / 'topic' if reuse else None), mock.patch.dict(
                    os.environ, {'_ROBERT_CONTROL_DEADLINE': str(time.monotonic() + 2)}
                ):
                    result = self._prepare(root, runner)
                self.assertTrue(result['ok'])
                self.assertEqual(len(calls), 6 if reuse else 5)
                self.assertTrue(all(0 < call.get('timeout', 999) <= 2 for call in calls), calls)

    def test_materialization_probe_rejects_expired_deadline_before_process(self):
        with mock.patch.object(common.subprocess, 'run') as runner, mock.patch.dict(
            os.environ, {'_ROBERT_CONTROL_DEADLINE': str(time.monotonic() - 1)}
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                run_once._can_materialize_worktree({'repo_root': '/unused'})
        runner.assert_not_called()

    def test_publication_expired_deadline_retains_intent_without_remote_invocation(self):
        fixture = publication_fixture.PublicationReconcileTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, '[]', ''))
        with mock.patch.dict(os.environ, {'_ROBERT_CONTROL_DEADLINE': str(time.monotonic() - 1)}):
            result = publish.publish_ready_actions(fixture.db_path, run_command=runner)
        self.assertEqual(result['failed_count'], 1, result)
        self.assertEqual(result['published_count'], 0)
        runner.assert_not_called()
        with closing(sqlite3.connect(fixture.db_path)) as conn:
            self.assertEqual(conn.execute('SELECT status FROM publication_intents').fetchone()[0], 'executing')
            self.assertEqual(conn.execute('SELECT publish_status FROM github_actions').fetchone()[0], 'not_published')
            self.assertEqual(conn.execute("SELECT json_extract(metadata_json, '$.publish.status') FROM github_actions").fetchone()[0], 'publish_failed')

    def test_publication_caps_injected_commands_by_deadline_and_default(self):
        for remaining in (None, 2):
            with self.subTest(remaining=remaining):
                fixture = publication_fixture.PublicationReconcileTests()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                calls = []
                def runner(command, **kwargs):
                    calls.append(kwargs['timeout'])
                    return subprocess.CompletedProcess(command, 0, '[]', '')
                environment = dict(os.environ)
                environment.pop('_ROBERT_CONTROL_DEADLINE', None)
                if remaining is not None:
                    environment['_ROBERT_CONTROL_DEADLINE'] = str(time.monotonic() + remaining)
                with mock.patch.dict(os.environ, environment, clear=True):
                    result = publish.publish_ready_actions(fixture.db_path, run_command=runner)
                self.assertTrue(result['ok'], result)
                self.assertGreaterEqual(len(calls), 2)
                if remaining is None:
                    self.assertTrue(all(timeout == 60 for timeout in calls), calls)
                else:
                    self.assertTrue(all(0 < timeout <= remaining for timeout in calls), calls)

    def test_daemon_lease_covers_rate_query_children_and_timeout_cleanup(self):
        config = dict(DAEMON_DEFAULTS)
        # A valid sequential decision can spend all of these bounded waits before
        # the next daemon heartbeat, even before maintenance/delivery overhead.
        decision_seconds = (30 + config['live_run_timeout_seconds']
                            + config['local_drain_timeout_seconds'] + 2 * (2 + 2))
        self.assertGreater(daemon._lease_ttl_seconds(config), decision_seconds)
