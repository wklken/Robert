"""Fresh trusted requests start independent subjects after terminal executions."""
from robert_agent.work.application import synchronization as synchronization
from robert_agent.work import scan_batches as scan_batches
from robert_agent.adapters.github import sources as github_sources
from contextlib import closing
import json
import sqlite3
from unittest import mock

from robert_agent.adapters.sqlite import execution_control as execution_store
from robert_agent.runtime import cycle as run_once
from robert_agent.execution import workspace as assigned_workspace
from robert_agent.work.application import source_events
from robert_agent.foundation import values
from tests.support.terminal_github_followup import TerminalGithubFollowupFixture


class TerminalGithubFollowupTests(TerminalGithubFollowupFixture):
    def test_completed_issue_and_pr_followup_start_once_and_preserve_history(self):
        for source_type in ('issue', 'pull_request'):
            with self.subTest(source_type=source_type):
                if source_type == 'pull_request':
                    self.fixture.doCleanups()
                    self.setUp()
                self.source_type = source_type
                self.finish()
                result = self.deliver('followup', body='@robot is there a cached decorator for functions and methods?', intent='unclear')
                self.assertEqual(len(result['prompt_paths']), 1)
                rows = self.tasks()
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[1][1], self.parent_id)
                with closing(sqlite3.connect(self.db_path)) as conn:
                    self.assertEqual(execution_store.get_binding(conn, 'task', self.parent_id), self.old_binding)
                    self.assertEqual(execution_store.get_binding(conn, 'task', rows[1][0])['binding_status'], 'active')
                    links = conn.execute('SELECT event_id,relationship FROM task_events WHERE task_id=?', (rows[1][0],)).fetchall()
                    self.assertEqual(links, [('event:comment:followup', 'trigger')])
                    self.assertIsNone(conn.execute('SELECT completed_at FROM work_items').fetchone()[0])
                self.deliver('followup')
                with closing(sqlite3.connect(self.db_path)) as conn, conn:
                    conn.execute("UPDATE tasks SET lifecycle='completed' WHERE task_id=?", (rows[1][0],))
                    conn.execute("UPDATE attempts SET status='completed',metadata_json='{}' WHERE task_id=?", (rows[1][0],))
                    conn.execute("UPDATE workstreams SET lifecycle='completed',active_task_id=NULL")
                    execution_store.close_binding(conn, 'task', rows[1][0], values.utc_now())
                self.deliver('followup')
                self.assertEqual(len(self.tasks()), 2)

    def test_failed_closed_parent_retry_preserves_original_trigger_and_new_context(self):
        self.finish('failed')
        self.deliver('retry', body='@robot also compare method decorators')
        rows = self.tasks()
        self.assertEqual(len(rows), 2)
        with closing(sqlite3.connect(self.db_path)) as conn:
            links = conn.execute('SELECT event_id,relationship FROM task_events WHERE task_id=? ORDER BY relationship', (rows[1][0],)).fetchall()
            self.assertEqual(links, [('event:comment:retry', 'context'), ('event:comment:first', 'trigger')])
            self.assertEqual(execution_store.get_binding(conn, 'task', self.parent_id), self.old_binding)
        self.deliver('retry')
        self.assertEqual(len(self.tasks()), 2)

    def test_untrusted_and_bot_mentions_do_not_reopen_terminal_task(self):
        self.finish()
        self.deliver('untrusted', actor='outsider')
        self.deliver('bot', actor='robot')
        self.assertEqual(len(self.tasks()), 1)

    def test_terminal_followup_waits_for_process_publication_attention_and_cancellation(self):
        self.finish()
        cases = [
            ("UPDATE attempts SET status='running'", "UPDATE attempts SET status='completed'"),
            ("UPDATE attempts SET metadata_json=json_set(metadata_json,'$.dispatch.pid',99999)", "UPDATE attempts SET metadata_json='{\"result_contract\":\"compact-v1\"}'"),
            ("UPDATE execution_subjects SET binding_status='attention'", "UPDATE execution_subjects SET binding_status='terminal'"),
            ("UPDATE tasks SET metadata_json='{\"execution_attention\":{\"reason_code\":\"protocol\"}}'", "UPDATE tasks SET metadata_json='{}'"),
            ("UPDATE work_items SET canceled_at='2026-10-31T00:00:00Z'", "UPDATE work_items SET canceled_at=NULL"),
            ("UPDATE workstreams SET lifecycle='canceled'", "UPDATE workstreams SET lifecycle='completed'"),
        ]
        for i, (block, release) in enumerate(cases):
            with self.subTest(block=block):
                with closing(sqlite3.connect(self.db_path)) as conn, conn:
                    conn.execute(block)
                self.deliver(f'blocked-{i}')
                self.assertEqual(len(self.tasks()), 1)
                with closing(sqlite3.connect(self.db_path)) as conn, conn:
                    conn.execute(release)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("INSERT INTO github_actions(action_id,task_id,action_type,audit_status,publish_status,created_at,metadata_json) SELECT 'pending',task_id,'comment','accepted','not_published',?, '{}' FROM attempts", (values.utc_now(),))
        self.deliver('publication-pending')
        self.assertEqual(len(self.tasks()), 1)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE github_actions SET publish_status='published'")
        self.deliver('publication-pending')
        self.assertEqual(len(self.tasks()), 2)

    def test_rechecks_cancellation_after_worktree_io(self):
        self.finish()
        original = assigned_workspace.prepare_worktree
        def cancel(*args, **kwargs):
            with closing(sqlite3.connect(self.db_path)) as conn, conn:
                conn.execute("UPDATE work_items SET canceled_at='2026-10-31T00:00:00Z'")
                conn.execute("UPDATE workstreams SET lifecycle='canceled'")
            return original(*args, **kwargs)
        with mock.patch.object(assigned_workspace, 'prepare_worktree', side_effect=cancel):
            self.deliver('racing-cancel')
        self.assertEqual(len(self.tasks()), 1)

    def test_scanner_reobserves_unlinked_followup_after_original_batch_finished(self):
        from robert_agent.configuration import command as validate_config
        from robert_agent.adapters.github.read import GitHubReadSession
        self.finish()
        repo = validate_config.validate_config(self.fixture.config_path, skip_external=True)['repos'][0]
        event = {'id': 'lost', 'number': 123, 'source_type': 'issue', 'event_type': 'comment',
                 'actor_login': 'owner', 'body': '@robot compare decorators', 'state': 'open'}
        from robert_agent.security import authorization as authorize
        from robert_agent.adapters.github import discovery as discover
        decision = authorize.authorize_events(discover.normalize_events([event], repo), repo,
                                              known_workstreams={self.stream_id})[0]
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            source_events.insert_source_and_event(conn, self.fixture.repo_id, decision, values.utc_now())
        seen = []
        def collect(_repo, **kwargs):
            seen.extend(kwargs['notification_hints'])
            return []  # Even a notification that omits this comment must not lose it.
        config = validate_config.validate_config(self.fixture.config_path, skip_external=True)
        session = GitHubReadSession(self.db_path, account='robot')
        with mock.patch.object(session, 'collect_events', side_effect=collect), \
                mock.patch.object(github_sources, 'read_source_state', return_value={'state': 'open'}), \
                mock.patch('robert_agent.adapters.github.discovery._source_metadata', return_value=({'number': 123, 'source_type': 'issue', 'event_type': 'notification', 'state': 'open'}, 'open')), \
                mock.patch('robert_agent.adapters.github.discovery._enrich_known_workstream_contexts', return_value=[event]):
            batch_id = synchronization.scan_repo(config, repo, session, [])
        batch = scan_batches.next_batch(self.db_path, self.fixture.repo_id)
        self.assertEqual(batch['batch_id'], batch_id)
        self.assertTrue(any(raw.get('id') == 'lost' for raw in batch['raw_events']))
        self.assertTrue(any(h['number'] == 123 for h in seen))

    def test_closed_source_after_worktree_io_does_not_admit_followup(self):
        self.finish()
        original = assigned_workspace.prepare_worktree
        def close(*args, **kwargs):
            with closing(sqlite3.connect(self.db_path)) as conn, conn:
                conn.execute("UPDATE github_sources SET state='closed'")
            return original(*args, **kwargs)
        with mock.patch.object(assigned_workspace, 'prepare_worktree', side_effect=close):
            self.deliver('racing-close')
        self.assertEqual(len(self.tasks()), 1)

    def test_waiting_reply_does_not_overwrite_cancellation_during_worktree_io(self):
        self.deliver('first')
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            self.parent_id = conn.execute('SELECT task_id FROM tasks').fetchone()[0]
            conn.execute("UPDATE tasks SET lifecycle='waiting_for_user'")
            conn.execute("UPDATE workstreams SET lifecycle='waiting_for_user'")
            conn.execute("UPDATE attempts SET status='completed',metadata_json='{}'")
        original = assigned_workspace.prepare_worktree
        def cancel(*args, **kwargs):
            with closing(sqlite3.connect(self.db_path)) as conn, conn:
                conn.execute("UPDATE tasks SET lifecycle='canceled'")
                conn.execute("UPDATE workstreams SET lifecycle='canceled',active_task_id=NULL")
                conn.execute("UPDATE work_items SET canceled_at='2026-10-31T00:00:00Z'")
                execution_store.close_binding(conn, 'task', self.parent_id, values.utc_now())
            return original(*args, **kwargs)
        with mock.patch.object(assigned_workspace, 'prepare_worktree', side_effect=cancel):
            self.deliver('waiting-reply')
        self.assertEqual(self.tasks(), [(self.parent_id, None, 'canceled')])

    def test_new_owner_during_worktree_io_keeps_followup_unlinked(self):
        self.finish()
        original = assigned_workspace.prepare_worktree
        def occupy(*args, **kwargs):
            with closing(sqlite3.connect(self.db_path)) as conn, conn:
                conn.execute("UPDATE workstreams SET lifecycle='active',active_task_id=?", (self.parent_id,))
            return original(*args, **kwargs)
        with mock.patch.object(assigned_workspace, 'prepare_worktree', side_effect=occupy):
            self.deliver('racing-owner')
        self.assertEqual(len(self.tasks()), 1)
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM task_events WHERE event_id='event:comment:racing-owner'").fetchone())

    def test_scanner_does_not_recover_requests_after_trust_revocation_or_cancellation(self):
        from robert_agent.security import authorization as authorize
        from robert_agent.adapters.github import discovery as discover
        from robert_agent.configuration import command as validate_config
        from robert_agent.adapters.github.read import GitHubReadSession
        self.finish()
        config = validate_config.validate_config(self.fixture.config_path, skip_external=True)
        repo = config['repos'][0]
        event = {'id': 'lost', 'number': 123, 'source_type': 'issue', 'event_type': 'comment',
                 'actor_login': 'owner', 'body': '@robot compare decorators', 'state': 'open'}
        decision = authorize.authorize_events(discover.normalize_events([event], repo), repo,
                                              known_workstreams={self.stream_id})[0]
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            source_events.insert_source_and_event(conn, self.fixture.repo_id, decision, values.utc_now())
        cases = [
            ({**repo, 'trusted_actors': []}, None),
            (repo, "UPDATE work_items SET canceled_at='2026-10-31T00:00:00Z'"),
        ]
        for current_repo, mutation in cases:
            with self.subTest(mutation=mutation):
                if mutation:
                    with closing(sqlite3.connect(self.db_path)) as conn, conn:
                        conn.execute(mutation)
                session = GitHubReadSession(self.db_path, account='robot')
                with mock.patch.object(session, 'collect_events', return_value=[]) as collect, \
                        mock.patch('robert_agent.adapters.github.discovery._source_metadata', side_effect=AssertionError('recovered blocked source')):
                    synchronization.scan_repo(config, current_repo, session, [])
                self.assertEqual(collect.call_args.kwargs['notification_hints'], [])

    def test_scanner_preserves_existing_pr_metadata_for_recovered_comments(self):
        from robert_agent.security import authorization as authorize
        from robert_agent.adapters.github import discovery as discover
        from robert_agent.configuration import command as validate_config
        from robert_agent.adapters.github.read import GitHubReadSession
        self.source_type = 'pull_request'
        self.finish()
        config = validate_config.validate_config(self.fixture.config_path, skip_external=True)
        repo = config['repos'][0]
        event = {'id': 'lost-review', 'number': 123, 'source_type': 'pull_request', 'event_type': 'comment',
                 'actor_login': 'owner', 'body': '@robot update the existing branch', 'state': 'open'}
        decision = authorize.authorize_events(discover.normalize_events([event], repo), repo,
                                              known_workstreams={self.stream_id})[0]
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            source_events.insert_source_and_event(conn, self.fixture.repo_id, decision, values.utc_now())
        session = GitHubReadSession(self.db_path, account='robot')
        def discussion(raw, _repo, _runner):
            return [{**raw, **event}]
        metadata = {'number': 123, 'source_type': 'pull_request', 'event_type': 'notification', 'state': 'open'}
        with mock.patch.object(session, 'collect_events', return_value=[]), \
                mock.patch.object(github_sources, 'read_source_state', return_value={'state': 'open'}), \
                mock.patch.object(discover, '_source_metadata', return_value=(metadata, 'open')), \
                mock.patch.object(discover, '_enrich_pull_request_metadata', return_value={**metadata,
                    'has_open_dd_pr': True, 'existing_pr_head_branch': 'codex/existing',
                    'origin_workstream_id': 'github:example/repo#99'}), \
                mock.patch.object(discover, '_enrich_known_workstream_contexts', side_effect=discussion):
            synchronization.scan_repo(config, repo, session, [])
        recovered = scan_batches.next_batch(self.db_path, self.fixture.repo_id)['raw_events'][0]
        self.assertEqual(recovered['existing_pr_head_branch'], 'codex/existing')
        self.assertEqual(recovered['origin_workstream_id'], 'github:example/repo#99')

    def test_followup_question_is_not_routed_as_code_fix_from_old_issue_title(self):
        self.finish()
        path = self.fixture.root / 'comments.json'
        path.write_text(json.dumps({'events': [{
            'id': 'question', 'number': 123, 'source_type': 'issue',
            'event_type': 'comment', 'actor_login': 'owner',
            'title': 'Research: OpenAPI import bug / 报错',
            'body': '@robot can Prance configuration remove this limit? Are there recommended alternatives?',
            'intent': 'bug_fix', 'state': 'open',
        }]}))
        result = run_once.run_once(self.fixture.config_path, fixture_path=path,
                                  dry_run=True, skip_external=True, skip_publish=True)
        self.assertTrue(result['ok'], result)
        with closing(sqlite3.connect(self.db_path)) as conn:
            route = conn.execute('SELECT route_id FROM tasks WHERE parent_task_id=?', (self.parent_id,)).fetchone()
        self.assertEqual(route, ('classification-result',))
