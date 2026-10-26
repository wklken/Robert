"""Regressions for PR #46 routing and handoff review findings PR-1 through PR-7."""
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

import yaml
from robert_agent import discover, vote_store, vote_workspace, vote_publish
from tests import test_vote_coordinator as coordinator_fixture
from tests import test_vote_files as files_fixture


class VoteRequestRemediationTests(unittest.TestCase):
    def setUp(self):
        self.vote = coordinator_fixture.VoteCoordinatorTests()
        self.vote.setUp()
        self.addCleanup(self.vote.doCleanups)

    def waiting(self):
        self.vote.clarify = True
        driver = self.vote.start()
        driver.run()
        self.vote.fixture.cycle([])
        self.vote.publish_all()
        self.vote.fixture.cycle([], skip_publish=False)
        return driver

    def test_pr1_ordinary_comparison_fix_keeps_original_route_with_vote_disabled_or_enabled(self):
        body = '@robert-bot compare main and this branch and fix the regression'
        intent = discover.infer_intent('', body, 'comment', 'robert-bot')
        self.assertEqual(intent, 'bug_fix')
        path = self.vote.fixture.config_path
        config = yaml.safe_load(path.read_text())
        config['routes'] = {}
        path.write_text(yaml.safe_dump(config))
        self.vote.fixture.cycle([{**self.vote.event(), 'body': body, 'intent': intent}])
        with vote_store.connect(self.vote.db) as conn:
            self.assertEqual(conn.execute('SELECT route_id FROM tasks').fetchone()[0], 'new-pr')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM vote_sessions').fetchone()[0], 0)
        task, _ = self.vote.fixture.current()
        self.vote.fixture.finish_task(task)
        config['routes'] = {'vote': {'worker': 'codex', 'multi_agent_vote': {
            'enabled': True, 'workers': ['codex','claude']}}}
        path.write_text(yaml.safe_dump(config))
        self.vote.fixture.cycle([{**self.vote.event('enabled-fix'), 'number': 43, 'body': body, 'intent': intent}])
        with vote_store.connect(self.vote.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM vote_sessions').fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks WHERE route_id='new-pr'").fetchone()[0], 2)

    def test_pr1_other_mentions_email_and_background_do_not_trigger_vote(self):
        for body in ['@robert-bot why does the test fail? compare the logs and the CI config',
                     '@robert-bot 修复这个 bug，可以对比一下之前的方案',
                     'cc @alice we should compare option A and B. @robert-bot please implement A',
                     'Contact me at a@b.com about the vote', '@alice vote for A',
                     '@robert-bot fix the vote implementation']:
            with self.subTest(body=body):
                self.assertNotEqual(discover.infer_intent('', body, 'comment', 'robert-bot'), 'vote')

    def test_pr1_configured_account_is_required_in_discovery_and_classifier_routing(self):
        body = '@custom-bot 投票 A/B'
        self.assertEqual(discover.infer_intent('', body, 'comment', 'custom-bot'), 'vote')
        self.assertNotEqual(discover.infer_intent('', body, 'comment', 'robert-bot'), 'vote')
        self.assertNotEqual(discover.infer_intent('', '@robert-bot-other vote A/B', 'comment', 'robert-bot'), 'vote')
        self.assertEqual(discover._issue_search_to_raw({'number': 42, 'body': body}, 'mention', 'custom-bot')['intent'], 'vote')
        from robert_agent import run_once, validate_config
        config = validate_config.validate_config(self.vote.fixture.config_path, skip_external=True)
        event = {**self.vote.event(), 'body': '@alice choose A or B', 'mentions_dd': True}
        route = run_once._effective_route(config, config['repos'][0], event, route_id='vote')
        self.assertNotIn('native_vote_workers', route)

    def test_pr1_polite_fix_and_explanation_requests_do_not_vote_on_a_function_name(self):
        for body in ['@robert-bot can you fix the vote implementation',
                     '@robert-bot please help fix the vote bug',
                     '@robert-bot 请帮我修复投票功能的 bug',
                     '@robert-bot explain why the vote function fails']:
            with self.subTest(body=body):
                self.assertNotEqual(discover.infer_intent('', body, 'comment', 'robert-bot'), 'vote')

    def test_pr3_independent_fix_supersedes_waiting_vote_instead_of_orphaning_it(self):
        driver = self.waiting()
        body = '@robert-bot fix the regression'
        self.vote.fixture.cycle([{**self.vote.event('independent-fix'), 'body': body,
                                 'intent': discover.infer_intent('', body, 'comment', 'robert-bot')}])
        with vote_store.connect(self.vote.db) as conn:
            entry = vote_store.session(conn, round_id=driver.round_id)
            self.assertEqual(entry['status'], 'superseded')
            self.assertEqual(entry['request']['event'].get('clarifications', []), [])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks WHERE route_id='new-pr' AND lifecycle='queued'").fetchone()[0], 1)

    def test_pr2_a_comment_already_linked_to_a_task_never_becomes_a_new_vote(self):
        event = self.vote.event()
        self.vote.fixture.cycle([{**event, 'intent': 'bug_fix'}])
        task, _ = self.vote.fixture.current()
        self.vote.fixture.finish_task(task)
        self.vote.fixture.cycle([event])
        with vote_store.connect(self.vote.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM vote_sessions').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 1)

    def test_pr2_enabling_vote_does_not_replay_a_previously_disabled_request(self):
        path = self.vote.fixture.config_path
        config = yaml.safe_load(path.read_text())
        enabled_routes = config['routes']
        config['routes'] = {}
        path.write_text(yaml.safe_dump(config))
        event = self.vote.event()
        self.vote.fixture.cycle([event])
        task, _ = self.vote.fixture.current()
        self.vote.fixture.finish_task(task)
        config['routes'] = enabled_routes
        path.write_text(yaml.safe_dump(config))
        self.vote.fixture.cycle([event])
        with vote_store.connect(self.vote.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM vote_sessions').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 1)

    def test_pr3_waiting_vote_does_not_swallow_an_independent_pr_review_request(self):
        self.vote.issue = False
        path = self.vote.fixture.config_path
        config = yaml.safe_load(path.read_text())
        config['routes']['review-pr'] = {'worker': 'codex', 'multi_agent_review': {
            'enabled': True, 'workers': ['codex','claude'], 'author_allowlist': ['author']}}
        path.write_text(yaml.safe_dump(config))
        driver = self.waiting()
        body = '@robert-bot please review'
        self.vote.fixture.cycle([{**self.vote.event('review-next'), 'body': body,
                                 'intent': discover.infer_intent('', body, 'comment', 'robert-bot')}])
        with vote_store.connect(self.vote.db) as conn:
            entry = vote_store.session(conn, round_id=driver.round_id)
            self.assertEqual(entry['status'], 'waiting_for_user')
            self.assertEqual(entry['request']['event'].get('clarifications', []), [])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM review_sessions').fetchone()[0], 1)

    def test_pr4_option_comparison_reply_continues_the_waiting_round(self):
        driver = self.waiting()
        self.vote.clarify = False
        body = '@robert-bot 比较 A 和 B 两个方案，延迟目标是 1 秒'
        self.vote.fixture.cycle([{**self.vote.event('answer'), 'body': body,
                                 'intent': discover.infer_intent('', body, 'comment', 'robert-bot')}])
        with vote_store.connect(self.vote.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM vote_sessions').fetchone()[0], 1)
        resumed = self.vote.driver()
        self.assertEqual(resumed.round_id, driver.round_id)
        self.assertTrue(resumed.run().is_file())
        with vote_store.connect(self.vote.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM vote_sessions').fetchone()[0], 1)
            entry = vote_store.session(conn, round_id=driver.round_id)
            self.assertEqual(len(entry['request']['event']['clarifications']), 1)

    def test_pr5_issue_code_is_pinned_in_a_new_checkout_after_clarification_and_head_move(self):
        self.vote.need_code = True
        first = self.waiting()
        original_tree = Path(first.manifest['source_worktree'])
        original_contents = (original_tree / 'source.py').read_text()
        self.vote.fixture.git_fixture.commit('value = 9\n')
        self.vote.clarify = False
        self.vote.fixture.cycle([{**self.vote.event('answer'), 'body': '@robert-bot latency is one second', 'intent': 'analysis'}])
        resumed = self.vote.driver()
        self.assertTrue(resumed.run().is_file())
        self.assertNotEqual(first.manifest['source_worktree'], resumed.manifest['source_worktree'])
        self.assertEqual(resumed.manifest['head_sha'], self.vote.fixture.git_fixture.head)
        self.assertEqual((original_tree / 'source.py').read_text(), original_contents)
        self.assertTrue(vote_workspace.verify_snapshot(resumed.manifest))


class VoteBallotRemediationTests(unittest.TestCase):
    def test_pr6_optional_abstention_reason_is_validated_before_sealing_a_choice(self):
        fixture = files_fixture.VoteFileTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        for reason in ([1], {'reason': 'bad shape'}, True, None, 'x' * 4001):
            with self.subTest(reason=reason), self.assertRaises(ValueError):
                fixture.read({**fixture.ballot, 'abstain_reason': reason})


class VotePublicationRemediationTests(unittest.TestCase):
    def setUp(self):
        self.vote = coordinator_fixture.VoteCoordinatorTests()
        self.vote.setUp()
        self.addCleanup(self.vote.doCleanups)

    def make_report(self, count=1):
        source = self.vote.source()
        adopted = [{'id': i, 'kind': 'comment', 'body': f'context-{i}', 'user': {'login': 'maintainer'}} for i in range(1, count+1)]
        source['discussion'] = adopted
        self.vote.source = lambda *args: source
        driver, action, scope = self.vote.publication()
        return source, adopted, driver, action, scope

    def test_pr7_deleted_adopted_comment_supersedes_without_retry_or_post(self):
        source, _, _, action, scope = self.make_report()
        def transport(command, **kwargs):
            self.assertNotIn('--method', command)
            if 'comments?' in command[2]:
                return SimpleNamespace(returncode=0, stdout='[[]]')
            if '/issues/comments/' in command[2]:
                return SimpleNamespace(returncode=1, stdout='{"message":"Not Found","status":"404"}')
            return SimpleNamespace(returncode=0, stdout=json.dumps(source))
        self.assertEqual(vote_publish.publish(action, scope, transport)['status'], 'superseded')

    def test_pr7_many_adopted_comments_are_read_with_bounded_paginated_queries(self):
        source, adopted, _, action, scope = self.make_report(count=100)
        calls = []
        def transport(command, **kwargs):
            calls.append(command)
            if '--method' in command:
                body = next(v[5:] for v in command if v.startswith('body='))
                value = {'id': 101, 'body': body, 'user': {'login': 'robert-bot'}}
            elif 'comments?' in command[2]:
                value = [[], adopted]
            elif '/issues/comments/' in command[2]:
                value = adopted[int(command[2].split('/')[-1])-1]
            else:
                value = source
            return SimpleNamespace(returncode=0, stdout=json.dumps(value))
        self.assertTrue(vote_publish.publish(action, scope, transport)['ok'])
        self.assertLessEqual(len(calls), 4)
        self.assertFalse(any('/issues/comments/' in c[2] for c in calls))

    def test_pr7_incomplete_or_timed_out_snapshot_is_unknown_not_deleted(self):
        import subprocess
        source, _, _, action, scope = self.make_report()
        for timed_out in (False, True):
            comment_queries = []
            def transport(command, **kwargs):
                self.assertNotIn('--method', command)
                if 'comments?' in command[2]:
                    comment_queries.append(command)
                    if len(comment_queries) > 1:
                        if timed_out:
                            raise subprocess.TimeoutExpired(command, 60)
                        return SimpleNamespace(returncode=1, stdout='partial page')
                    return SimpleNamespace(returncode=0, stdout='[[]]')
                return SimpleNamespace(returncode=0, stdout=json.dumps(source))
            with self.subTest(timed_out=timed_out):
                self.assertEqual(vote_publish.publish(action, scope, transport)['status'], 'publish_failed')
