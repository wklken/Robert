from contextlib import closing
import json
import sqlite3
from unittest.mock import patch

from robert_agent.adapters.tapd.client import TapdItem
from robert_agent.configuration.validation import validate_config
from robert_agent.persistence.queries import proposal_feedback, proposals as proposal_queries
from robert_agent.readmodels import proposals as proposal_view
from robert_agent.work.application.admission import ordinary_dispatch_allowed
from robert_agent.persistence import database
from robert_agent.work import proposals, proposal_requests, source_policy
from robert_agent.work.application import commands as work_commands, proposal_admission, proposal_commands
from tests.support.board_control_integration import BoardControlIntegrationFixture


class ProposalAdmissionTests(BoardControlIntegrationFixture):
    def setUp(self):
        super().setUp()
        self.config = validate_config(self.config_path, skip_external=True)
        self.config["proposer"].update(
            enabled=True,
            github_repositories=["example/repo"],
            github_assignees=["owner"],
        )
        self.source = proposals.github_source(
            self.config["repos"][0],
            {
                "id": 11,
                "number": 1,
                "html_url": "https://github.com/example/repo/issues/1",
                "title": "Analyze startup",
                "body": "Find the cause",
                "state": "open",
                "user": {"login": "reporter"},
                "assignees": [],
                "created_at": "2026-11-22T01:00:00+00:00",
            },
            ["owner"],
        )
        self.report = {
            "kind": "investigation",
            "summary": "Find startup cost",
            "scope": "Read startup code",
            "exclusions": "No code modification",
            "verification": "Cite measured paths",
            "risks": "No production access",
            "commit": "a" * 40,
            "evidence": [{"path": "src/start.py", "line": 1, "reason": "startup entry"}],
        }
        self.now = "2026-11-22T02:00:00+00:00"
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO proposals (proposal_id,source_identity,repo_id,status,version,revision,source_key,"
                "fingerprint,created_at,updated_at) VALUES ('p-1',?,?,'pending',1,1,?,'fp',?,?)",
                (self.source["identity"], self.repo_id, self.source["source_key"], self.now, self.now),
            )
            conn.execute(
                "INSERT INTO proposal_revisions VALUES ('p-1',1,?,?,?)",
                (json.dumps(self.source), json.dumps(self.report), self.now),
            )
        proposal_commands.decide(
            self.db_path,
            "p-1",
            context=self.context,
            action="approve",
            expected_version=1,
            idempotency_key="decision",
        )

    def run_prepare(self):
        queue = []
        with closing(database.connect(self.db_path)) as conn, conn:
            admitted = proposal_admission.prepare(
                conn,
                config=self.config,
                repo=self.config["repos"][0],
                repo_id=self.repo_id,
                now=self.now,
                dispatch_queue=queue,
                dispatch_budget={"remaining": 1},
                prompt_paths=[],
            )
        return admitted, queue

    def test_approved_proposal_prepares_an_ordinary_analysis_and_survives_replay(self):
        with (
            patch.object(proposal_admission.proposal_sources, "refresh", return_value=(self.source, None)),
            patch.object(proposal_admission, "_evidence_current", return_value=True),
            patch.object(proposal_admission.workspace, "prepare_worktree", return_value=None),
        ):
            admitted, queue = self.run_prepare()
            self.assertEqual(admitted, {self.source["source_key"]})
            self.assertEqual(len(queue), 1)
            self.assertEqual(queue[0]["task_info"]["route_id"], "comment-analysis")
            self.assertEqual(self.run_prepare(), (set(), []))
        with closing(sqlite3.connect(self.db_path)) as conn:
            status, item, stream = conn.execute("SELECT status,work_item_id,workstream_id FROM proposals").fetchone()
            self.assertEqual(status, "admitted")
            self.assertTrue(item)
            self.assertEqual(stream, self.source["source_key"])
            event = json.loads(conn.execute("SELECT payload_json FROM github_events").fetchone()[0])
        with closing(database.connect(self.db_path)) as conn:
            self.assertTrue(proposal_queries.task_bound(conn, queue[0]["task_info"]["task_id"]))
            self.assertFalse(ordinary_dispatch_allowed(conn, queue[0]["task_info"]["task_id"], self.config["repos"][0]))
            self.assertTrue(
                source_policy.reauthorize_saved_request(
                    event,
                    self.config["repos"][0],
                    conn=conn,
                    config=self.config,
                )["drives_execution"]
            )
            changed_stream = {**event, "workstream_id": "github:other/repo#1"}
            self.assertFalse(proposal_requests.reauthorize(conn, self.config, changed_stream)["drives_execution"])
            altered = {**event, "body": event["body"] + " make more changes"}
            self.assertFalse(
                source_policy.reauthorize_saved_request(
                    altered,
                    self.config["repos"][0],
                    conn=conn,
                    config=self.config,
                )["drives_execution"]
            )
            self.config["proposer"]["enabled"] = False
            self.assertFalse(proposal_requests.reauthorize(conn, self.config, event)["drives_execution"])

    def test_investigation_start_does_not_generate_formal_progress_or_claim_the_source(self):
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE proposal_decisions SET action='investigate'")
            report = {**self.report, "analyzed_at": self.now}
            conn.execute("UPDATE proposal_revisions SET report_json=?", (json.dumps(report),))
        with (
            patch.object(proposal_admission.proposal_sources, "refresh", return_value=(self.source, None)),
            patch.object(proposal_admission, "_evidence_current", return_value=True),
            patch.object(proposal_admission.workspace, "prepare_worktree", return_value=None),
        ):
            _, queue = self.run_prepare()
        result = queue[0]["task_info"]
        self.assertEqual(result["route_id"], "comment-analysis")
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE attempts SET metadata_json=?", (json.dumps({"worker_started_at": self.now}),))
        with closing(database.connect(self.db_path)) as conn:
            row = proposal_queries.current(conn, proposal_id="p-1")[0]
            self.assertEqual(
                (row["status"], row["investigation_task_id"], row["task_id"]),
                ("investigating", result["task_id"], None),
            )
            self.assertFalse(proposal_queries.formal_work_exists(conn, self.source["source_key"]))
            self.assertEqual(proposal_feedback.unreported(conn, [self.repo_id], [], scheduled=True), [])
            self.assertEqual(proposal_queries.finished_investigations(conn, self.repo_id), [])

    def test_previous_approval_cannot_authorize_a_new_decision_on_the_same_revision(self):
        with (
            patch.object(proposal_admission.proposal_sources, "refresh", return_value=(self.source, None)),
            patch.object(proposal_admission, "_evidence_current", return_value=True),
            patch.object(proposal_admission.workspace, "prepare_worktree", return_value=None),
        ):
            self.run_prepare()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            event = json.loads(conn.execute("SELECT payload_json FROM github_events").fetchone()[0])
            conn.execute("UPDATE proposals SET status='pending', version=4 WHERE proposal_id='p-1'")
        proposal_commands.decide(
            self.db_path,
            "p-1",
            context=self.context,
            action="approve",
            expected_version=4,
            idempotency_key="new-approval",
        )
        with closing(database.connect(self.db_path)) as conn:
            self.assertFalse(proposal_requests.reauthorize(conn, self.config, event)["drives_execution"])

    def test_changed_source_blocks_before_worktree_or_task_creation(self):
        changed = {**self.source, "owners": ["other"]}
        with (
            patch.object(proposal_admission.proposal_sources, "refresh", return_value=(changed, None)),
            patch.object(proposal_admission.workspace, "prepare_worktree") as prepare_tree,
        ):
            self.assertEqual(self.run_prepare(), (set(), []))
            prepare_tree.assert_not_called()
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT status FROM proposals").fetchone()[0], "blocked")
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 0)

    def test_network_refresh_runs_without_an_open_writer(self):
        def read_source(*_):
            with closing(sqlite3.connect(self.db_path, timeout=0)) as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.rollback()
            return self.source, None

        with (
            patch.object(proposal_admission.proposal_sources, "refresh", side_effect=read_source),
            patch.object(proposal_admission, "_evidence_current", return_value=False),
        ):
            self.assertEqual(self.run_prepare(), (set(), []))

    def test_tapd_approval_uses_source_queue_without_a_mention(self):
        project = {
            "host": "tapd.example.test",
            "workspace_id": "101",
            "trusted_user": "owner",
            "label_rules": [{"label": "startup", "repository": "example/repo"}],
        }
        self.config["tapd"].update(enabled=True, projects=[project], mention_name="robert-test")
        self.config["proposer"].update(tapd_projects=["tapd.example.test/101"], tapd_assignees=["owner"])
        ticket = TapdItem(
            "story",
            "211",
            "Analyze startup",
            "open",
            "startup",
            "Find the cause",
            "12",
            False,
            None,
            "2026-11-21 16:00:00",
            ("owner",),
        )
        source = proposals.tapd_source(project, ticket, {"done": "Done"}, ["owner"])
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE proposals SET source_identity=?, source_key=?", (source["identity"], source["source_key"])
            )
            conn.execute("UPDATE proposal_revisions SET source_json=?", (json.dumps(source),))
        with (
            patch.object(proposal_admission.proposal_sources.tapd_client, "client_for") as tapd,
            patch.object(proposal_admission, "_evidence_current", return_value=True),
            patch.object(proposal_admission.workspace, "prepare_worktree", return_value=None),
        ):
            tapd.return_value.get_item.return_value = ticket
            tapd.return_value.terminal_states.return_value = {"done": "Done"}
            tapd.return_value.ticket_comments.return_value = ()
            admitted, queue = self.run_prepare()
        self.assertEqual(admitted, {source["source_key"]})
        self.assertEqual(queue[0]["task_info"]["route_id"], "comment-analysis")
        with closing(database.connect(self.db_path)) as conn:
            self.assertTrue(
                ordinary_dispatch_allowed(conn, queue[0]["task_info"]["task_id"], self.config["repos"][0], self.config)
            )

    def test_unlabeled_ticket_can_start_in_the_confirmed_repository_and_waits_for_worker_evidence(self):
        project = {
            "host": "tapd.example.test",
            "workspace_id": "101",
            "trusted_user": "owner",
            "label_rules": [{"label": "startup", "repository": "example/repo"}],
        }
        self.config["tapd"].update(enabled=True, projects=[project], mention_name="robert-test")
        self.config["proposer"].update(
            tapd_projects=["tapd.example.test/101"],
            tapd_assignees=["owner"],
            tapd_todo_states={"story": ["open"]},
            schedule={"enabled": True},
            publish_comments=True,
        )
        ticket = TapdItem("story", "211", "Fix startup", "open", "", "Fix the cause", "12", False, None, self.now, ())
        source = proposals.tapd_source(project, ticket, {"done": "Done"}, ["owner"], allow_unbound=True)
        source.update(status="open", labels=[], priority=None)
        report = {
            **self.report,
            "kind": "implementation",
            "repository": "example/repo",
            "feasibility": "ready",
            "analyzed_at": self.now,
        }
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("DELETE FROM proposal_decisions")
            conn.execute(
                "UPDATE proposals SET status='pending',version=1,repo_id=NULL,source_identity=?,source_key=?",
                (source["identity"], source["source_key"]),
            )
            conn.execute(
                "UPDATE proposal_revisions SET source_json=?,report_json=?", (json.dumps(source), json.dumps(report))
            )
        proposal_commands.decide(
            self.db_path,
            "p-1",
            context=self.context,
            action="approve",
            repository_label="startup",
            expected_version=1,
            idempotency_key="confirm-repo",
        )
        with (
            patch.object(proposal_admission.proposal_sources.tapd_client, "client_for") as tapd,
            patch.object(proposal_admission, "_evidence_current", return_value=True),
            patch.object(proposal_admission.workspace, "prepare_worktree", return_value=None),
        ):
            tapd.return_value.get_item.return_value = ticket
            tapd.return_value.terminal_states.return_value = {"done": "Done"}
            tapd.return_value.ticket_comments.return_value = ()
            _, queue = self.run_prepare()
        self.assertEqual(len(queue), 1)
        with closing(database.connect(self.db_path)) as conn:
            self.assertCountEqual(
                [row["notification_type"] for row in proposal_feedback.status(conn, "p-1")],
                ["admitted", "tag_sync"],
            )
            self.assertEqual(
                proposal_feedback.unreported(conn, [self.repo_id], ["tapd.example.test/101"], scheduled=True), []
            )
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE attempts SET metadata_json=?", (json.dumps({"worker_started_at": self.now}),))
        with closing(database.connect(self.db_path)) as conn:
            rows = proposal_feedback.unreported(conn, [self.repo_id], ["tapd.example.test/101"], scheduled=True)
            self.assertEqual([row["event_type"] for row in rows], ["started"])

    def test_progress_comments_only_use_proposal_task_and_linked_pr(self):
        with (
            patch.object(proposal_admission.proposal_sources, "refresh", return_value=(self.source, None)),
            patch.object(proposal_admission, "_evidence_current", return_value=True),
            patch.object(proposal_admission.workspace, "prepare_worktree", return_value=None),
        ):
            _, queue = self.run_prepare()
        root_task = queue[0]["task_info"]["task_id"]
        later = "2026-11-22T03:00:00+00:00"
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            item = conn.execute("SELECT work_item_id FROM proposals").fetchone()[0]
            conn.execute("UPDATE tasks SET lifecycle='completed' WHERE task_id=?", (root_task,))
            conn.execute(
                "INSERT INTO tasks (task_id,workstream_id,lifecycle,created_at,updated_at) VALUES ('unrelated',?,'completed',?,?)",
                (self.source["source_key"], later, later),
            )
            conn.execute(
                "INSERT INTO github_actions (action_id,task_id,action_type,target_url,audit_status,publish_status,created_at)"
                " VALUES ('pr-action',?,'open_pr','https://github.com/example/repo/pull/41','accepted','published',?)",
                (root_task, later),
            )
        with closing(database.connect(self.db_path)) as conn, conn:
            for event, key, metadata in (
                ("completed", "root-result", {"task_id": root_task}),
                ("completed", "unrelated-result", {"task_id": "unrelated"}),
                ("pr_merged", "root-pr", {"number": 41}),
                ("pr_merged", "unrelated-pr", {"number": 42}),
            ):
                work_commands.record_system_event(
                    conn, item, event_type=event, idempotency_key=key, metadata=metadata, now=later
                )
            rows = proposal_feedback.unreported(conn, [self.repo_id], [])
            self.assertCountEqual(
                [row["event_type"] for row in rows],
                ["admitted", "completed", "pr_opened", "pr_merged"],
            )
            self.assertEqual(proposal_queries.work_progress(conn, root_task)["task_id"], root_task)
            self.assertTrue(proposal_feedback.pull_request(conn, root_task)["url"].endswith("/41"))

        detail = proposal_view.read(self.db_path, proposal_id="p-1")[0]
        self.assertEqual(detail["work"]["state"], "in_review")
        self.assertFalse(detail["work"]["terminal"])
