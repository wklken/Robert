from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tests.support import PACKAGE_ROOT  # noqa: F401
import yaml
from robert_agent.init_config import init_config
from robert_agent.run_once import run_once
from robert_agent.worker.result import record_result


class ChildRoutePolicyTests(unittest.TestCase):
    def test_classification_child_preserves_missing_required_skill_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / 'repo'
            repo.mkdir()
            (repo / '.git').mkdir()
            config_path = root / 'config.yml'
            init_config(config_path, {'repo': 'example/backend', 'repo_path': str(repo),
                        'worker': 'codex', 'github_account': 'robert-bot',
                        'trusted_actor': 'maintainer'}, non_interactive=True)
            config = yaml.safe_load(config_path.read_text())
            config['data_dir'] = str(root / 'data')
            config['routes'] = {'new-pr': {'required_skills': ['missing-required-review-repro']}}
            config_path.write_text(yaml.safe_dump(config, sort_keys=False))
            fixture = root / 'fixture.json'
            fixture.write_text(json.dumps({'events': [{'id': 'unclear-42', 'number': 42,
                'source_type': 'issue', 'event_type': 'comment', 'actor_login': 'maintainer',
                'body': '@robert-bot please classify', 'intent': 'unclear'}]}))
            first = run_once(config_path, fixture_path=fixture, dry_run=True, skip_external=True)
            self.assertTrue(first['ok'], first)
            db = root / 'data' / 'robert.sqlite3'
            with closing(sqlite3.connect(db)) as conn:
                task_id, attempt_id = conn.execute('SELECT task_id,attempt_id FROM attempts').fetchone()
            result = record_result(db, {'task_id': task_id, 'attempt_id': attempt_id, 'output_type': 'classification_result',
                'submission': {'planned_github_actions': [], 'consumed_event_fingerprints': ['comment:unclear-42'],
                    'verification': [], 'used_skills': [], 'handoff': 'Classified request',
                    'recommended_route': 'new-pr', 'branch_slug': 'classified-change'}})
            self.assertTrue(result['ok'], result)
            second = run_once(config_path, dry_run=True, skip_external=True)
            self.assertTrue(second['ok'], second)
            with closing(sqlite3.connect(db)) as conn:
                lifecycle, required = conn.execute('''SELECT t.lifecycle,rd.required_skills_json
                    FROM tasks t JOIN route_decisions rd ON rd.task_id=t.task_id
                    WHERE t.route_id='new-pr' ''').fetchone()
            self.assertEqual(lifecycle, 'failed')
            self.assertEqual(json.loads(required), ['missing-required-review-repro'])
            self.assertEqual(second['dispatch_count'], 0)

    def test_local_task_preserves_required_skill_gate(self):
        from robert_agent import work_items

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / 'repo'
            repo.mkdir()
            (repo / '.git').mkdir()
            config_path = root / 'config.yml'
            init_config(config_path, {'repo': 'example/backend', 'repo_path': str(repo),
                        'worker': 'codex', 'github_account': 'robert-bot',
                        'trusted_actor': 'maintainer'}, non_interactive=True)
            config = yaml.safe_load(config_path.read_text())
            config['data_dir'] = str(root / 'data')
            config['routes'] = {'local-result': {'required_skills': ['missing-required-review-repro']}}
            config_path.write_text(yaml.safe_dump(config, sort_keys=False))
            run_once(config_path, dry_run=True, skip_external=True)
            db = root / 'data' / 'robert.sqlite3'
            with closing(sqlite3.connect(db)) as conn:
                repo_id = conn.execute('SELECT repo_id FROM repos').fetchone()[0]
            work_items.create_work_item(db,
                context=work_items.CommandContext('operator', 'owner', frozenset({repo_id}), frozenset()),
                repo_id=repo_id, title='Analyze this code', description='Please analyze the code',
                priority='P2', routing_mode='auto', requested_worker=None, start=True,
                idempotency_key='local-policy')
            result = run_once(config_path, dry_run=True, skip_external=True)
            self.assertTrue(result['ok'], result)
            self.assertEqual(result['dispatch_count'], 0)
            with closing(sqlite3.connect(db)) as conn:
                state, required = conn.execute('SELECT t.lifecycle,rd.required_skills_json FROM tasks t '
                    'JOIN route_decisions rd ON rd.task_id=t.task_id').fetchone()
            self.assertEqual(state, 'failed')
            self.assertEqual(json.loads(required), ['missing-required-review-repro'])
