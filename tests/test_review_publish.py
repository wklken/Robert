from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent import audit_result, publication_reconcile, publish, review_publish, storage


class Completed:
    def __init__(self, value=None, returncode=0):
        self.stdout = json.dumps(value) if value is not None else ''
        self.returncode = returncode
        self.stderr = ''


class ReviewPublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.db = self.root / 'db.sqlite3'
        storage.init_database(self.db)
        self.now = '2026-10-25T00:00:00+00:00'
        self.head = 'a' * 40
        self.base = 'b' * 40
        self.current = {'state': 'open', 'merged': False, 'head': {'sha': self.head},
                        'base': {'sha': self.base, 'ref': 'main'}}
        self.remote_reviews = []
        self.calls = []
        self.action, self.scope = self.make_action('round-1')
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) VALUES ('repo','x/y','bot','main','/repo','/trees')")
            conn.execute("INSERT INTO workstreams(workstream_id,repo_id,lifecycle,created_at,updated_at) VALUES ('ws','repo','active',?,?)", (self.now,self.now))
        self.seed('1', self.action, self.scope)

    def make_action(self, round_id):
        body = '\n\n'.join(['# PR #42 Code Review 汇总报告', '## Report', '### Critical Issues', '### High Issues',
                              '### Medium Issues', '### Low Issues', '### Dismissed Findings', '### Agent Agreement Summary',
                              '**合并建议**：ready to merge', '[from Robert]', f'<!-- robert-review round={round_id} -->']) + '\n'
        path = self.root / round_id / 'merged-review.md'
        path.parent.mkdir()
        path.write_text(body, encoding='utf-8')
        action = {'type':'submit_pr_review', 'target_url':'https://github.com/x/y/pull/42',
                  'body':body,'round_id':round_id,'commit_id':self.head,'base_sha':self.base,'base_ref':'main',
                  'report_path':str(path),'report_sha256':hashlib.sha256(body.encode()).hexdigest()}
        scope = {'repo':'x/y','sources':[{'source_type':'pull_request','number':42}],
                 **{scoped: action[field] for field,scoped in review_publish.SCOPE_FIELDS.items()}}
        return action, scope

    def seed(self, suffix, action, scope):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE tasks SET lifecycle='completed' WHERE task_id!=?", ('task-'+suffix,))
            conn.execute("INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) VALUES (?,'ws','running',?,?)", ('task-'+suffix,self.now,self.now))
            conn.execute("INSERT INTO attempts(attempt_id,task_id,attempt_no,status) VALUES (?,?,1,'completed')", ('attempt-'+suffix,'task-'+suffix))
            conn.execute("INSERT INTO worker_results(result_id,task_id,attempt_id,output_type,created_at,metadata_json) VALUES (?,?,?,'multi_agent_review_report',?,?)", ('result-'+suffix,'task-'+suffix,'attempt-'+suffix,self.now,json.dumps({'result_contract':'compact-v1','audit':{'status':'accepted','action_scope':scope}})))
            conn.execute("INSERT INTO github_actions(action_id,result_id,task_id,action_type,target_url,audit_status,publish_status,created_at,metadata_json) VALUES (?,?,?,'submit_pr_review',?,'accepted','not_published',?,?)", ('action-'+suffix,'result-'+suffix,'task-'+suffix,action['target_url'],self.now,json.dumps(action)))
            conn.execute("INSERT INTO review_sessions(round_id,repo,pr_number,workstream_id,trigger_fingerprint,task_id,status,request_json,report_path,report_sha256,created_at,updated_at) VALUES (?,'x/y',42,'ws',?,?,'ready','{}',?,?,?,?)", (action['round_id'],'trigger-'+suffix,'task-'+suffix,action['report_path'],action['report_sha256'],self.now,self.now))
            conn.execute("UPDATE tasks SET lifecycle='completed' WHERE task_id!=?", ('task-'+suffix,))

    def runner(self, command, **kwargs):
        self.calls.append(command)
        if '--paginate' in command:
            return Completed([[],self.remote_reviews])
        if '--method' not in command:
            return Completed(self.current)
        body = next(part[5:] for part in command if part.startswith('body='))
        commit = next(part[10:] for part in command if part.startswith('commit_id='))
        review = {'id':len(self.remote_reviews)+1,'body':body,'commit_id':commit,'state':'COMMENTED',
                  'html_url':f'https://github.com/x/y/pull/42#pullrequestreview-{len(self.remote_reviews)+1}'}
        self.remote_reviews.append(review)
        return Completed(review)

    def table_action(self, action=None, action_id='action-1'):
        action = action or self.action
        return {'action_id':action_id,'action_type':'submit_pr_review','target_url':action['target_url'],
                'metadata_json':json.dumps(action)}

    def test_audits_exact_registered_file_and_no_fake_skill_requirement(self):
        result = {'output_type':'multi_agent_review_report','planned_github_actions':[self.action],
                  'used_skills':[],'consumed_event_fingerprints':['trigger-1']}
        accepted = audit_result.audit_result(result,['submit_pr_review'],required_skills=['old-review-skill'],
                                            expected_output='multi_agent_review_report',action_scope=self.scope)
        self.assertTrue(accepted['ok'],accepted)
        Path(self.action['report_path']).write_text('changed')
        self.assertFalse(audit_result.audit_result(result,['submit_pr_review'],action_scope=self.scope)['ok'])

    def test_normal_publish_and_new_round_same_visible_body_are_independent(self):
        first = publish.publish_ready_actions(self.db,run_command=self.runner)
        self.assertEqual(first['published_count'],1,first)
        second_action, second_scope = self.make_action('round-2')
        self.seed('2',second_action,second_scope)
        second = publish.publish_ready_actions(self.db,run_command=self.runner)
        self.assertEqual(second['published_count'],1,second)
        self.assertEqual(len(self.remote_reviews),2)
        self.assertTrue(all('event=COMMENT' in call for call in self.calls if '--method' in call))
        first_keys = publication_reconcile.derive_publication_keys(self.table_action(),self.scope)
        retry_keys = publication_reconcile.derive_publication_keys(self.table_action(action_id='other'),self.scope)
        second_keys = publication_reconcile.derive_publication_keys(self.table_action(second_action),second_scope)
        self.assertEqual(first_keys,retry_keys)
        self.assertNotEqual(first_keys,second_keys)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT status FROM review_sessions ORDER BY round_id').fetchall(), [('published',),('published',)])

    def test_stale_head_base_or_closed_pr_skip_without_post(self):
        for current in (self.current | {'head':{'sha':'c'*40}}, self.current | {'base':{'sha':'c'*40,'ref':'main'}},
                        self.current | {'base':{'sha':self.base,'ref':'release'}}, self.current | {'state':'closed'}):
            self.current = current
            result = review_publish.publish(self.table_action(),self.scope,self.runner)
            self.assertEqual(result['status'],'superseded',result)
        self.assertFalse(any('--method' in call for call in self.calls))
        result = publish.publish_ready_actions(self.db,run_command=self.runner)
        self.assertEqual(result['skipped_count'],1,result)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT status FROM review_sessions').fetchone()[0],'superseded')

    def test_ambiguous_post_reconciles_later_page_even_after_head_advance(self):
        def crashed(command,**kwargs):
            result = self.runner(command,**kwargs)
            if '--method' in command:
                raise RuntimeError('response lost after remote effect')
            return result
        with self.assertRaises(RuntimeError):
            publish.publish_ready_actions(self.db,run_command=crashed)
        self.current['head']['sha'] = 'c'*40
        result = publish.publish_ready_actions(self.db,run_command=self.runner)
        self.assertEqual(result['deduplicated_count'],1,result)
        self.assertEqual(len(self.remote_reviews),1)
        self.assertEqual(sum('--method' in call for call in self.calls),1)

    def test_read_error_conflicting_body_and_duplicate_markers_are_unknown(self):
        result = review_publish.reconcile(self.table_action(),self.scope,lambda *_a,**_k:Completed({'message':'forbidden'},1))
        self.assertEqual(result['status'],'unknown')
        malformed = review_publish.reconcile(self.table_action(),self.scope,lambda *_a,**_k:Completed([{'message':'unexpected'}]))
        self.assertEqual(malformed['status'],'unknown')
        self.runner(['gh','api','write','--method','POST','-f','body='+self.action['body'],'-f','commit_id='+self.head])
        self.remote_reviews[0]['body'] += 'changed'
        result = review_publish.reconcile(self.table_action(),self.scope,self.runner)
        self.assertEqual(result['status'],'unknown')
        self.remote_reviews[0]['body'] = self.action['body']
        self.remote_reviews.append(dict(self.remote_reviews[0],id=2))
        self.assertEqual(review_publish.reconcile(self.table_action(),self.scope,self.runner)['status'],'unknown')

    def test_scope_hash_symlink_and_last_moment_tamper_are_rejected(self):
        with self.assertRaises(ValueError):
            review_publish.validate_action(self.action,self.scope | {'review_round_id':'other'})
        report = Path(self.action['report_path'])
        replacement = self.root / 'elsewhere.md'
        report.rename(replacement)
        report.symlink_to(replacement)
        with self.assertRaises(ValueError):
            review_publish.validate_action(self.action,self.scope)
        report.unlink()
        replacement.rename(report)
        def changed(command,**kwargs):
            result = self.runner(command,**kwargs)
            if '--paginate' not in command:
                report.write_text('tampered')
            return result
        self.assertEqual(review_publish.publish(self.table_action(),self.scope,changed)['status'],'publish_failed')
        self.assertFalse(any('--method' in call for call in self.calls))
