"""Attempt input snapshots and deterministic prompt context contracts."""
import json
from pathlib import Path
import sqlite3
import os
import unittest
from unittest.mock import patch

from tests.support import PACKAGE_ROOT
from robert_agent.context import prompts as render_prompt
from robert_agent.workflows import routes as route
from robert_agent.delivery.lock import task_publication_lock
from robert_agent.execution.application import dispatch as dispatch_queue
from robert_agent.work.application import preparation
from robert_agent.foundation import values
from tests.support.attempt_context import AttemptContextFixture


class AttemptContextTests(AttemptContextFixture):
    def test_generated_prompt_uses_existing_domain_entrypoints(self):
        prompt = self.prompt.read_text()
        for relative in ('worker/result.py', 'worker/snapshot.py',
                         'worker/heartbeat.py', 'readmodels/status.py'):
            with self.subTest(entrypoint=relative):
                target = PACKAGE_ROOT / relative
                self.assertTrue(target.is_file())
                self.assertIn(str(target), prompt)

    def test_resume_preserves_previous_attempt_inputs(self):
        before = {path: Path(path).read_bytes() for _, path, _ in self.artifacts()}
        resumed = preparation.create_resume_attempt_and_prompt(self.conn, self.data, self.db,
            self.repo, values.repository_id('example/backend'), self.task_id, self.stream,
            self.attempt_id, values.utc_now(), {'summary': 'retry'})
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
            with patch.object(dispatch_queue, 'dispatch_queued_locked') as dispatch:
                self.assertEqual(dispatch_queue.dispatch_queued(self.db, [], queue, True, 1), [])
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
        with patch.object(os, 'replace', side_effect=replace):
            with self.assertRaisesRegex(OSError, 'injected'):
                self.refresh(event)
        self.assertEqual(before, {path: Path(path).read_bytes() for path in before})
        self.assertFalse(list(self.prompt.parent.glob('.*.tmp')))
        self.assertIsNotNone(self.refresh(event))

    def test_database_failure_restores_files_and_metadata_for_retry(self):
        event = self.add_event()
        before = {path: Path(path).read_bytes() for _, path, _ in self.artifacts()}
        artifacts = self.artifacts()
        with patch.object(preparation, 'upsert_route_decision', side_effect=sqlite3.OperationalError('injected')):
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
