from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from tests import support  # noqa: F401
from robert_agent import board, execution_store, storage, workbench, work_items


class WebReviewRemediationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'state.sqlite3'
        storage.init_database(self.db)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) VALUES ('r','o/r','bot','main','/r','/w')")
        self.context = work_items.CommandContext(actor_kind='operator', actor_identity='owner',
            allowed_repo_ids=frozenset({'r'}), allowed_workers=frozenset({'default'}))

    def create(self, suffix, *, cancel=False):
        item = work_items.create_work_item(self.db, context=self.context, repo_id='r',
            title=suffix, description='Task', priority='P2', routing_mode='auto',
            requested_worker=None, start=True, idempotency_key='create-' + suffix)['item']
        if cancel:
            work_items.execute_command(self.db, item['work_item_id'], context=self.context,
                command='cancel', expected_version=item['version'], idempotency_key='cancel-' + suffix)
        return item

    def test_operator_canceled_local_work_leaves_attention_inbox(self):
        item = self.create('canceled', cancel=True)
        self.assertEqual(workbench.list_work_items(self.db)['items'], [])
        detail = workbench.get_work_item(self.db, item['workstream_id'])
        self.assertEqual(detail['operator_state']['bucket'], 'history')
        self.assertEqual(detail['operator_state']['reason_code'], 'canceled')

    def test_cancellation_does_not_hide_execution_attention_or_pending_action(self):
        item = self.create('attention', cancel=True)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            task = conn.execute('SELECT task_id FROM tasks WHERE workstream_id=?', (item['workstream_id'],)).fetchone()[0]
            conn.execute("INSERT INTO execution_subjects(subject_type,subject_id,workflow_kind,binding_status,created_at,updated_at,metadata_json) VALUES ('task',?,'human_request','attention','2026-10-29','2026-10-29',?)", (task, json.dumps({'reason_code': 'cleanup_pending'})))
        self.assertEqual(workbench.get_work_item(self.db, item['workstream_id'])['operator_state']['bucket'], 'needs_attention')
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE execution_subjects SET binding_status='terminal' WHERE subject_id=?", (task,))
            conn.execute("INSERT INTO github_actions(action_id,task_id,action_type,audit_status,publish_status,created_at) VALUES ('pending',?,'comment','pending','not_published','2026-10-29')", (task,))
        self.assertNotEqual(workbench.get_work_item(self.db, item['workstream_id'])['operator_state']['bucket'], 'history')

    def test_cancellation_does_not_hide_a_derived_worker_that_has_not_stopped(self):
        item = self.create('derived-worker', cancel=True)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO workstreams(workstream_id,repo_id,origin_workstream_id,lifecycle,created_at,updated_at) VALUES ('child','r',?,'active','2026-10-29','2026-10-29')", (item['workstream_id'],))
            conn.execute("INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) VALUES ('child-task','child','completed','2026-10-29','2026-10-29')")
            conn.execute("INSERT INTO attempts(attempt_id,task_id,attempt_no,status,metadata_json) VALUES ('child-attempt','child-task',1,'completed',?)", (json.dumps({'dispatch': {'pid': 123}}),))
        self.assertNotEqual(workbench.get_work_item(self.db, item['workstream_id'])['operator_state']['bucket'], 'history')

    def test_detail_preserves_ancestor_lifecycle_without_loading_unrelated_work(self):
        item = self.create('root')
        self.create('unrelated')
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO workstreams(workstream_id,repo_id,origin_workstream_id,lifecycle,created_at,updated_at) VALUES ('child','r',?,'active','2026-10-29','2026-10-29')", (item['workstream_id'],))
        with mock.patch.object(board, '_project_item', wraps=board._project_item) as project:
            detail = workbench.get_work_item(self.db, 'child')
        self.assertEqual(detail['lifecycle']['work_item_id'], item['work_item_id'])
        self.assertEqual(project.call_count, 1)

    def test_hidden_history_does_not_build_card_projections(self):
        for index in range(12):
            self.create(str(index), cancel=True)
        with mock.patch.object(board, '_project_item', wraps=board._project_item) as project:
            self.assertEqual(board.list_board(self.db, limit=1)['items'], [])
        self.assertEqual(project.call_count, 0)

    def test_board_reads_controls_once_for_all_visible_cards(self):
        for index in range(8):
            self.create(str(index))
        with mock.patch.object(execution_store, 'read_controls', wraps=execution_store.read_controls) as read:
            result = board.list_board(self.db, limit=2)
        self.assertEqual(len(result['items']), 2)
        self.assertEqual(result['counts']['todo'], 8)
        self.assertEqual(read.call_count, 1)

    def test_repository_filter_is_applied_before_projecting_cards(self):
        self.create('selected')
        other = self.create('other')
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) VALUES ('other','o/other','bot','main','/other','/w-other')")
            conn.execute("UPDATE work_items SET repo_id='other' WHERE work_item_id=?", (other['work_item_id'],))
            conn.execute("UPDATE workstreams SET repo_id='other' WHERE workstream_id=?", (other['workstream_id'],))
        with mock.patch.object(board, '_project_item', wraps=board._project_item) as project:
            result = board.list_board(self.db, repo='o/r', limit=1)
        self.assertEqual(result['counts']['todo'], 1)
        self.assertEqual(project.call_count, 1)

    def test_scoped_controls_preserve_neutral_precedence_and_exclude_other_subjects(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO execution_subjects(subject_type,subject_id,workflow_kind,binding_status,created_at,updated_at) VALUES ('task','included','human_request','terminal','2026-10-29','2026-10-29')")
            conn.execute("INSERT INTO workflow_bindings(subject_type,subject_id,workflow_kind,owner_engine,binding_status,created_at,updated_at) VALUES ('task','included','human_request','legacy','attention','2026-10-29','2026-10-29')")
            conn.execute("INSERT INTO execution_subjects(subject_type,subject_id,workflow_kind,binding_status,created_at,updated_at) VALUES ('task','unrelated','human_request','attention','2026-10-29','2026-10-29')")
            subjects = [('task', 'included'), *(('task', str(index)) for index in range(250))]
            controls = execution_store.read_controls(conn, subjects)
        self.assertEqual(set(controls), {('task', 'included')})
        self.assertEqual(controls[('task', 'included')]['binding_status'], 'terminal')

    def test_workbench_detail_does_not_project_unrelated_history(self):
        target = self.create('target')
        for index in range(12):
            self.create(str(index), cancel=True)
        with mock.patch.object(board, '_project_item', wraps=board._project_item) as project:
            detail = workbench.get_work_item(self.db, target['workstream_id'])
        self.assertEqual(detail['id'], target['workstream_id'])
        self.assertEqual(project.call_count, 1)
