"""Durable questions must survive process restarts and resolve with operator input."""
from contextlib import closing
import json
import sqlite3

from tests.support.work_items import WorkItemCommandFixture
from robert_agent.work.application import commands


class AttentionOutboxTests(WorkItemCommandFixture):
    def test_question_and_notification_commit_and_resolve_together(self):
        item = self._create(start=True)['item']
        question = self._question(item['work_item_id'], 1)
        with closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute("SELECT notification_id,metadata_json FROM notifications WHERE notification_type='operator_question'").fetchall()
        self.assertEqual(len(rows), 1)
        metadata = json.loads(rows[0][1])
        self.assertEqual(metadata['attention_event_id'], question['event_id'])
        self.assertEqual(metadata['work_item_id'], item['work_item_id'])
        commands.execute_command(self.db_path, item['work_item_id'], context=self.context,
            command='reply', expected_version=item['version'], expected_event_id=question['event_id'],
            idempotency_key='reply', body='Use SQLite.')
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute('SELECT status FROM notifications WHERE notification_id=?', (rows[0][0],)).fetchone()[0], 'resolved')
