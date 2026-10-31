import json
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

from tests import test_review_input_workflow as review_fixtures
from robert_agent import publish, run_once, validate_config, execution_store
from tests import test_terminal_github_followup as terminal_fixtures
from robert_agent.worker import result

from tests import test_pr_remediation_integration as remediation_fixtures
from robert_agent import route, workstream, execution_operations
import unittest


class PublicationLifecycleFollowupTests(unittest.TestCase):
    def test_superseded_update_finishes_intent_and_allows_new_execution(self):
        f = review_fixtures.ReviewInputIntegrationTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        db, task_id, attempt_id, task_metadata, _ = f.prepare("comment")
        tree = Path(f.tmp.name) / "lc1-assigned-worktree"
        tree.mkdir()

        def git(*args):
            return subprocess.run(
                ["git", *args], cwd=tree, text=True, capture_output=True, check=True
            ).stdout.strip()

        git("init", "-b", "main")
        git("config", "user.email", "probe@example.invalid")
        git("config", "user.name", "Lifecycle probe")
        (tree / "source.py").write_text("value = 1\n")
        git("add", "source.py")
        git("commit", "-m", "base fixture")
        base = git("rev-parse", "HEAD")
        (tree / "source.py").write_text("value = 2\n")
        git("commit", "-am", "audited fixture change")
        audited_head = git("rev-parse", "HEAD")
        git("checkout", "-b", "remote-divergent", base)
        (tree / "source.py").write_text("value = 3\n")
        git("commit", "-am", "divergent remote fixture")
        remote_head = git("rev-parse", "HEAD")
        git("checkout", "main")
        git("remote", "add", "origin", "https://github.com/example/backend.git")
        # This check actually executes; the verification entry below reports it.
        git("diff", "--check")
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute(
                "UPDATE attempts SET worktree_path=?,branch_name='main',"
                "metadata_json=json_set(metadata_json,'$.existing_pr_head_sha',?) "
                "WHERE attempt_id=?",
                (str(tree), base, attempt_id),
            )
            required_skills = json.loads(
                conn.execute(
                    "SELECT required_skills_json FROM route_decisions WHERE task_id=?",
                    (task_id,),
                ).fetchone()[0]
            )
            repo_id, workstream_id = conn.execute(
                "SELECT w.repo_id,w.workstream_id FROM tasks t "
                "JOIN workstreams w ON w.workstream_id=t.workstream_id WHERE t.task_id=?",
                (task_id,),
            ).fetchone()
        envelope = result.build_result(
            task_id,
            attempt_id,
            "update_existing_pr",
            ["comment:request-1"],
            [
                {
                    "command": "git diff --check",
                    "status": "passed",
                    "purpose": "Check the fixture change for whitespace errors",
                    "required": True,
                    "exit_code": 0,
                    "source": "worker_reported",
                }
            ],
            "Applied the requested fixture update.",
            planned_github_actions=[
                {"type": "push_existing_pr"},
                {
                    "type": "comment",
                    "target_url": "https://github.com/example/backend/pull/456",
                    "body": "Applied the requested update; git diff --check passed.",
                },
            ],
            used_skills=required_skills,
            review_input_kind="request",
        )
        receipt = result.record_result(db, envelope, notifier=lambda _: None)
        assert receipt["ok"], receipt
        config = validate_config.validate_config(f.config_path, skip_external=True)
        assert config["ok"], config
        repo = next(r for r in config["repos"] if r["full_name"] == "example/backend")
        with closing(sqlite3.connect(db)) as conn, conn:
            audited = run_once._audit_completed_results(
                conn,
                repo_id,
                f.data_dir,
                db,
                repo,
                "lc1-evidence-run",
                run_once._now(),
                True,
                [],
                None,
                config_result=config,
            )
            saved = json.loads(
                conn.execute(
                    "SELECT metadata_json FROM worker_results WHERE result_id=?",
                    (receipt["result_id"],),
                ).fetchone()[0]
            )
            before = conn.execute(
                "SELECT action_type,audit_status,publish_status FROM github_actions "
                "WHERE result_id=? ORDER BY created_at,action_id",
                (receipt["result_id"],),
            ).fetchall()
            output_type = conn.execute(
                "SELECT output_type FROM worker_results WHERE result_id=?",
                (receipt["result_id"],),
            ).fetchone()[0]
        assert audited == 1 and saved["audit"]["status"] == "accepted", saved
        assert output_type == "update_existing_pr", output_type
        assert saved["submission_head_sha"] == audited_head
        assert saved["audit"]["action_scope"]["result_head_sha"] == audited_head
        assert before == [
            ("push_existing_pr", "accepted", "not_published"),
            ("comment", "accepted", "not_published"),
        ], before
        calls = []

        def transport(command, **kwargs):
            calls.append(command)
            if command[:2] == ["git", "ls-remote"]:
                return subprocess.CompletedProcess(
                    command, 0, remote_head + " refs/heads/main\n", ""
                )
            if command[:2] == ["git", "fetch"]:
                # The actual divergent commit already exists in this temp repository.
                return subprocess.CompletedProcess(command, 0, "", "")
            if command[:3] == ["git", "merge-base", "--is-ancestor"]:
                checked = subprocess.run(command, **kwargs)
                assert checked.returncode == 1, checked
                return checked
            raise AssertionError("Unexpected external operation: " + repr(command))

        publication = publish.publish_ready_actions(db, run_command=transport)
        with closing(sqlite3.connect(db)) as conn, conn:
            run_once._finalize_published_results(
                conn,
                repo_id,
                f.data_dir,
                db,
                repo,
                "lc1-evidence-run",
                run_once._now(),
                True,
                [],
                None,
                config_result=config,
            )
            after = conn.execute(
                "SELECT action_type,audit_status,publish_status,"
                "json_extract(metadata_json,'$.publish.safe_error') "
                "FROM github_actions WHERE result_id=? ORDER BY created_at,action_id",
                (receipt["result_id"],),
            ).fetchall()
            task_state = conn.execute(
                "SELECT lifecycle FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()[0]
            attempt_state = conn.execute(
                "SELECT status FROM attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()[0]
            intents = conn.execute(
                "SELECT ga.action_type,pi.status FROM publication_intents pi "
                "JOIN github_actions ga ON ga.action_id=pi.action_id "
                "WHERE ga.result_id=?",
                (receipt["result_id"],),
            ).fetchall()
        second = publish.publish_ready_actions(db, run_command=transport)
        assert (
            publication["skipped_count"] == 2 and publication["published_count"] == 0
        ), publication
        assert [row[:3] for row in after] == [
            ("push_existing_pr", "accepted", "skipped"),
            ("comment", "accepted", "skipped"),
        ], after
        assert after[1][3] == "skipped because the preceding push was superseded", after
        assert task_state == "failed" and attempt_state == "completed"
        assert intents == [("push_existing_pr", "confirmed_absent")], intents
        assert second["pending_count"] == 0

    def test_remediation_question_publishes_before_episode_stops(self):
        self.remediation_question_reply("github")

    def test_board_reply_to_remediation_question_keeps_source_and_human_ownership(self):
        self.remediation_question_reply("board")

    def remediation_question_reply(self, reply_mode):
        for kind in ("merge_conflict_remediation", "ci_remediation"):
            f = remediation_fixtures.PrRemediationIntegrationTests()
            f.setUp()
            try:
                now = run_once._now()
                with closing(sqlite3.connect(f.db_path)) as c, c:
                    for old_id, source_key in c.execute(
                        "SELECT source_id,source_key FROM github_sources"
                    ).fetchall():
                        canonical = run_once._source_id(source_key)
                        c.execute(
                            "UPDATE github_sources SET source_id=? WHERE source_id=?",
                            (canonical, old_id),
                        )
                        c.execute(
                            "UPDATE workstreams SET primary_source_id=? WHERE primary_source_id=?",
                            (canonical, old_id),
                        )
                    choices = run_once._collect_pr_remediation_decisions(
                        c, "repo:example/backend", f.repo, f.runner(), now
                    )
                    event = next(e for e in choices if e["task_kind"] == kind)
                    routed = route.route_task(event)
                    # Use the supported dry-run worktree preparation; no real Git worker.
                    tree = run_once._prepare_worktree(f.repo, event, routed, True)
                    task = run_once._create_task_attempt_and_prompt(
                        c,
                        Path(f.tmp.name) / "data",
                        f.db_path,
                        f.repo,
                        "repo:example/backend",
                        event,
                        routed,
                        workstream.plan_event(event, active_workstreams=set()),
                        now,
                        worktree_result=tree,
                    )
                    c.execute(
                        "UPDATE attempts SET worktree_path=NULL,branch_name=NULL WHERE attempt_id=?",
                        (task["attempt_id"],),
                    )
                envelope = result.build_result(
                    task["task_id"],
                    task["attempt_id"],
                    "waiting_for_user",
                    [event["event_fingerprint"]],
                    [],
                    "Need maintainer decision",
                    [
                        {
                            "type": "comment",
                            "body": "Which behavior should be preserved?",
                            "target_url": "https://github.com/example/backend/pull/13",
                        }
                    ],
                    used_skills=routed.get("required_skills", []),
                    operator_question={
                        "kind": "clarification",
                        "summary": "Which behavior should be preserved?",
                    },
                )
                recorded = result.record_result(
                    f.db_path, envelope, notifier=lambda _: None
                )
                assert recorded["ok"], recorded
                with closing(sqlite3.connect(f.db_path)) as c, c:
                    audited = run_once._audit_completed_results(
                        c,
                        "repo:example/backend",
                        Path(f.tmp.name) / "data",
                        f.db_path,
                        f.repo,
                        "no-run",
                        now,
                        True,
                        [],
                        None,
                    )
                    scope = json.loads(
                        c.execute(
                            "SELECT metadata_json FROM worker_results"
                        ).fetchone()[0]
                    )["audit"]["action_scope"]
                    state = {
                        "task": c.execute("SELECT lifecycle FROM tasks").fetchone()[0],
                        "audit": c.execute(
                            "SELECT audit_status FROM github_actions"
                        ).fetchone()[0],
                        "episode": c.execute(
                            "SELECT status FROM pr_remediation_episodes WHERE episode_id=?",
                            (scope["episode_id"],),
                        ).fetchone()[0],
                        "scope_current": execution_operations.publication_scope_is_current(
                            c, task["task_id"], scope
                        ),
                    }
                calls = []

                def blocked(cmd, **kw):
                    calls.append(cmd)
                    payload = (
                        []
                        if not (
                            "--method" in cmd
                            or "-X" in cmd
                            or "-f" in cmd
                            or "--input" in cmd
                        )
                        else {
                            "id": 123,
                            "html_url": "https://github.com/example/backend/pull/13#issuecomment-123",
                        }
                    )
                    return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")

                pub = publish.publish_ready_actions(f.db_path, run_command=blocked)
                with closing(sqlite3.connect(f.db_path)) as c:
                    final = {
                        "task": c.execute("SELECT lifecycle FROM tasks").fetchone()[0],
                        "action": c.execute(
                            "SELECT publish_status FROM github_actions"
                        ).fetchone()[0],
                        "intent": c.execute(
                            "SELECT status FROM publication_intents"
                        ).fetchone()[0],
                        "binding": c.execute(
                            "SELECT binding_status FROM execution_subjects WHERE subject_id=?",
                            (scope["episode_id"],),
                        ).fetchone()[0],
                    }
                assert (
                    audited == 1
                    and state["audit"] == "accepted"
                    and state["scope_current"]
                ), state
                assert pub["ok"] and calls and final["action"] == "published", pub
                with closing(sqlite3.connect(f.db_path)) as c, c:
                    run_once._finalize_published_results(
                        c,
                        "repo:example/backend",
                        Path(f.tmp.name) / "data",
                        f.db_path,
                        f.repo,
                        "no-run",
                        now,
                        True,
                        [],
                        None,
                    )
                    assert (
                        c.execute("SELECT lifecycle FROM tasks").fetchone()[0]
                        == "waiting_for_user"
                    )
                    assert (
                        c.execute(
                            "SELECT count(*) FROM work_item_events WHERE event_type='operator_question'"
                        ).fetchone()[0]
                        == 1
                    )
                    run_once._close_subject_binding(
                        c, "remediation_episode", scope["episode_id"], now
                    )
                    parent_id = task["task_id"]
                # Exercise the supported pipeline after the Episode owner has closed.
                from tests import test_board_control_integration as board_fixture

                config_fixture = board_fixture.BoardControlIntegrationTests()
                config_fixture.setUp()
                self.addCleanup(config_fixture.doCleanups)
                cfg = json.loads(config_fixture.config_path.read_text())
                cfg.update(data_dir=str(f.db_path.parent), database=f.db_path.name)
                cfg["repos"][0].update(
                    full_name="example/backend",
                    github_account="robert-bot",
                    trusted_actors=["wklken"],
                )
                config_fixture.config_path.write_text(json.dumps(cfg))
                reply_file = config_fixture.root / "reply.json"
                reply_file.write_text(
                    json.dumps(
                        {
                            "events": [
                                {
                                    "id": "human-reply",
                                    "number": 13,
                                    "source_type": "pull_request",
                                    "event_type": "comment",
                                    "actor_login": "wklken",
                                    "body": "@robert-bot analyze the alternative",
                                    "state": "open",
                                }
                            ]
                        }
                    )
                )
                if reply_mode == "board":
                    from robert_agent import board, work_items, github_scan

                    with closing(sqlite3.connect(f.db_path)) as conn:
                        item_id = conn.execute(
                            "SELECT work_item_id FROM work_items"
                        ).fetchone()[0]
                    card = board.get_work_item_detail(f.db_path, item_id)
                    context = work_items.CommandContext(
                        "operator",
                        "maintainer",
                        frozenset({"repo:example/backend"}),
                        frozenset({"default", "reviewer"}),
                    )
                    requested = work_items.execute_command(
                        f.db_path,
                        item_id,
                        context=context,
                        command="reply",
                        expected_version=card["version"],
                        idempotency_key="reply-remediation",
                        body="Analyze the alternative.",
                    )
                    self.assertEqual(requested["task_id"] is not None, True)
                    with closing(sqlite3.connect(f.db_path)) as conn, conn:
                        import time

                        github_scan._save_reads(
                            conn,
                            "repo:example/backend",
                            {
                                github_scan.command_key(
                                    ["gh", "api", "repos/example/backend/issues/13"]
                                ): json.dumps(
                                    {
                                        "state": "open",
                                        "body": "Repair PR",
                                        "title": "Repair PR",
                                    }
                                )
                            },
                            time.time() + 1,
                        )
                    reply_file.write_text('{"events": []}')
                cycle = run_once.run_once(
                    config_fixture.config_path,
                    fixture_path=reply_file,
                    dry_run=True,
                    skip_external=True,
                    skip_publish=True,
                )
                self.assertTrue(cycle["ok"], cycle)
                with closing(sqlite3.connect(f.db_path)) as conn:
                    child = conn.execute(
                        "SELECT task_kind FROM tasks WHERE parent_task_id=?",
                        (parent_id,),
                    ).fetchone()
                    self.assertEqual(child, ("human_request",))

            finally:
                f.doCleanups()

    def test_remote_close_retires_unstarted_publication_and_reopen_allows_request(self):
        for state_reason in ("completed", "not_planned"):
            with self.subTest(state_reason=state_reason):
                self.remote_close_reopen(state_reason)

    def remote_close_reopen(self, state_reason):
        f = terminal_fixtures.TerminalGithubFollowupTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.deliver("first")
        with closing(sqlite3.connect(f.db_path)) as conn:
            task, attempt = conn.execute(
                "SELECT task_id,attempt_id FROM attempts"
            ).fetchone()
        payload = {
            "task_id": task,
            "attempt_id": attempt,
            "output_type": "comment_analysis",
            "submission": {
                "planned_github_actions": [
                    {"type": "comment", "body": "Analysis finished"}
                ],
                "consumed_event_fingerprints": ["comment:first"],
                "verification": [],
                "handoff": "done",
                "used_skills": [],
            },
        }
        self.assertTrue(result.record_result(f.db_path, payload)["ok"])
        f.deliver("first")
        with closing(sqlite3.connect(f.db_path)) as conn, conn:

            def remote(cmd, **kwargs):
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    json.dumps(
                        {
                            "state": "closed",
                            "state_reason": state_reason,
                            "closed_at": run_once._now(),
                        }
                    ),
                    "",
                )

            run_once._reconcile_remote_source_states(
                conn,
                f.fixture.repo_id,
                {"full_name": "example/repo"},
                remote,
                run_once._now(),
            )
            self.assertEqual(
                conn.execute("SELECT publish_status FROM github_actions").fetchone()[0],
                "skipped",
            )
            self.assertEqual(
                execution_store.get_binding(conn, "task", task)["binding_status"],
                "terminal",
            )
        publication = publish.publish_ready_actions(
            f.db_path,
            run_command=lambda *a, **k: self.fail("closed action must not publish"),
        )
        self.assertEqual(publication["pending_count"], 0)
        f.deliver("new-after-reopen")
        with closing(sqlite3.connect(f.db_path)) as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 2
            )

    def test_remote_close_preserves_unknown_effect_until_read_only_reconciliation(self):
        from tests import test_publication_reconcile as fixtures

        for present in (False, True):
            with self.subTest(present=present):
                fixture = fixtures.PublicationReconcileTests()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)

                def lost_response(command, **kwargs):
                    if command[:3] == [
                        "gh",
                        "api",
                        "repos/x/y/issues/1/comments?per_page=100",
                    ]:
                        return fixtures.Completed("[]")
                    return fixtures.Completed("", 1, "connection reset after response")

                first = publish.publish_ready_actions(
                    fixture.db_path, run_command=lost_response
                )
                self.assertEqual(first["failed_count"], 1)
                with closing(sqlite3.connect(fixture.db_path)) as conn, conn:
                    conn.execute("UPDATE tasks SET lifecycle='canceled'")
                    conn.execute(
                        "UPDATE github_actions SET metadata_json=json_remove(metadata_json,'$.publish.next_retry_at')"
                    )
                    publish.retire_unstarted_actions(conn, "task-1", run_once._now())
                    self.assertEqual(
                        conn.execute(
                            "SELECT publish_status FROM github_actions"
                        ).fetchone()[0],
                        "not_published",
                    )
                    self.assertEqual(
                        conn.execute(
                            "SELECT status FROM publication_intents"
                        ).fetchone()[0],
                        "executing",
                    )
                calls = []

                def reconcile(command, **kwargs):
                    calls.append(command)
                    self.assertNotIn("--input", command)
                    self.assertNotIn("-f", command)
                    comments = (
                        [
                            {
                                "id": 99,
                                "body": fixture.body,
                                "html_url": "https://github.com/x/y/issues/1#issuecomment-99",
                            }
                        ]
                        if present
                        else []
                    )
                    return fixtures.Completed(json.dumps([comments]))

                recovered = publish.publish_ready_actions(
                    fixture.db_path, run_command=reconcile
                )
                self.assertTrue(recovered["ok"], recovered)
                with closing(sqlite3.connect(fixture.db_path)) as conn:
                    self.assertEqual(
                        conn.execute(
                            "SELECT publish_status FROM github_actions"
                        ).fetchone()[0],
                        "published" if present else "skipped",
                    )
                    self.assertEqual(
                        conn.execute(
                            "SELECT status FROM publication_intents"
                        ).fetchone()[0],
                        "published" if present else "confirmed_absent",
                    )
                self.assertTrue(calls)
