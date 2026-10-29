"""Attempt input snapshots and deterministic prompt context contracts."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import os
import tempfile
import unittest
from unittest.mock import patch

from tests.support import PACKAGE_ROOT
from robert_agent import render_prompt, route, run_once
from robert_agent.publication_lock import task_publication_lock


class AttemptContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / 'data'
        self.repo = {'full_name': 'example/backend', 'github_account': 'robot',
                     'trusted_actors': ['human'], 'default_base_branch': 'main',
                     'repo_root': str(self.root / 'repo'),
                     'worktree_root': str(self.root / 'worktrees')}
        Path(self.repo['repo_root']).mkdir()
        (Path(self.repo['repo_root']) / '.git').mkdir()
        Path(self.repo['worktree_root']).mkdir()
        config = self.root / 'config.yml'
        config.write_text('data_dir: ' + str(self.data) + '\ndatabase: test.sqlite3\nrepos:\n' +
                          ''.join('  - ' + key + ': ' + str(value) + '\n' if index == 0 else
                                  '    ' + key + ': ' + str(value) + '\n'
                                  for index, (key, value) in enumerate(self.repo.items())))
        fixture = self.root / 'events.json'
        fixture.write_text(json.dumps({'events': [{'id': '1', 'number': 1,
            'source_type': 'issue', 'event_type': 'comment', 'actor_login': 'human',
            'body': '@robot fix empty input', 'intent': 'bug_fix', 'state': 'open',
            'url': 'https://github.com/example/backend/issues/1'}]}))
        result = run_once.run_once(config, workflow_path=PACKAGE_ROOT / 'resources/workflow.yml',
                                   fixture_path=fixture, dry_run=True, skip_external=True)
        self.assertTrue(result['ok'], result)
        self.db = self.data / 'test.sqlite3'
        self.conn = sqlite3.connect(self.db)
        self.addCleanup(self.conn.close)
        self.task_id, self.attempt_id = self.conn.execute('SELECT task_id,attempt_id FROM attempts').fetchone()
        self.stream = self.conn.execute('SELECT workstream_id FROM tasks').fetchone()[0]
        self.prompt = Path(result['prompt_paths'][0])
        self.route = deepcopy(route.ROUTES['update-existing-pr'])
        self.route['confidence'] = 'high'
        self.prepare = patch.object(run_once, '_prepare_worktree', return_value={
            'ok': True, 'worktree_path': str(self.root / 'worktree'), 'branch_name': 'fix'})
        self.prepare_mock = self.prepare.start()
        self.addCleanup(self.prepare.stop)
        self.effective = patch.object(run_once, '_effective_route', return_value=self.route)
        self.effective.start()
        self.addCleanup(self.effective.stop)

    def add_event(self, number=2):
        event = {'event_fingerprint': f'comment:{number}', 'event_type': 'comment',
                 'source_type': 'pull_request', 'source_key': 'example/backend!2',
                 'number': 2, 'workstream_id': self.stream, 'actor_login': 'human',
                 'body': f'@robot follow-up {number}', 'intent': 'bug_fix',
                 'has_open_dd_pr': True, 'url': 'https://github.com/example/backend/pull/2'}
        run_once._insert_source_and_event(self.conn, run_once._repo_id('example/backend'), event, run_once._now())
        self.conn.execute('INSERT INTO task_events(task_id,event_id,relationship,created_at) VALUES (?,?,?,?)',
                          (self.task_id, run_once._event_id(event['event_fingerprint']), 'context', run_once._now()))
        self.conn.commit()
        return event

    def refresh(self, event):
        return run_once._refresh_prepared_task(self.conn, self.data, self.db, self.repo,
                                               event, run_once._now(), True)

    def artifacts(self):
        return self.conn.execute("SELECT artifact_type,path,bytes FROM artifacts WHERE attempt_id=? AND artifact_type IN ('prompt','github_context_md','github_context_json')", (self.attempt_id,)).fetchall()

    def test_resume_preserves_previous_attempt_inputs(self):
        before = {path: Path(path).read_bytes() for _, path, _ in self.artifacts()}
        resumed = run_once._create_resume_attempt_and_prompt(self.conn, self.data, self.db,
            self.repo, run_once._repo_id('example/backend'), self.task_id, self.stream,
            self.attempt_id, run_once._now(), {'summary': 'retry'})
        self.assertIsNotNone(resumed)
        paths = dict(self.conn.execute('SELECT artifact_type,path FROM artifacts WHERE attempt_id=?', (resumed['attempt_id'],)))
        self.assertIn(resumed['attempt_id'] + '.github-context.md', paths['github_context_md'])
        self.assertNotIn(paths['github_context_md'], before)
        self.assertEqual(before, {path: Path(path).read_bytes() for path in before})

    def test_refresh_updates_artifacts_without_duplicate_history(self):
        self.refresh(self.add_event(2))
        self.refresh(self.add_event(3))
        rows = self.artifacts()
        self.assertEqual(len(rows), 3, rows)
        for _, path, size in rows:
            self.assertEqual(size, Path(path).stat().st_size)
        self.assertIn('follow-up 3', self.prompt.read_text())

    def test_refresh_cannot_write_while_dispatch_owns_task_lock(self):
        event = self.add_event()
        before = self.prompt.read_bytes()
        with task_publication_lock(self.db, self.task_id) as acquired:
            self.assertTrue(acquired)
            self.assertIsNone(self.refresh(event))
        self.prepare_mock.assert_not_called()
        self.assertEqual(before, self.prompt.read_bytes())

    def test_started_attempt_is_never_refreshed(self):
        event = self.add_event()
        self.conn.execute("UPDATE attempts SET status='running' WHERE attempt_id=?", (self.attempt_id,))
        self.conn.commit()
        before = self.prompt.read_bytes()
        self.assertIsNone(self.refresh(event))
        self.assertEqual(before, self.prompt.read_bytes())
        self.prepare_mock.assert_not_called()

    def test_git_failure_preserves_inputs_and_is_retryable(self):
        event = self.add_event()
        before = {path: Path(path).read_bytes() for _, path, _ in self.artifacts()}
        self.prepare_mock.side_effect = FileNotFoundError('git unavailable')
        with self.assertRaises(FileNotFoundError):
            self.refresh(event)
        self.assertEqual(before, {path: Path(path).read_bytes() for path in before})
        self.prepare_mock.side_effect = None
        self.assertIsNotNone(self.refresh(event))

    def test_refresh_lock_blocks_dispatch_until_input_is_committed(self):
        event = self.add_event()
        def prepare(*args, **kwargs):
            queue = [{'task_info': {'task_id': self.task_id}}]
            with patch.object(run_once, '_dispatch_queued_locked') as dispatch:
                self.assertEqual(run_once._dispatch_queued(self.db, [], queue, True, 1), [])
                dispatch.assert_not_called()
            # The task lock must not be confused with a SQLite writer lock.
            with sqlite3.connect(self.db) as observer:
                observer.execute('BEGIN IMMEDIATE')
            return {'ok': True}
        self.prepare_mock.side_effect = prepare
        self.assertIsNotNone(self.refresh(event))

    def test_post_git_owner_change_prevents_input_replacement(self):
        event = self.add_event()
        before = self.prompt.read_bytes()
        def prepare(*args, **kwargs):
            with sqlite3.connect(self.db) as observer:
                observer.execute("UPDATE attempts SET status='running' WHERE attempt_id=?", (self.attempt_id,))
            return {'ok': True}
        self.prepare_mock.side_effect = prepare
        self.assertIsNone(self.refresh(event))
        self.assertEqual(before, self.prompt.read_bytes())

    def test_revoked_workstream_owner_cannot_refresh(self):
        event = self.add_event()
        self.conn.execute('UPDATE workstreams SET active_task_id=NULL WHERE workstream_id=?', (self.stream,))
        self.conn.commit()
        self.assertIsNone(self.refresh(event))
        self.prepare_mock.assert_not_called()

    def test_partial_file_replace_failure_restores_inputs_and_allows_retry(self):
        event = self.add_event()
        before = {path: Path(path).read_bytes() for _, path, _ in self.artifacts()}
        original = os.replace
        calls = 0
        def replace(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('injected replace failure')
            return original(source, destination)
        with patch.object(run_once.os, 'replace', side_effect=replace):
            with self.assertRaisesRegex(OSError, 'injected'):
                self.refresh(event)
        self.assertEqual(before, {path: Path(path).read_bytes() for path in before})
        self.assertFalse(list(self.prompt.parent.glob('.*.tmp')))
        self.assertIsNotNone(self.refresh(event))

    def test_database_failure_restores_files_and_metadata_for_retry(self):
        event = self.add_event()
        before = {path: Path(path).read_bytes() for _, path, _ in self.artifacts()}
        artifacts = self.artifacts()
        with patch.object(run_once, '_upsert_route_decision', side_effect=sqlite3.OperationalError('injected')):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'injected'):
                self.refresh(event)
        self.assertEqual(before, {path: Path(path).read_bytes() for path in before})
        self.assertEqual(artifacts, self.artifacts())
        self.assertIsNotNone(self.refresh(event))

    def test_new_pending_does_not_change_already_rendered_consumption(self):
        before = self.prompt.read_text()
        self.add_event(99)
        self.assertEqual(before, self.prompt.read_text())
        self.assertNotIn('comment:99', before)


class InlineContextTests(unittest.TestCase):
    def render(self, events, **runtime):
        return render_prompt.render_prompt(
            {'task_id': 'task', 'attempt_id': 'attempt', 'workstream_id': 'stream'},
            route.ROUTES['update-existing-pr'], events,
            runtime_context={'github_context': {'md_path': '/input/context.md',
                                               'json_path': '/input/context.json'}, **runtime})

    def test_short_request_and_original_requirements_are_inline(self):
        prompt = self.render([
            {'event_fingerprint': 'comment:2', 'body': 'Fix the empty input error.'},
            {'event_fingerprint': 'issue:1', 'body': 'Accept empty UTF-8 input and keep whitespace.', 'source_type': 'issue'},
            {'event_fingerprint': 'comment:3', 'body': 'Operator reply: preserve existing output format.'}])
        self.assertIn('Fix the empty input error.', prompt)
        self.assertIn('Accept empty UTF-8 input and keep whitespace.', prompt)
        self.assertIn('Operator reply: preserve existing output format.', prompt)
        self.assertIn('untrusted', prompt)
        self.assertIn('No additional context read is required', prompt)

    def test_long_body_has_explicit_markdown_entry_without_fake_summary(self):
        body = 'must preserve every requirement\n' * 1000
        prompt = self.render([{'event_fingerprint': 'issue:long', 'body': body}])
        self.assertNotIn(body, prompt)
        self.assertIn('/input/context.md#event-1', prompt)
        self.assertIn('Read', prompt)
        self.assertIn('Do not infer requirements', prompt)
        self.assertIn('Do not read both formats by default', prompt)

    def test_current_request_precedes_large_history_and_consumption_stays_input_set(self):
        events = [{'event_fingerprint': f'old:{i}', 'body': 'history ' * 200} for i in range(10)]
        events.append({'event_fingerprint': 'current:1', 'body': 'Keep public API unchanged.'})
        prompt = self.render(events, github_context={'md_path': '/input/context.md',
            'json_path': '/input/context.json', 'current_event_fingerprint': 'current:1'})
        self.assertLess(prompt.index('Keep public API unchanged.'), prompt.index('history history'))
        payload = json.loads(prompt.split("<<'JSON'\n", 1)[1].split('\nJSON', 1)[0])
        self.assertEqual(payload['consumed_event_fingerprints'], [e['event_fingerprint'] for e in events])

    def test_comment_only_guidance_omits_open_pr_and_push_schema(self):
        prompt = render_prompt.render_prompt({'task_id': 'task', 'attempt_id': 'attempt',
            'workstream_id': 'stream'}, route.ROUTES['comment-analysis'], [], runtime_context={})
        self.assertNotIn('push_existing_pr has only type', prompt)
        self.assertNotIn('For open_pr supply', prompt)

    def test_update_template_does_not_require_push(self):
        prompt = self.render([{'event_fingerprint': 'comment:2', 'body': 'Explain current behavior.'}])
        payload = json.loads(prompt.split("<<'JSON'\n", 1)[1].split('\nJSON', 1)[0])
        self.assertEqual([a['type'] for a in payload['planned_github_actions']], ['comment'])
        self.assertIn('Only plan push_existing_pr when actual code changes', prompt)
