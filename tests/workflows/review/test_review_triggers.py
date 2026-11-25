import json
import shlex
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from robert_agent.configuration import command as validate_config
from robert_agent.delivery.application import publish
from robert_agent.persistence.rounds import review as review_store
from robert_agent.resource_files import resource
from robert_agent.runtime import cycle as run_once
from robert_agent.work.application import remediation_requests, source_lifecycle
from robert_agent.workflows import routing as workflow_routing
from robert_agent.workflows.review import coordinator as review_coordinator
from tests.support import database as fixture_database
from tests.support.review_coordinator import ReviewCoordinatorFixture


class ReviewTriggerTests(ReviewCoordinatorFixture):
    def test_later_review_request_keeps_original_owner_in_native_prompts(self):
        self.cycle([self.event()])
        first = self.coordinator()
        first.run()
        self.finish_task(first.task_id)
        config = yaml.safe_load(self.config_path.read_text())
        config["repos"][0]["trusted_actors"].append("another-maintainer")
        self.config_path.write_text(yaml.safe_dump(config))
        self.cycle([self.event("second", actor="another-maintainer")])
        second = self.coordinator()
        second.run()
        self.assertEqual(second.manifest["decision_owner"]["login"], "maintainer")
        self.assertEqual(second.request["event"]["actor_login"], "another-maintainer")
        self.assertIn(b'"decision_mention": "@maintainer"', self.calls[-1][2])

    def test_assignment_then_comment_runs_two_binaries_and_publishes_each_revision_once(self):
        binary = self.root / "file_reviewer.py"
        binary.write_text("""import hashlib,json,os,sys
from pathlib import Path
prompt=sys.stdin.buffer.read()
text=prompt.decode()
stage_id=next(line.removeprefix('Stage: ') for line in text.splitlines() if line.startswith('Stage: '))
def required_path(item):
    section=text.split('\\n- '+item+' (required, file, complete):',1)[1].split('\\n- ',1)[0]
    line=next(line for line in section.splitlines() if line.startswith('  Read '))
    return Path(line.removeprefix('  Read ').removesuffix(' before acting.'))
manifest_path=required_path('manifest.json')
manifest=json.loads(manifest_path.read_text())
context=manifest_path.parent
source=Path(manifest['worktree_path'],'source.py').read_text()
assert Path.cwd()==Path(manifest['worktree_path'])
assert source.strip() in (context/'diff.patch').read_text()
output=Path(os.environ['ROBERT_REVIEW_OUTPUT_DIR'])
identity={key:manifest[key] for key in ('round_id','snapshot_id','head_sha')}
def save(name,value):
    path=output/(name+'.tmp')
    path.write_text(json.dumps(value))
    path.replace(output/name)
selected=[]
if stage_id=='review.prepare':
    stage='prepare'
    (output/'context-summary.md').write_text(source)
    save('source-index.json',{'files':[{'path':'source.py','reason':'changed source'}],'reading_notes':source})
elif stage_id=='review.evaluate':
    stage='review'
    index=json.loads((context/'source-index.json').read_text())
    assert index['preparation_mode']=='controller-inventory'
    assert 'source.py' in [entry['path'] for entry in index['files']]
    assert 'not read' in index['reading_notes']
    save('result.json',dict(identity,schema_version=1,findings=[],dismissed_findings=[],
        coverage={'reviewed_paths':['source.py'],'unreviewed_paths':[],'notes':source},merge_recommendation='ready to merge'))
else:
    assert stage_id=='review.synthesize'
    stage='synthesis'
    inputs=required_path('selected-reviewer-results')
    records=json.loads(inputs.read_text())['records']
    assert len(records)==2
    for record in records:
        result=json.loads(Path(record['result_path']).read_text())
        assert result['coverage']['notes']==source and result['head_sha']==identity['head_sha']
        assert Path(record['report_path']).read_text()
        selected.append(record['result_path'])
    save('evaluation.json',dict(identity,schema_version=1,items=[],merge_recommendation='ready to merge',merge_reason='无问题'))
save('observed.json',dict(identity,stage=stage,source=source,prompt_sha256=hashlib.sha256(prompt).hexdigest(),selected=selected))
""")
        config = yaml.safe_load(self.config_path.read_text())
        for worker in config["workers"].values():
            worker["command"] = shlex.join([sys.executable, str(binary)])
        self.config_path.write_text(yaml.safe_dump(config))
        first_event = self.event() | {
            "event_type": "assigned",
            "actor_login": "robert-bot",
            "assignment_actor_login": "maintainer",
            "assigned_to": "robert-bot",
            "body": "",
            "intent": "unclear",
        }
        remote_reviews, rounds = [], []

        def api(command, **kwargs):
            if "--paginate" in command:
                value = [remote_reviews]
            elif "--method" not in command:
                value = self.pr("", 42)
            else:
                value = {
                    "id": 100 + len(remote_reviews),
                    "state": "COMMENTED",
                    "commit_id": next(arg[10:] for arg in command if arg.startswith("commit_id=")),
                    "body": next(arg[5:] for arg in command if arg.startswith("body=")),
                }
                value["html_url"] = f"https://github.com/example/backend/pull/42#pullrequestreview-{value['id']}"
                remote_reviews.append(value)
            return SimpleNamespace(returncode=0, stdout=json.dumps(value), stderr="")

        for event, value in ((first_event, 2), (self.event("second"), 3)):
            if value == 3:
                self.git_fixture.commit("value = 3\n")
            self.cycle([event, event])
            task, attempt = self.current()
            driver = review_coordinator.Coordinator(self.db, task, attempt, fetch_pr=self.pr)
            report = driver.run()
            rounds.append((driver, report.read_bytes()))
            self.cycle([])
            result = publish.publish_ready_actions(self.db, run_command=api)
            self.assertEqual(result["published_count"], 1, result)
            self.assertEqual(publish.publish_ready_actions(self.db, run_command=api)["published_count"], 0)
            self.cycle([], skip_publish=False)
            with fixture_database.connect(self.db) as conn:
                session = review_store.session(conn, task_id=task)
                self.assertEqual(session["status"], "published")
                runs = review_store.runs(conn, driver.round_id)
            observed = [json.loads(Path(run["output_dir"], "observed.json").read_text()) for run in runs]
            reviews = [item for item in observed if item["stage"] == "review"]
            self.assertEqual(len(reviews), 2)
            self.assertEqual(len({item["prompt_sha256"] for item in reviews}), 1)
            self.assertEqual({item["source"] for item in observed}, {f"value = {value}\n"})
            self.assertTrue(all(driver.round_id in path for item in observed for path in item["selected"]))
        self.cycle([first_event, self.event("second")])
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_sessions").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 2)
        self.assertEqual(len(remote_reviews), 2)
        self.assertEqual(
            [item["commit_id"] for item in remote_reviews], [driver.manifest["head_sha"] for driver, _ in rounds]
        )
        self.assertNotEqual(rounds[0][0].manifest["head_sha"], rounds[1][0].manifest["head_sha"])
        self.assertNotEqual(rounds[0][0].manifest["worktree_path"], rounds[1][0].manifest["worktree_path"])
        with fixture_database.connect(self.db) as conn:
            report = review_store.session(conn, round_id=rounds[0][0].round_id)["report_path"]
        self.assertEqual(Path(report).read_bytes(), rounds[0][1])

    def test_non_allowlisted_pr_author_keeps_legacy_worker(self):
        result = self.cycle([self.event(author="other")])
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_sessions").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT expected_output FROM tasks").fetchone()[0], "pr_review_comment")
        self.assertEqual(result["dispatch_count"], 1)

    def test_new_same_sha_trigger_is_new_round_and_untrusted_trigger_is_not(self):
        self.cycle([self.event(), self.event("untrusted", actor="stranger")])
        self.cycle([self.event(), self.event("second")])
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_sessions").fetchone()[0], 2)

    def test_enabling_native_review_does_not_replay_consumed_legacy_triggers(self):
        for event_type in ("comment", "review_request"):
            with self.subTest(event_type=event_type):
                fixture = ReviewCoordinatorFixture()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                config = yaml.safe_load(fixture.config_path.read_text())
                config["routes"]["review-pr"]["multi_agent_review"]["enabled"] = False
                fixture.config_path.write_text(yaml.safe_dump(config))
                event = fixture.event("legacy-" + event_type)
                if event_type == "review_request":
                    event.update(
                        event_type="review_request",
                        body="",
                        requested_reviewer="robert-bot",
                        requester_login="maintainer",
                    )
                fixture.cycle([event])
                task, _ = fixture.current()
                fixture.finish_task(task)
                with fixture_database.connect(fixture.db) as conn:
                    conn.execute("UPDATE task_events SET relationship='consumed' WHERE task_id=?", (task,))
                    self.assertEqual(
                        conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=?", (task,)).fetchone()[0], 1
                    )
                    before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
                config["routes"]["review-pr"]["multi_agent_review"]["enabled"] = True
                fixture.config_path.write_text(yaml.safe_dump(config))

                fixture.cycle([event, event])

                with fixture_database.connect(fixture.db) as conn:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_sessions").fetchone()[0], 0)
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], before)

    def test_queued_legacy_replay_is_canceled_without_creating_a_task(self):
        config = yaml.safe_load(self.config_path.read_text())
        config["routes"]["review-pr"]["multi_agent_review"]["enabled"] = False
        self.config_path.write_text(yaml.safe_dump(config))
        self.cycle([self.event()])
        task, _ = self.current()
        self.finish_task(task)
        config["routes"]["review-pr"]["multi_agent_review"]["enabled"] = True
        self.config_path.write_text(yaml.safe_dump(config))
        runtime = validate_config.validate_config(self.config_path, skip_external=True)
        repo = runtime["repos"][0]
        with fixture_database.connect(self.db) as conn:
            event = json.loads(conn.execute("SELECT payload_json FROM github_events LIMIT 1").fetchone()[0])
            route = workflow_routing.effective_route(runtime, repo, event)
            queued = review_store.enqueue(conn, repo, event, route, route["native_review_workers"])

        self.cycle([])

        with fixture_database.connect(self.db) as conn:
            entry = review_store.session(conn, round_id=queued["round_id"])
            self.assertEqual(entry["status"], "canceled")
            self.assertEqual(entry["error"], "trigger_already_processed")
            self.assertIsNone(entry["task_id"])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)
        self.cycle([self.event("fresh")])
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 2)

    def test_known_pr_review_request_uses_requester_trust_and_native_author_gate(self):
        self.cycle([self.event()])
        request = self.event("requested") | {
            "event_type": "review_request",
            "body": "",
            "requested_reviewer": "robert-bot",
            "requester_login": "maintainer",
        }
        self.cycle(
            [
                request,
                request,
                request | {"id": "untrusted", "actor_login": "stranger", "requester_login": "stranger"},
                request | {"id": "wrong-target", "requested_reviewer": "someone-else"},
            ]
        )
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_sessions").fetchone()[0], 2)
        config = yaml.safe_load(self.config_path.read_text())
        config["routes"]["review-pr"]["multi_agent_review"]["enabled"] = False
        self.config_path.write_text(yaml.safe_dump(config))
        self.cycle([request | {"id": "disabled"}])
        config["routes"]["review-pr"]["multi_agent_review"]["enabled"] = True
        self.config_path.write_text(yaml.safe_dump(config))
        self.cycle([request | {"id": "unmatched", "pr_author_login": "other"}])
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_sessions").fetchone()[0], 2)

    def test_live_review_request_follows_old_assignment_and_uses_requester(self):
        config = yaml.safe_load(self.config_path.read_text())
        config["routes"]["review-pr"]["multi_agent_review"]["author_allowlist"] = ["robert-bot"]
        self.config_path.write_text(yaml.safe_dump(config))
        assigned = self.event("10", author="robert-bot") | {
            "event_type": "assigned",
            "actor_login": "robert-bot",
            "assignment_actor_login": "maintainer",
            "assigned_to": "robert-bot",
            "body": "",
            "intent": "unclear",
        }
        self.cycle([assigned])
        timeline = [
            {
                "id": 10,
                "event": "assigned",
                "actor": {"login": "maintainer"},
                "assignee": {"login": "robert-bot"},
                "created_at": "2026-10-25T00:00:00Z",
            },
            {
                "id": 11,
                "event": "review_requested",
                "actor": {"login": "maintainer"},
                "requested_reviewer": {"login": "robert-bot"},
                "created_at": "2026-10-25T01:00:00Z",
            },
        ]

        def api(command, **kwargs):
            if command[:3] == ["gh", "search", "issues"]:
                value = []
            elif command[:3] == ["gh", "api", "notifications"]:
                value = [
                    [
                        {
                            "id": "notify-42",
                            "reason": "review_requested",
                            "updated_at": "2026-10-25T01:00:00Z",
                            "repository": {"full_name": "example/backend"},
                            "subject": {
                                "type": "PullRequest",
                                "url": "https://api.github.com/repos/example/backend/pulls/42",
                            },
                        }
                    ]
                ]
            elif command[2] in {"repos/example/backend/pulls/42", "repos/example/backend/issues/42"}:
                value = self.pr("", 42) | {
                    "user": {"login": "robert-bot"},
                    "title": "PR",
                    "body": "",
                    "html_url": "https://github.com/example/backend/pull/42",
                }
            elif command[2] == "repos/example/backend/issues/42/timeline":
                value = timeline
            else:
                raise AssertionError(command)
            return SimpleNamespace(returncode=0, stdout=json.dumps(value), stderr="")

        def poll():
            with (
                patch.object(source_lifecycle, "reconcile_remote_source_states", return_value=0),
                patch.object(remediation_requests, "collect_pr_remediation_decisions", return_value=[]),
            ):
                result = run_once.run_once(
                    self.config_path,
                    workflow_path=resource("workflow.yml"),
                    dry_run=True,
                    skip_publish=True,
                    discovery_runner=api,
                )
            self.assertTrue(result["ok"], result)

        config["routes"]["review-pr"]["multi_agent_review"]["enabled"] = False
        self.config_path.write_text(yaml.safe_dump(config))
        poll()
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_sessions").fetchone()[0], 1)
        config["routes"]["review-pr"]["multi_agent_review"]["enabled"] = True
        self.config_path.write_text(yaml.safe_dump(config))
        poll()
        poll()
        with fixture_database.connect(self.db) as conn:
            sessions = conn.execute("SELECT trigger_fingerprint FROM review_sessions").fetchall()
            self.assertEqual({row[0] for row in sessions}, {"assigned:10", "review_request:11"})
        timeline.append(
            {
                "id": 12,
                "event": "review_requested",
                "actor": {"login": "stranger"},
                "requested_reviewer": {"login": "robert-bot"},
                "created_at": "2026-10-25T02:00:00Z",
            }
        )
        poll()
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_sessions").fetchone()[0], 2)

    def test_disabled_or_unmatched_followup_preserves_legacy_route(self):
        config = validate_config.validate_config(self.config_path, skip_external=True)
        event = {**self.event(), "authorization_status": "accepted_context"}
        self.assertEqual(workflow_routing.effective_route(config, config["repos"][0], event)["route_id"], "review-pr")
        for author, enabled in [("other", True), ("author", False)]:
            config["routes"]["review-pr"]["multi_agent_review"]["enabled"] = enabled
            result = workflow_routing.effective_route(config, config["repos"][0], {**event, "pr_author_login": author})
            self.assertEqual(result["route_id"], "review-comment")
            self.assertNotIn("native_review_workers", result)
