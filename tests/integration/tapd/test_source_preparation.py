"""Exercise a native child from real intake and admission through its Worker."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from robert_agent.persistence import database, transactions
from robert_agent.persistence.schema.database import init_database
from robert_agent.persistence.stores import source_sessions
from robert_agent.work import tapd_authority as facts
from robert_agent.work.application import source_preparation, tapd_intake


class SourcePreparationTests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        self.db = self.root / "state.sqlite"
        init_database(self.db)
        self.ticket = facts.TicketIdentity("tapd.example", "001", "bug", "90000000000000000009")
        self.repo = {
            "full_name": "org/alpha",
            "github_account": "bot",
            "default_base_branch": "main",
            "repo_root": str(self.root / "checkout"),
            "worktree_root": str(self.root / "trees"),
        }
        Path(self.repo["repo_root"]).mkdir()
        project = {
            "workspace_id": "001",
            "trusted_author": "test-user",
            "repositories": ["org/alpha"],
            "mentions": {"enabled": True, "activated_after": "2026-11-01T00:00:00+00:00"},
            "rules": [],
        }
        self.config = {
            "tapd": {
                "enabled": True,
                "host": self.ticket.host,
                "api_timezone": "UTC",
                "bot_name": "helper-team",
                "reply_name": "service-desk",
                "projects": [project],
            },
            "repos": [self.repo],
            "workers": {
                "worker": {
                    "name": "worker",
                    "command": "mock",
                    "agent": "command",
                    "default_model": "m",
                    "default_effort": "normal",
                }
            },
            "default_worker": "worker",
        }
        self.now = datetime.now(timezone.utc)
        self.item = facts.TicketFacts(
            self.ticket, "Bug report", "Complete ticket content", "open", (), (), False, ("closed",)
        )
        self.publication = facts.PublicationFacts(self.ticket)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) VALUES('a',?,?,?,?,?)",
                tuple(self.repo.values()),
            )

    def comment(self, identifier, *, suffix="", mention=True):
        body = ("@helper-team " if mention else "") + "Please inspect " + identifier + suffix
        created = (self.now - timedelta(minutes=15 - int(identifier))).strftime("%Y-%m-%d %H:%M:%S")
        version = facts.CommentVersion(self.ticket, "bug", identifier, created, None, sha256(body.encode()).hexdigest())
        return facts.CommentObservation(
            version,
            body,
            "test-user",
            facts.CreatorEvidence(version, "test-user", "creator"),
            facts.OwnBodyEvidence(version, "test-user", "own"),
        )

    def admit(self, comments):
        now = self.now.isoformat()
        observation = facts.TicketObservation(self.item, now, tuple(comments), "complete")
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            current = tapd_intake.observe_ticket(conn, self.config["tapd"], observation, self.publication, now=now)
            claim = source_sessions.claim(
                conn, source_id=self.ticket.source_id, owner="test", now=now, lease_seconds=3600
            )
            round_ = tapd_intake.admit_round(
                conn, self.config["tapd"], self.ticket, claim, round_id="round-1", revision=current.revision, now=now
            )
            self.assertIsNotNone(round_)
        return claim, current.revision

    def test_prepare_pins_real_source_prompt_and_bindings(self):
        claim, revision = self.admit([self.comment("1"), self.comment("2")])
        task = source_preparation.prepare_source_task(
            self.db, self.root, self.config, claim, round_id="round-1", revision=revision, repo_id="a", role="evidence"
        )
        self.assertIsNotNone(task)
        prompt = task["prompt_path"].read_text()
        self.assertIn("Complete ticket content", prompt)
        self.assertIn("Please inspect 1", prompt)
        self.assertIn("Please inspect 2", prompt)
        self.assertIn("native-v1", prompt)
        self.assertNotIn("planned_github_actions", prompt)

    def _prepare(self, claim, revision, *, role="evidence", repo_id="a", round_id="round-1", **kw):
        return source_preparation.prepare_source_task(
            self.db,
            self.root,
            self.config,
            claim,
            round_id=round_id,
            revision=revision,
            repo_id=repo_id,
            role=role,
            **kw,
        )

    def _observation(self, comments):
        return facts.TicketObservation(self.item, datetime.now(timezone.utc).isoformat(), tuple(comments), "complete")

    def _refresh(self, claim, comments):
        from robert_agent.work.application.admission import SourceDispatchObservation

        def refresh(ticket, *, task_id, attempt_id):
            self.assertEqual(ticket, self.ticket)
            self.assertTrue(task_id and attempt_id)
            return SourceDispatchObservation(claim, self._observation(comments), self.publication)

        return refresh

    def _dispatch(self, task, *, config=True, refresh=None, spawn=None):
        from unittest.mock import patch
        from robert_agent.execution.application import dispatch

        worker = self.config["workers"]["worker"]
        queue = []
        dispatch.queue_dispatch(queue, task, task["worktree_result"], repo_id=task["repo_id"], repo=task["repo"])
        with patch.object(dispatch.dispatch, "dispatch_worker", side_effect=spawn) as launch:
            result = dispatch.dispatch_queued(
                self.db,
                [task["repo_id"]],
                queue,
                False,
                1,
                workers=[worker],
                config_result=self.config if config else None,
                source_refresh=refresh,
            )
        return launch.call_count, result

    def _read(self, sql, values=()):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute(sql, values).fetchall()

    def _audit(self, repo_id="a"):
        from robert_agent.delivery.application import result_audit
        from robert_agent.persistence.stores import agent_runs
        from uuid import uuid4

        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            run_id = uuid4().hex
            now = datetime.now(timezone.utc).isoformat()
            agent_runs.insert_for_start_run(conn, run_id, now, None, 0, "{}")
            return result_audit.audit_completed_results(
                conn,
                repo_id,
                self.root,
                self.db,
                self.repo,
                run_id,
                now,
                False,
                [],
                0,
            )

    def _submit(self, task, events, *, output="local_result", extra=None):
        from robert_agent.delivery.application import submission

        return submission.record_result(
            self.db,
            {
                "task_id": task["task_id"],
                "attempt_id": task["attempt_id"],
                "output_type": output,
                "submission": {
                    "planned_actions": [],
                    "consumed_event_fingerprints": [event.version.event_id for event in events],
                    "used_skills": [],
                    "verification": [],
                    "handoff": "done",
                    **(
                        extra
                        or {
                            "evidence": [
                                {
                                    "repo_id": task["repo_id"],
                                    "path": "README.md",
                                    "revision": "sha256:" + sha256(b"Evidence\n").hexdigest(),
                                }
                            ]
                        }
                    ),
                },
            },
        )

    def _stop(self, task):
        from robert_agent.persistence.stores import attempts

        with closing(database.connect(self.db)) as conn, conn:
            now = datetime.now(timezone.utc).isoformat()
            attempts.update_for_supervise_running_attempts_4(conn, now, now, task["attempt_id"])

    def _started(self, **kw):
        return {
            "ok": True,
            "status": "running",
            "pid": 1234567,
            "task_id": kw["task_id"],
            "attempt_id": kw["attempt_id"],
        }

    def test_dispatch_refreshes_all_members_and_accepts_frozen_running_edit(self):
        first, second, future = self.comment("1"), self.comment("2"), self.comment("3")
        claim, revision = self.admit([first, second])
        task = self._prepare(claim, revision)
        frozen = task["prompt_path"].read_bytes()
        self.assertEqual(self._dispatch(task, config=False, refresh=self._refresh(claim, [first, second]))[0], 0)
        self.assertEqual(self._dispatch(task)[0], 0)

        def launch(**kw):
            self.assertEqual(kw["prompt_path"].read_bytes(), frozen)
            self.assertEqual(kw["worktree_path"], self.repo["repo_root"])
            active = self._read("SELECT json_extract(metadata_json,'$.dispatch_inflight') FROM execution_subjects")
            self.assertEqual(active, [(task["attempt_id"],)])
            return self._started(**kw)

        calls, dispositions = self._dispatch(task, refresh=self._refresh(claim, [first, second]), spawn=launch)
        self.assertEqual(calls, 1, dispositions)
        self.assertEqual(dispositions[0]["status"], "running")
        edited = self.comment("2", suffix=" with clarification")
        observation = self._observation([first, edited, future])
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            tapd_intake.observe_ticket(
                conn, self.config["tapd"], observation, self.publication, now=observation.observed_at
            )
        rejected = self._submit(task, [first, second, future])
        self.assertEqual(rejected["field_path"], "consumed_event_fingerprints", rejected)
        accepted = self._submit(task, [first, second])
        self.assertEqual(accepted["status"], "recorded", accepted)
        self.assertEqual(self._audit(), 0)
        self._stop(task)
        self.assertEqual(self._audit(), 1)
        self.assertEqual(task["prompt_path"].read_bytes(), frozen)
        self.assertEqual(self._read("SELECT lifecycle FROM tasks"), [("completed",)])
        self.assertEqual(self._read("SELECT DISTINCT state,task_id FROM request_events"), [("pending", None)])

    def test_rotating_provider_proof_unchanged_input_launches_once(self):
        from dataclasses import replace

        comment = self.comment("1")
        claim, revision = self.admit([comment])
        task = self._prepare(claim, revision)
        renewed = replace(
            comment,
            creator=replace(comment.creator, evidence_id="fresh-proof"),
            own_body=replace(comment.own_body, evidence_id="fresh-own-proof"),
        )
        calls, rows = self._dispatch(task, refresh=self._refresh(claim, [renewed]), spawn=self._started)
        self.assertEqual(calls, 1, rows)

    def _latest_revision(self):
        [[revision]] = self._read("SELECT json_extract(metadata_json,'$.revision') FROM sources WHERE provider='tapd'")
        return revision

    def _replace_round(self, claim, *, next_round="replacement"):
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            return tapd_intake.replace_unlaunched_round(
                conn,
                self.config,
                self.ticket,
                claim,
                round_id="round-1",
                replacement_id=next_round,
                revision=self._latest_revision(),
                now=datetime.now(timezone.utc).isoformat(),
            )

    def test_initial_ticket_edit_retires_and_launches_refrozen_prompt(self):
        from dataclasses import replace

        project = self.config["tapd"]["projects"][0]
        project["rules"] = [
            {
                "id": "queue",
                "assignee": "maintainer",
                "label": "label",
                "repo": "org/alpha",
                "kinds": ["bug"],
            }
        ]
        self.item = replace(self.item, owners=("maintainer",), labels=("label",))
        claim, revision = self.admit([])
        old = self._prepare(claim, revision, role="classification")
        before = old["prompt_path"].read_bytes()
        self.item = replace(self.item, body="Revised entire ticket content")
        calls, rows = self._dispatch(old, refresh=self._refresh(claim, []), spawn=self._started)
        self.assertEqual(calls, 0, rows)
        self.assertEqual(rows[0]["reason_code"], "native_input_changed")
        replacement = self._replace_round(claim)
        self.assertIsNotNone(replacement)
        self.assertEqual(self._read("SELECT status FROM source_rounds WHERE round_id='round-1'"), [("canceled",)])
        self.assertEqual(self._read("SELECT status FROM attempts"), [("canceled",)])
        self.assertEqual(self._read("SELECT state FROM request_events"), [("pending",)])
        self.assertIn(
            "Revised entire ticket content", self._read("SELECT content_snapshot_json FROM ingress_requests")[0][0]
        )
        current = self._prepare(claim, self._latest_revision(), role="classification", round_id="replacement")
        self.assertIn("Revised entire ticket content", current["prompt_path"].read_text())
        self.assertEqual(self._dispatch(current, refresh=self._refresh(claim, []), spawn=self._started)[0], 1)
        self.assertEqual(old["prompt_path"].read_bytes(), before)

    def test_new_prelaunch_trigger_remains_future_context_after_atomic_replacement(self):
        first, future = self.comment("1"), self.comment("2")
        claim, revision = self.admit([first])
        old = self._prepare(claim, revision)
        calls, rows = self._dispatch(old, refresh=self._refresh(claim, [first, future]), spawn=self._started)
        self.assertEqual((calls, rows[0]["reason_code"]), (0, "native_input_changed"))
        self.assertIsNotNone(self._replace_round(claim))
        [[same_member]] = self._read(
            "SELECT count(DISTINCT request_id) FROM source_round_requests WHERE round_id IN ('round-1','replacement')"
        )
        self.assertEqual(same_member, 1)
        task = self._prepare(claim, self._latest_revision(), round_id="replacement")
        self.assertIn('"request_scope": "future_request"', task["prompt_path"].read_text())
        self.assertEqual(self._dispatch(task, refresh=self._refresh(claim, [first, future]), spawn=self._started)[0], 1)
        self.assertEqual(self._submit(task, [future])["field_path"], "consumed_event_fingerprints")

    def test_unknown_or_checkpointed_launch_cannot_retire_stale_input(self):
        comment = self.comment("1")
        claim, revision = self.admit([comment])
        task = self._prepare(claim, revision)
        edited = self.comment("1", suffix=" ordinary change")
        for inflight in ("missing-attempt", task["attempt_id"]):
            with self.subTest(inflight=inflight), closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute(
                    "UPDATE execution_subjects SET metadata_json=json_object('dispatch_inflight',?)", (inflight,)
                )
            calls, rows = self._dispatch(task, refresh=self._refresh(claim, [edited]), spawn=self._started)
            self.assertEqual(calls, 0, rows)
            self.assertIsNone(self._replace_round(claim))
        self.assertEqual(self._read("SELECT status FROM source_rounds"), [("active",)])

    def test_prompt_edited_before_launch_is_preserved_and_held(self):
        first, second = self.comment("1"), self.comment("2")
        claim, revision = self.admit([first, second])
        task = self._prepare(claim, revision)
        original = task["prompt_path"].read_bytes()
        edited = self.comment("2", suffix=" changed")
        calls, dispositions = self._dispatch(task, refresh=self._refresh(claim, [first, edited]), spawn=self._started)
        self.assertEqual(calls, 0)
        self.assertEqual(dispositions[0]["reason_code"], "native_input_changed")
        self.assertEqual(task["prompt_path"].read_bytes(), original)
        self.assertEqual(self._read("SELECT status FROM attempts"), [("prepared",)])
        self.assertEqual(self._read("SELECT DISTINCT phase FROM ingress_requests"), [("pending",)])

    def test_revoked_secondary_member_blocks_launch_and_result(self):
        first, second = self.comment("1"), self.comment("2")
        claim, revision = self.admit([first, second])
        task = self._prepare(claim, revision)
        changed = self.comment("2", mention=False)
        calls, dispositions = self._dispatch(task, refresh=self._refresh(claim, [first, changed]), spawn=self._started)
        self.assertEqual(calls, 0)
        self.assertEqual(dispositions[0]["reason_code"], "native_authorization_unavailable")
        self.assertEqual(self._submit(task, [first])["status"], "unsupported_contract")

    def test_readonly_never_prepares_development_and_second_repository_waits(self):
        beta = {**self.repo, "full_name": "org/beta"}
        self.config["repos"].append(beta)
        self.config["tapd"]["projects"][0]["repositories"].append("org/beta")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) VALUES('b',?,?,?,?,?)",
                tuple(beta.values()),
            )
        comment = self.comment("1")
        claim, revision = self.admit([comment])
        self.assertIsNone(self._prepare(claim, revision, role="development"))
        self.assertEqual(self._read("SELECT count(*) FROM work_items"), [(0,)])
        first = self._prepare(claim, revision, role="classification")
        self.assertIsNone(self._prepare(claim, revision, repo_id="b"))
        calls, dispositions = self._dispatch(first, refresh=self._refresh(claim, [comment]), spawn=self._started)
        self.assertEqual(calls, 1, dispositions)
        bad = self._submit(first, [comment], output="classification_result", extra={"recommended_route": "new-pr"})
        self.assertEqual(bad["field_path"], "recommended_route")
        good = self._submit(
            first, [comment], output="classification_result", extra={"recommended_route": "comment-analysis"}
        )
        self.assertEqual(good["status"], "recorded", good)
        self.assertIsNone(self._prepare(claim, revision, repo_id="b"))
        self._stop(first)
        self.assertEqual(self._audit(), 1)
        edited = self.comment("1", suffix=" ordinary clarification after launch")
        current = self._observation([edited])
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            revision = tapd_intake.observe_ticket(
                conn, self.config["tapd"], current, self.publication, now=current.observed_at
            ).revision
        second = self._prepare(claim, revision, repo_id="b")
        self.assertIsNotNone(second)
        self.assertNotEqual(first["workstream_id"], second["workstream_id"])
        self.assertNotIn("ordinary clarification after launch", second["prompt_path"].read_text())
        calls, dispositions = self._dispatch(second, refresh=self._refresh(claim, [edited]), spawn=self._started)
        self.assertEqual(calls, 1, dispositions)
        self.assertEqual(self._read("SELECT repo_id FROM work_items ORDER BY repo_id"), [("a",), ("b",)])

    def test_author_change_after_first_launch_blocks_the_next_sibling(self):
        from dataclasses import replace

        comment = self.comment("1")
        claim, revision = self.admit([comment])
        first = self._prepare(claim, revision)
        self.assertEqual(self._dispatch(first, refresh=self._refresh(claim, [comment]), spawn=self._started)[0], 1)
        self.assertEqual(self._submit(first, [comment])["status"], "recorded")
        self._stop(first)
        self.assertEqual(self._audit(), 1)
        sibling = self._prepare(claim, self._latest_revision())
        changed = replace(
            comment,
            creator=replace(comment.creator, principal="other"),
            own_body=replace(comment.own_body, principal="other"),
        )
        calls, rows = self._dispatch(sibling, refresh=self._refresh(claim, [changed]), spawn=self._started)
        self.assertEqual((calls, rows[0]["reason_code"]), (0, "native_authorization_unavailable"))
        self.assertIsNone(self._replace_round(claim))
        self.assertEqual(self._read("SELECT state FROM request_events"), [("revoked",)])

    def test_disabled_repository_is_excluded_before_round_candidates(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE repos SET enabled=0")
        comment = self.comment("1")
        now = self.now.isoformat()
        observation = facts.TicketObservation(self.item, now, (comment,), "complete")
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            observed = tapd_intake.observe_ticket(conn, self.config["tapd"], observation, self.publication, now=now)
            claim = source_sessions.claim(
                conn, source_id=self.ticket.source_id, owner="test", now=now, lease_seconds=20
            )
            self.assertIsNone(
                tapd_intake.admit_round(
                    conn,
                    self.config["tapd"],
                    self.ticket,
                    claim,
                    round_id="db-disabled",
                    revision=observed.revision,
                    now=now,
                    configured_repositories=self.config["repos"],
                )
            )
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE repos SET enabled=1")
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            self.assertIsNone(
                tapd_intake.admit_round(
                    conn,
                    self.config["tapd"],
                    self.ticket,
                    claim,
                    round_id="config-disabled",
                    revision=observed.revision,
                    now=now,
                    configured_repositories=[{**self.repo, "enabled": False}],
                )
            )
        self.assertEqual(self._read("SELECT COUNT(*) FROM source_rounds"), [(0,)])

    def test_root_fence_lost_during_workspace_io_prevents_any_preparation(self):
        from unittest.mock import patch
        from robert_agent.execution import workspace

        comment = self.comment("1")
        claim, revision = self.admit([comment])
        original = workspace.prepare_source_worktree

        def supersede(*args, **kwargs):
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute("UPDATE source_sessions SET owner='other',token=token+1")
            return original(*args, **kwargs)

        with patch.object(workspace, "prepare_source_worktree", side_effect=supersede):
            self.assertIsNone(self._prepare(claim, revision))
        self.assertEqual(self._read("SELECT count(*) FROM tasks"), [(0,)])
        self.assertEqual(self._read("SELECT count(*) FROM work_items"), [(0,)])
        self.assertEqual(list(self.root.glob("artifacts/**/*.md")), [])

    def test_supersession_after_refresh_blocks_final_launch(self):
        from unittest.mock import patch
        from robert_agent.execution import workspace

        comment = self.comment("1")
        claim, revision = self.admit([comment])
        task = self._prepare(claim, revision)
        original = workspace.validate_source_workspace

        def supersede(*args, **kwargs):
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute("UPDATE source_sessions SET owner='other',token=token+1")
            return original(*args, **kwargs)

        with patch.object(workspace, "validate_source_workspace", side_effect=supersede):
            calls, result = self._dispatch(task, refresh=self._refresh(claim, [comment]), spawn=self._started)
        self.assertEqual(calls, 0)
        self.assertEqual(result[0]["reason_code"], "native_authorization_unavailable")
        self.assertEqual(
            self._read("SELECT json_extract(metadata_json,'$.dispatch_inflight') FROM execution_subjects"), [(None,)]
        )

    def test_repository_alias_is_pinned_and_retargeting_is_refused(self):
        real_root = Path(self.repo["repo_root"])
        alias = self.root / "checkout-alias"
        alias.symlink_to(real_root, target_is_directory=True)
        self.repo["repo_root"] = str(alias)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE repos SET repo_root=?", (str(alias),))
        comment = self.comment("1")
        claim, revision = self.admit([comment])
        task = self._prepare(claim, revision)
        self.assertEqual(task["worktree_result"]["worktree_path"], str(real_root))
        other = self.root / "other"
        other.mkdir()
        alias.unlink()
        alias.symlink_to(other, target_is_directory=True)
        calls, result = self._dispatch(task, refresh=self._refresh(claim, [comment]), spawn=self._started)
        self.assertEqual(calls, 0)
        self.assertEqual(result[0]["reason_code"], "native_frozen_workspace_or_prompt_unavailable")

    def _save_unknown_tapd_intent(self):
        import json

        target = {
            "provider": "tapd",
            "host": self.ticket.host,
            "workspace_id": self.ticket.workspace_id,
            "kind": self.ticket.kind,
            "external_id": self.ticket.external_id,
        }
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "INSERT INTO source_results(source_result_id,source_id,kind,input_version,idempotency_key,"
                "evidence_json,payload_json,accepted_at) VALUES ('root',?,'control_notice','v1','notice','{}','{}','now')",
                (self.ticket.source_id,),
            )
            conn.execute(
                "INSERT INTO github_actions(action_id,source_result_id,action_type,provider,target_json,audit_status,created_at)"
                " VALUES ('unknown-action','root','comment','tapd',?,'accepted','now')",
                (json.dumps(target),),
            )
            conn.execute(
                "INSERT INTO publication_intents(publication_id,action_id,idempotency_key,request_fingerprint,status,"
                "created_at,updated_at) VALUES ('unknown-intent','unknown-action','sent','fp','executing','now','now')"
            )

    def test_refresh_callbacks_cannot_hide_new_unknown_publication(self):
        comment = self.comment("1")
        claim, revision = self.admit([comment])
        task = self._prepare(claim, revision)
        read = self._refresh(claim, [comment])

        def refresh(ticket, **kwargs):
            old = read(ticket, **kwargs)
            self._save_unknown_tapd_intent()
            return old

        calls, outcomes = self._dispatch(task, refresh=refresh, spawn=self._started)
        self.assertEqual(calls, 0)
        self.assertEqual(outcomes[0]["reason_code"], "native_authorization_unavailable")
        self.assertEqual(
            self._read("SELECT json_extract(metadata_json,'$.unknown_intents[0]') FROM sources WHERE provider='tapd'"),
            [("unknown-intent",)],
        )

    def test_publication_changed_after_refresh_blocks_final_launch(self):
        from unittest.mock import patch
        from robert_agent.execution import workspace

        comment = self.comment("1")
        claim, revision = self.admit([comment])
        task = self._prepare(claim, revision)
        original = workspace.validate_source_workspace

        def new_intent(*args, **kwargs):
            self._save_unknown_tapd_intent()
            return original(*args, **kwargs)

        with patch.object(workspace, "validate_source_workspace", side_effect=new_intent):
            calls, outcomes = self._dispatch(task, refresh=self._refresh(claim, [comment]), spawn=self._started)
        self.assertEqual(calls, 0)
        self.assertEqual(outcomes[0]["reason_code"], "native_publication_evidence_changed")

    def _answer_for_root(self):
        first, second = self.comment("1"), self.comment("2")
        claim, revision = self.admit([first, second])
        task = self._prepare(claim, revision, role="answer")
        self.assertIsNotNone(task)
        calls, dispositions = self._dispatch(task, refresh=self._refresh(claim, [first, second]), spawn=self._started)
        self.assertEqual(calls, 1, dispositions)
        accepted = self._submit(
            task,
            [first],
            output="comment_analysis",
            extra={
                "planned_actions": [
                    {"provider": "tapd", "type": "comment", "target_id": "ticket", "body": "Repository findings"}
                ]
            },
        )
        self.assertEqual(accepted["status"], "recorded", accepted)
        revision = self._read("SELECT json_extract(metadata_json,'$.revision') FROM sources WHERE provider='tapd'")[0][
            0
        ]
        return claim, task, accepted["result_id"], revision

    def _accept_root(self, claim, worker_result, revision, *, config=None):
        from robert_agent.delivery.application.source_results import accept_worker_result

        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            return accept_worker_result(
                conn,
                self.config if config is None else config,
                self.ticket,
                claim,
                round_id="round-1",
                worker_result_id=worker_result,
                revision=revision,
                now=datetime.now(timezone.utc).isoformat(),
            )

    def test_audited_answer_consumes_handled_members_once_but_delivery_remains_pending(self):
        from robert_agent.persistence.queries import source_results as results, source_sessions as rounds

        claim, task, worker_result, revision = self._answer_for_root()
        self.assertIsNone(self._accept_root(claim, worker_result, revision))
        self._stop(task)
        self.assertIsNone(self._accept_root(claim, worker_result, revision))
        self.assertEqual(self._audit(), 1)
        root = self._accept_root(claim, worker_result, revision)
        self.assertIsNotNone(root)
        self.assertEqual(self._accept_root(claim, worker_result, revision), root)
        self.assertEqual(
            self._read("SELECT state,result_id,source_result_id FROM request_events ORDER BY state"),
            [("consumed", None, root), ("pending", None, None)],
        )
        self.assertEqual(self._read("SELECT lifecycle FROM tasks"), [("completed",)])
        self.assertEqual(self._read("SELECT kind,worker_result_id FROM source_results"), [("answer", worker_result)])
        with closing(database.connect(self.db)) as conn:
            actions = results.required_actions(conn, root)
            self.assertEqual(len(actions), 1)
            self.assertEqual((actions[0]["task_id"], actions[0]["result_id"]), (task["task_id"], worker_result))
            self.assertEqual(actions[0]["publish_status"], "not_published")
            blocked = rounds.blockers_for_source(conn, self.ticket.source_id)
            self.assertEqual([(row.kind, row.identity) for row in blocked], [("publication", actions[0]["action_id"])])
            self.assertFalse(results.tapd_publication_evidence(conn, self.ticket.source_id).own_comments)

    def test_root_refuses_revocation_missing_repository_and_stale_claim(self):
        from dataclasses import replace

        claim, task, worker_result, revision = self._answer_for_root()
        self._stop(task)
        self.assertEqual(self._audit(), 1)
        self.assertIsNone(self._accept_root(replace(claim, token=claim.token + 1), worker_result, revision))
        self.assertIsNone(self._accept_root(claim, worker_result, revision, config={**self.config, "repos": []}))
        self.assertIsNone(
            self._accept_root(
                claim, worker_result, revision, config={**self.config, "repos": [{**self.repo, "enabled": False}]}
            )
        )
        observation = self._observation([self.comment("1"), self.comment("2", mention=False)])
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            current = tapd_intake.observe_ticket(
                conn, self.config["tapd"], observation, self.publication, now=observation.observed_at
            )
        self.assertIsNone(self._accept_root(claim, worker_result, current.revision))
        self.assertEqual(self._read("SELECT COUNT(*) FROM source_results"), [(0,)])
        self.assertEqual(self._read("SELECT COUNT(*) FROM request_events WHERE state='consumed'"), [(0,)])

    def test_root_acceptance_rolls_back_ledger_and_action_links_together(self):
        from unittest.mock import patch
        from robert_agent.persistence.stores import source_results as store

        claim, task, worker_result, revision = self._answer_for_root()
        self._stop(task)
        self.assertEqual(self._audit(), 1)
        with patch.object(store, "finish_worker_execution", return_value=False):
            # Assert rollback within the caller's live writer, before outer context exits.
            from robert_agent.delivery.application.source_results import accept_worker_result
            from robert_agent.persistence.queries import source_results as results

            with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
                transactions.begin_immediate(conn)
                with self.assertRaisesRegex(ValueError, "task changed"):
                    accept_worker_result(
                        conn,
                        self.config,
                        self.ticket,
                        claim,
                        round_id="round-1",
                        worker_result_id=worker_result,
                        revision=revision,
                        now=datetime.now(timezone.utc).isoformat(),
                    )
                self.assertIsNone(results.for_round(conn, "round-1"))
        self.assertEqual(self._read("SELECT COUNT(*) FROM request_events WHERE state='consumed'"), [(0,)])
        self.assertEqual(self._read("SELECT COUNT(*) FROM source_required_actions"), [(0,)])
        self.assertIsNotNone(self._accept_root(claim, worker_result, revision))

    def test_fixed_conflict_notice_needs_no_repo_or_round_and_checks_receipts(self):
        from dataclasses import replace
        import json

        from robert_agent.delivery.application.source_results import accept_control_notice
        from robert_agent.delivery.tapd_policy import prepare_comment
        from robert_agent.persistence.queries import source_results as results, source_sessions as rounds
        from robert_agent.persistence.stores import source_results as store

        project = self.config["tapd"]["projects"][0]
        project["repositories"] = ["org/alpha", "org/beta"]
        project["rules"] = [
            {"id": key, "assignee": "owner", "label": key, "repo": f"org/{repo}", "kinds": ["bug"]}
            for key, repo in (("a", "alpha"), ("b", "beta"))
        ]
        self.item = replace(self.item, owners=("owner",), labels=("a", "b"))
        observation = self._observation([])
        now = observation.observed_at
        with closing(sqlite3.connect(self.db)) as raw, raw:
            raw.execute("DELETE FROM repos")
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            current = tapd_intake.observe_ticket(conn, self.config["tapd"], observation, self.publication, now=now)
            claim = source_sessions.claim(
                conn, source_id=self.ticket.source_id, owner="test", now=now, lease_seconds=3600
            )
            result = accept_control_notice(conn, self.config, self.ticket, claim, revision=current.revision, now=now)
            self.assertIsNotNone(result, current)
            self.assertEqual(
                accept_control_notice(conn, self.config, self.ticket, claim, revision=current.revision, now=now), result
            )
            self.assertIsNone(accept_control_notice(conn, self.config, self.ticket, claim, revision="stale", now=now))
            [action] = results.required_actions(conn, result)
            self.assertIsNone(action["task_id"])
            self.assertEqual(action["source_result_id"], result)
            self.assertFalse(results.tapd_publication_evidence(conn, self.ticket.source_id).unknown_intents)
            self.assertEqual(len(rounds.blockers_for_source(conn, self.ticket.source_id)), 1)
        self.assertEqual(self._read("SELECT kind,round_id FROM source_results"), [("control_notice", None)])
        for table in ("repos", "tasks", "ingress_requests", "request_events", "source_rounds"):
            self.assertEqual(self._read(f"SELECT count(*) FROM {table}"), [(0,)])

        action_id = action["action_id"]
        self.assertIsNotNone(self._delivery_authority(claim, result, action_id, self._observation([])))
        original_item = self.item
        self.item = replace(self.item, labels=())
        self.assertIsNone(self._delivery_authority(claim, result, action_id, self._observation([])))
        self.item = original_item
        prepared = prepare_comment("Repository choice", "rbt-0123456789abcd", bot_name="helper-team", reply_name="desk")
        target = json.loads(action["target_json"])
        with closing(sqlite3.connect(self.db)) as raw, raw:
            raw.execute(
                "INSERT INTO publication_intents(publication_id,action_id,idempotency_key,request_fingerprint,status,"
                "created_at,updated_at) VALUES('intent',?,'key','fingerprint','prepared',?,?)",
                (action_id, now, now),
            )
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            store.insert_tapd_publication_preparation(
                conn,
                publication_id="intent",
                evidence={
                    "target": target,
                    "action_id": action_id,
                    "body": prepared.body,
                    "receipt_id": prepared.receipt_id,
                    "body_digest": prepared.body_digest,
                    "normalization_version": prepared.normalization_version,
                    "bot_name": prepared.bot_name,
                    "reply_name": prepared.reply_name,
                    "publisher_principal": "tapd-bot-account",
                    "publisher_identity_evidence_id": "verified-publisher-1",
                    "publisher_observed_at": now,
                },
            )
        receipt = {
            "status": "confirmed_effect",
            "receipt": {
                "action_id": action_id,
                "target": target,
                "entry_type": "bug",
                "external_id": "42",
                "receipt_id": prepared.receipt_id,
                "body_digest": prepared.body_digest,
                "publisher_principal": "tapd-bot-account",
                "identity_evidence_id": "verified-readback-1",
                "own_body_evidence_id": "verified-own-body-1",
                "confirmed_at": now,
            },
        }
        with closing(sqlite3.connect(self.db)) as raw, raw:
            raw.execute("UPDATE publication_intents SET status='attention' WHERE publication_id='intent'")
        with closing(database.connect(self.db)) as conn:
            self.assertEqual(
                results.tapd_publication_evidence(conn, self.ticket.source_id).unknown_intents, ("intent",)
            )
        with closing(sqlite3.connect(self.db)) as raw, raw:
            raw.execute(
                "UPDATE github_actions SET publish_status='published', external_id='42' WHERE action_id=?", (action_id,)
            )
            raw.execute("UPDATE publication_intents SET status='published' WHERE publication_id='intent'")
            raw.execute("UPDATE publication_intents SET remote_evidence_json=?", (json.dumps(receipt),))
        with closing(database.connect(self.db)) as conn:
            readback = results.tapd_publication_evidence(conn, self.ticket.source_id)
            self.assertEqual(readback.own_comments, frozenset({("bug", "42")}))
            self.assertFalse(readback.unknown_intents)
            self.assertFalse(rounds.blockers_for_source(conn, self.ticket.source_id))
        receipt["receipt"]["target"] = {**target, "host": "other.tapd.example"}
        with closing(sqlite3.connect(self.db)) as raw, raw:
            raw.execute("UPDATE publication_intents SET remote_evidence_json=?", (json.dumps(receipt),))
        with closing(database.connect(self.db)) as conn:
            result = results.tapd_publication_evidence(conn, self.ticket.source_id)
            self.assertFalse(result.own_comments)
            self.assertEqual(result.unknown_intents, ("intent",))
            self.assertEqual(
                [row.identity for row in rounds.blockers_for_source(conn, self.ticket.source_id)], ["intent"]
            )

    def test_exact_native_spool_left_after_consumption_is_acknowledged(self):
        import json
        from unittest.mock import patch

        from robert_agent.delivery.application import submission
        from robert_agent.execution import spool

        original_unlink = Path.unlink

        def leave_pending_record(path, *args, **kw):
            if path.parent == spool.directory(self.db) and path.suffix == ".json":
                return
            return original_unlink(path, *args, **kw)

        with patch.object(Path, "unlink", new=leave_pending_record):
            claim, task, worker_result, revision = self._answer_for_root()
        self._stop(task)
        self.assertEqual(self._audit(), 1)
        root = self._accept_root(claim, worker_result, revision)
        self.assertIsNotNone(root)
        [path] = spool.directory(self.db).glob("*.json")
        exact = json.loads(path.read_text())
        counts = submission.replay_pending_results(self.db, attempt_id=task["attempt_id"])
        self.assertEqual(counts, {"recorded": 1, "deferred": 0, "rejected": 0})
        self.assertFalse(path.exists())
        duplicate = self._submit(
            task,
            [self.comment("1")],
            output="comment_analysis",
            extra={
                "planned_actions": [
                    {"provider": "tapd", "type": "comment", "target_id": "ticket", "body": "Repository findings"}
                ]
            },
        )
        self.assertEqual(
            duplicate, {"ok": True, "status": "recorded", "result_id": worker_result, "deduplicated": True}
        )
        for field, value in (
            ("handoff", "tampered while keeping the saved fingerprint"),
            ("consumed_event_fingerprints", [self.comment("2").version.event_id]),
            ("planned_actions", [{**exact["payload"]["planned_actions"][0], "body": "Tampered reply body"}]),
            ("task_id", "wrong-task"),
            ("attempt_id", "wrong-attempt"),
            ("_result_contract", "compact-v1"),
            ("_normalizer_version", 999),
        ):
            with self.subTest(field=field):
                spool.atomic_write(path, {**exact, "payload": {**exact["payload"], field: value}})
                rejected = submission.replay_pending_results(self.db, attempt_id=task["attempt_id"])
                self.assertEqual(rejected, {"recorded": 0, "deferred": 0, "rejected": 1})
        self.assertEqual(self._read("SELECT COUNT(*) FROM worker_results"), [(1,)])
        self.assertEqual(self._read("SELECT COUNT(*) FROM github_actions"), [(1,)])
        self.assertEqual(
            self._read("SELECT COUNT(*) FROM request_events WHERE state='consumed' AND source_result_id=?", (root,)),
            [(1,)],
        )

    def _accepted_answer_for_delivery(self):
        claim, task, worker_result, revision = self._answer_for_root()
        self._stop(task)
        self.assertEqual(self._audit(), 1)
        root = self._accept_root(claim, worker_result, revision)
        self.assertIsNotNone(root)
        [[action_id]] = self._read("SELECT action_id FROM github_actions")
        return claim, task, root, action_id

    def _delivery_authority(self, claim, root, action_id, snapshot, *, config=None, now=None):
        from robert_agent.delivery.application.source_results import authorize_delivery

        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            return authorize_delivery(
                conn,
                self.config if config is None else config,
                self.ticket,
                claim,
                source_result_id=root,
                action_id=action_id,
                observation=snapshot,
                now=now or snapshot.observed_at,
            )

    def test_authorize_current_delivery_uses_exact_consumed_member_without_pendingness_bypass(self):
        from robert_agent.delivery.application.source_results import authorize_delivery

        claim, task, root, action_id = self._accepted_answer_for_delivery()
        snapshot = self._observation([self.comment("1", suffix=" with ordinary edit"), self.comment("2")])
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            grant = authorize_delivery(
                conn,
                self.config,
                self.ticket,
                claim,
                source_result_id=root,
                action_id=action_id,
                observation=snapshot,
                now=snapshot.observed_at,
            )
            self.assertIsNotNone(grant)
            self.assertEqual(grant.source_result_id, root)
            self.assertEqual(grant.action_id, action_id)
            self.assertFalse(source_sessions.pending_round_members(conn, "round-1"))
            from robert_agent.delivery.application.validation import context

            self.assertIsNotNone(context(conn, task["task_id"], task["attempt_id"], include_scope=False)[1])

    def test_delivery_refuses_foreign_scope_stale_observation_and_removed_configuration(self):
        from copy import deepcopy
        from dataclasses import replace

        claim, _, root, action_id = self._accepted_answer_for_delivery()
        snapshot = self._observation([self.comment("1"), self.comment("2")])
        self.assertIsNone(self._delivery_authority(claim, root, "unrelated-action", snapshot))
        self.assertIsNone(self._delivery_authority(claim, "foreign-root", action_id, snapshot))
        self.assertIsNone(self._delivery_authority(replace(claim, token=claim.token + 1), root, action_id, snapshot))
        self.assertIsNone(self._delivery_authority(claim, root, action_id, self._observation([])))
        for mutation in ("disabled_provider", "disabled_repository", "removed_project", "removed_mapping"):
            with self.subTest(mutation=mutation):
                config = deepcopy(self.config)
                if mutation == "disabled_provider":
                    config["tapd"]["enabled"] = False
                elif mutation == "disabled_repository":
                    config["repos"][0]["enabled"] = False
                elif mutation == "removed_project":
                    config["tapd"]["projects"] = []
                else:
                    config["tapd"]["projects"][0]["repositories"] = []
                self.assertIsNone(
                    self._delivery_authority(
                        claim, root, action_id, self._observation([self.comment("1"), self.comment("2")]), config=config
                    )
                )
        self.assertIsNone(self._delivery_authority(claim, root, action_id, snapshot))
        self.assertEqual(
            self._read("SELECT source_result_id,state FROM request_events WHERE source_result_id=?", (root,)),
            [(root, "consumed")],
        )

    def test_consumed_comment_revocation_refuses_delivery_and_preserves_consumption(self):
        from dataclasses import replace

        claim, _, root, action_id = self._accepted_answer_for_delivery()
        first = self.comment("1")
        second = self.comment("2")
        creator_changed = replace(
            first, creator=facts.CreatorEvidence(first.version, "someone-else", "different-verified-creator")
        )
        unknown_creator = replace(first, creator=None)
        for snapshot in (
            self._observation([unknown_creator, second]),
            self._observation([creator_changed, second]),
            self._observation([self.comment("1", mention=False), second]),
            replace(
                self._observation([second]),
                absences=(facts.CommentAbsence(self.ticket, "bug", "1", "verified-deletion"),),
            ),
        ):
            with self.subTest(snapshot=snapshot.observed_at):
                updated = replace(snapshot, observed_at=datetime.now(timezone.utc).isoformat())
                self.assertIsNone(self._delivery_authority(claim, root, action_id, updated))
                self.assertEqual(
                    self._read("SELECT state FROM request_events WHERE source_result_id=?", (root,)), [("consumed",)]
                )
        self.assertIsNone(self._delivery_authority(claim, root, action_id, self._observation([first, second])))

    def test_delivery_refuses_explicit_round_cancel(self):
        from robert_agent.persistence.stores import tapd_intake as store

        claim, _, root, action_id = self._accepted_answer_for_delivery()
        comments = [self.comment("1"), self.comment("2")]
        self.assertIsNotNone(self._delivery_authority(claim, root, action_id, self._observation(comments)))
        now = datetime.now(timezone.utc).isoformat()
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            self.assertTrue(
                store.request_cancellation(
                    conn, source_id=self.ticket.source_id, round_id="round-1", reason="operator_pause", now=now
                )
            )
        self.assertIsNone(self._delivery_authority(claim, root, action_id, self._observation(comments)))
        self.assertEqual(
            self._read("SELECT state FROM request_events WHERE source_result_id=?", (root,)), [("consumed",)]
        )

    def test_unknown_effect_reconciliation_uses_original_scope_without_a_write_grant(self):
        from dataclasses import asdict
        import json

        from robert_agent.delivery.application.source_results import tapd_reconciliation_scope
        from robert_agent.delivery.tapd_policy import prepare_comment
        from robert_agent.persistence import execution_control
        from robert_agent.persistence.stores import source_results as store
        from robert_agent.work.application.source_observation import publication_facts

        claim, _, root, action_id = self._accepted_answer_for_delivery()
        initial = self._observation([self.comment("1"), self.comment("2")])
        with closing(database.connect(self.db, foreign_keys=True)) as conn:
            self.assertFalse(publication_facts(conn, self.ticket).unknown_intents)

        [[encoded]] = self._read("SELECT target_json FROM github_actions WHERE action_id=?", (action_id,))
        target = json.loads(encoded)
        prepared = prepare_comment("Repository findings", "rbt-abcdefghijkl", bot_name="helper-team", reply_name="desk")
        then = datetime.now(timezone.utc).isoformat()
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            intent = execution_control.prepare_publication_intent(conn, action_id, "key", "fingerprint", then)
            publication_id = intent["publication_id"]
            store.insert_tapd_publication_preparation(
                conn,
                publication_id=publication_id,
                evidence={
                    **asdict(prepared),
                    "action_id": action_id,
                    "target": target,
                    "publisher_principal": "original-publisher-account",
                },
            )
            execution_control.transition_publication_intent(conn, publication_id, "executing", then)

        # The external snapshot was prepared before another writer installed an unknown effect.
        self.assertIsNone(
            self._delivery_authority(claim, root, action_id, initial, now=datetime.now(timezone.utc).isoformat())
        )
        [[unknown]] = self._read(
            "SELECT json_extract(metadata_json,'$.unknown_intents') FROM sources WHERE provider='tapd'"
        )
        self.assertEqual(json.loads(unknown), [publication_id])
        revoked = self._observation([self.comment("1", mention=False), self.comment("2")])
        self.assertIsNone(self._delivery_authority(claim, root, action_id, revoked))
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            readback = tapd_reconciliation_scope(
                conn,
                {**self.config, "repos": []},
                self.ticket,
                claim,
                source_result_id=root,
                action_id=action_id,
                now=datetime.now(timezone.utc).isoformat(),
            )
            self.assertIsNotNone(readback)
            self.assertEqual((readback.publication_id, readback.target), (publication_id, self.ticket.key))
            self.assertEqual(readback.prepared, prepared)
            self.assertEqual(readback.publisher_principal, "original-publisher-account")
            execution_control.transition_publication_intent(conn, publication_id, "attention", then)

        self.assertIsNone(
            self._delivery_authority(claim, root, action_id, self._observation([self.comment("1"), self.comment("2")]))
        )
        self.assertEqual(self._read("SELECT status FROM publication_intents"), [("attention",)])
        self.assertEqual(
            self._read("SELECT state FROM request_events WHERE source_result_id=?", (root,)), [("consumed",)]
        )

    def test_accepted_root_authority_is_independent_of_round_execution_completion(self):
        claim, _, root, action_id = self._accepted_answer_for_delivery()
        for status, authorized in (
            ("execution_finished", True),
            ("waiting_for_user", True),
            ("canceled", False),
            ("failed", False),
        ):
            with self.subTest(status=status):
                now = datetime.now(timezone.utc).isoformat()
                with closing(sqlite3.connect(self.db)) as conn, conn:
                    conn.execute(
                        "UPDATE source_rounds SET status=?,finished_at=? WHERE round_id='round-1'", (status, now)
                    )
                current = self._delivery_authority(
                    claim, root, action_id, self._observation([self.comment("1"), self.comment("2")])
                )
                self.assertEqual(current is not None, authorized)
                self.assertEqual(self._read("SELECT status FROM source_rounds"), [(status,)])

    def test_terminal_ticket_revokes_delivery_of_execution_finished_round(self):
        from dataclasses import replace

        claim, _, root, action_id = self._accepted_answer_for_delivery()
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE source_rounds SET status='execution_finished',finished_at=? WHERE round_id='round-1'", (now,)
            )
        self.item = replace(self.item, status="closed")
        self.assertIsNone(
            self._delivery_authority(claim, root, action_id, self._observation([self.comment("1"), self.comment("2")]))
        )
        self.assertEqual(self._read("SELECT COUNT(*) FROM source_round_cancellations"), [(0,)])
        self.assertEqual(
            self._read("SELECT state FROM request_events WHERE source_result_id=?", (root,)), [("consumed",)]
        )

    def test_delivery_requires_exact_ledger_and_frozen_grant(self):
        claim, _, root, action_id = self._accepted_answer_for_delivery()
        [[request]] = self._read("SELECT request_id FROM request_events WHERE source_result_id=?", (root,))
        comments = [self.comment("1"), self.comment("2")]
        with closing(sqlite3.connect(self.db)) as conn, conn:
            [[auth]] = conn.execute("SELECT authorization_json FROM ingress_requests WHERE request_id=?", (request,))
            conn.execute("UPDATE ingress_requests SET authorization_json='{}' WHERE request_id=?", (request,))
        self.assertIsNone(self._delivery_authority(claim, root, action_id, self._observation(comments)))
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE ingress_requests SET authorization_json=? WHERE request_id=?", (auth, request))
            conn.execute("UPDATE request_events SET source_result_id=NULL WHERE request_id=?", (request,))
        self.assertIsNone(self._delivery_authority(claim, root, action_id, self._observation(comments)))
        self.assertEqual(self._read("SELECT state FROM request_events WHERE request_id=?", (request,)), [("consumed",)])

    def _run_source_model(self, claim, stage, run_id, output, *, evidence=(), children=()):
        import json

        from robert_agent.adapters.processes.source_model import SourceModelOutcome, SourceModelProfile
        from robert_agent.execution.application import source_models

        [[request_id]] = self._read(
            "SELECT request_id FROM source_round_requests WHERE round_id='round-1' ORDER BY request_id LIMIT 1"
        )
        identity = {
            "pid": 1234567,
            "process_group_id": 1234567,
            "identity_source": "proc",
            "process_start_token": "mock-source-model",
            "command_fingerprint": "e" * 64,
        }
        profile = SourceModelProfile("model", Path("/controlled/claude"), "model", "high", 10, ())

        def runner(_profile, prompt, _stop, started):
            self.assertTrue(started(identity))
            envelope = json.loads(prompt)
            body = envelope.pop("prompt")
            self.assertIn("source_context_sha256:", body)
            self.assertIn("Complete ticket content", body)
            return SourceModelOutcome("read", json.dumps({**envelope, **output}).encode(), "group_exited", identity)

        return source_models.execute(
            self.db,
            claim,
            round_id="round-1",
            request_id=request_id,
            run_id=run_id,
            stage=stage,
            profile=profile,
            evidence=evidence,
            child_results=children,
            authorize=lambda *_: True,
            runner=runner,
        )

    def _accept_model(self, claim, run_id):
        from robert_agent.delivery.application.source_results import accept_model_result

        [[revision]] = self._read("SELECT json_extract(metadata_json,'$.revision') FROM sources WHERE provider='tapd'")
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            return accept_model_result(
                conn,
                self.config,
                self.ticket,
                claim,
                round_id="round-1",
                model_run_id=run_id,
                revision=revision,
                now=datetime.now(timezone.utc).isoformat(),
            )

    def test_identification_question_accepts_exact_handled_request_without_repo_task(self):
        first, second = self.comment("1"), self.comment("2")
        claim, _ = self.admit([first, second])
        outcome = self._run_source_model(
            claim,
            "identify",
            "question",
            {
                "kind": "question",
                "question": "Which behavior should I investigate?",
                "handled_event_fingerprints": [first.version.event_id],
            },
        )
        self.assertEqual(outcome.status, "succeeded", outcome)
        root = self._accept_model(claim, "question")
        self.assertIsNotNone(root)
        self.assertEqual(self._accept_model(claim, "question"), root)
        self.assertEqual(
            self._read("SELECT kind,model_run_id,worker_result_id FROM source_results"),
            [("question", "question", None)],
        )
        self.assertEqual(self._read("SELECT count(*) FROM tasks"), [(0,)])
        self.assertEqual(self._read("SELECT count(*) FROM work_items"), [(0,)])
        self.assertEqual(
            self._read("SELECT state,source_result_id FROM request_events ORDER BY state"),
            [("consumed", root), ("pending", None)],
        )
        [[action_id]] = self._read("SELECT action_id FROM github_actions WHERE source_result_id=?", (root,))
        self.assertIsNotNone(self._delivery_authority(claim, root, action_id, self._observation([first, second])))
        self.assertIsNone(
            self._delivery_authority(
                claim, root, action_id, self._observation([self.comment("1", mention=False), second])
            )
        )

    def _model_evidence_child(self, claim, revision, comments, *, repo_id="a", question=None):
        task = self._prepare(claim, revision, repo_id=repo_id)
        self.assertIsNotNone(task)
        calls, results = self._dispatch(task, refresh=self._refresh(claim, comments), spawn=self._started)
        self.assertEqual(calls, 1, results)
        if question:
            extra = {"investigation_question": question}
        else:
            extra = {
                "evidence": [
                    {
                        "repo_id": repo_id,
                        "path": "README.md",
                        "revision": "sha256:" + sha256(b"Evidence\n").hexdigest(),
                    }
                ]
            }
        stored = self._submit(task, comments, extra=extra)
        self.assertEqual(stored["status"], "recorded", stored)
        self._stop(task)
        self.assertEqual(self._audit(repo_id), 1)
        return task

    def test_synthesis_acceptance_rechecks_actual_child_exit_and_scoped_evidence(self):
        from robert_agent.adapters.git.source_evidence import EvidenceReader
        from robert_agent.execution.application import source_models
        from robert_agent.persistence.queries import source_sessions as rounds

        first = self.comment("1")
        claim, revision = self.admit([first])
        child = self._model_evidence_child(claim, revision, [first])
        self.assertEqual(self._read("SELECT state FROM request_events"), [("pending",)])
        (Path(self.repo["repo_root"]) / "README.md").write_text("Evidence\n")
        with EvidenceReader({"a": self.repo["repo_root"]}) as reader:
            evidence = reader.read("a", "README.md")
        with closing(database.connect(self.db)) as conn:
            children = source_models.synthesis_children(conn, rounds.active_round(conn, self.ticket.source_id))
        self.assertEqual(len(children), 1)
        reference = {"repo_id": "a", "path": evidence.path, "revision": evidence.revision}
        output = {
            "kind": "answer",
            "answer": "The repository has the documented behavior.",
            "references": [reference],
            "handled_event_fingerprints": [first.version.event_id],
        }
        result = self._run_source_model(
            claim, "synthesis", "synthesis", output, evidence=(evidence,), children=children
        )
        self.assertEqual(result.status, "succeeded", result)
        [[exit_proof]] = self._read("SELECT metadata_json FROM attempts WHERE attempt_id=?", (child["attempt_id"],))
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE attempts SET metadata_json=json_remove(metadata_json,'$.process_stopped_at') WHERE attempt_id=?",
                (child["attempt_id"],),
            )
        self.assertIsNone(self._accept_model(claim, "synthesis"))
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE attempts SET metadata_json=? WHERE attempt_id=?", (exit_proof, child["attempt_id"]))
        root = self._accept_model(claim, "synthesis")
        self.assertIsNotNone(root)
        self.assertEqual(self._read("SELECT kind,model_run_id FROM source_results"), [("answer", "synthesis")])
        [[action_id]] = self._read("SELECT action_id FROM github_actions WHERE source_result_id=?", (root,))
        self.assertIsNotNone(self._delivery_authority(claim, root, action_id, self._observation([first])))

    def test_synthesis_from_real_no_evidence_child_can_only_ask_question(self):
        from robert_agent.execution.application import source_models
        from robert_agent.persistence.queries import source_sessions as rounds

        first = self.comment("1")
        claim, revision = self.admit([first])
        self._model_evidence_child(claim, revision, [first], question="Which component should I inspect?")
        self.assertEqual(self._read("SELECT state FROM request_events"), [("pending",)])
        with closing(database.connect(self.db)) as conn:
            children = source_models.synthesis_children(conn, rounds.active_round(conn, self.ticket.source_id))
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0]["content"]["references"], [])
        outcome = self._run_source_model(
            claim,
            "synthesis",
            "question",
            {
                "kind": "question",
                "question": "Which component should I inspect?",
                "handled_event_fingerprints": [first.version.event_id],
            },
            children=children,
        )
        self.assertEqual(outcome.status, "succeeded", outcome)
        root = self._accept_model(claim, "question")
        self.assertIsNotNone(root)
        self.assertEqual(self._read("SELECT state,source_result_id FROM request_events"), [("consumed", root)])

    def test_legacy_frozen_question_is_resumable_local_proof_only(self):
        import json

        from robert_agent.adapters.processes.source_model import SourceModelOutcome, SourceModelProfile
        from robert_agent.context import source_prompts
        from robert_agent.execution.application import source_models
        from robert_agent.persistence.queries import source_sessions as rounds
        from robert_agent.persistence.rounds import source_model as store

        first = self.comment("1")
        claim, _ = self.admit([first])
        [[request_id]] = self._read("SELECT request_id FROM source_round_requests")
        profile = SourceModelProfile("model", Path("/controlled/claude"), "model", "high", 10, ())
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            round_ = rounds.active_round(conn, self.ticket.source_id)
            prompt = source_prompts.build(
                stage="identify",
                round_id="round-1",
                run_id="legacy",
                input_version=round_["input_version"],
                snapshot=json.loads(round_["input_snapshot_json"]),
                candidates=("a",),
                evidence=(),
            )
            packet = {
                "version": 1,
                "payload": prompt.payload,
                "references": [],
                "candidates": ["a"],
                "child_results": [],
                "profile": {
                    "worker_id": "model",
                    "executable": "/controlled/claude",
                    "model": "model",
                    "effort": "high",
                    "timeout": 10,
                    "credential_names": [],
                },
                "input_sha256": round_["input_sha256"],
            }
            row = store.enqueue(
                conn,
                claim,
                run_id="legacy",
                round_id="round-1",
                request_id=request_id,
                stage="identify",
                worker_id="model",
                input_version=round_["input_version"],
                prompt_sha256=prompt.prompt_sha256,
                input_packet=packet,
                now=datetime.now(timezone.utc).isoformat(),
            )
            frozen = row["input_packet_json"]

        identity = {
            "pid": 1234567,
            "process_group_id": 1234567,
            "identity_source": "proc",
            "process_start_token": "old-start",
            "command_fingerprint": "f" * 64,
        }

        def runner(_profile, payload, _stop, started):
            self.assertEqual(payload, prompt.payload)
            self.assertTrue(started(identity))
            output = {**prompt.binding, "kind": "question", "question": "Which component?"}
            return SourceModelOutcome("read", json.dumps(output).encode(), "group_exited", identity)

        result = source_models.execute(
            self.db,
            claim,
            round_id="round-1",
            request_id=request_id,
            run_id="legacy",
            stage="identify",
            profile=profile,
            authorize=lambda *_: True,
            runner=runner,
        )
        self.assertEqual(result.status, "succeeded", result)
        self.assertEqual(self._read("SELECT input_packet_json FROM source_model_runs"), [(frozen,)])
        self.assertIsNone(self._accept_model(claim, "legacy"))
        self.assertEqual(self._read("SELECT state FROM request_events"), [("pending",)])

    def test_identified_repositories_all_need_a_real_evidence_child_before_synthesis(self):
        import json

        from robert_agent.adapters.git.source_evidence import EvidenceReader
        from robert_agent.execution.application import source_models
        from robert_agent.persistence.queries import source_sessions as rounds

        beta = {**self.repo, "full_name": "org/beta", "repo_root": str(self.root / "beta")}
        self.config["repos"].append(beta)
        self.config["tapd"]["projects"][0]["repositories"].append("org/beta")
        Path(beta["repo_root"]).mkdir()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) VALUES('b',?,?,?,?,?)",
                tuple(beta.values()),
            )
        for repo in (self.repo, beta):
            (Path(repo["repo_root"]) / "README.md").write_text("Evidence\n")
        with EvidenceReader({"a": self.repo["repo_root"], "b": beta["repo_root"]}) as reader:
            evidence = tuple(reader.read(repo, "README.md") for repo in ("a", "b"))
        refs = [{"repo_id": proof.repo_id, "path": proof.path, "revision": proof.revision} for proof in evidence]
        first = self.comment("1")
        claim, revision = self.admit([first])
        picked = self._run_source_model(
            claim,
            "identify",
            "selection",
            {"kind": "selection", "repositories": ["a", "b"], "references": refs},
            evidence=evidence,
        )
        self.assertEqual(picked.status, "succeeded", picked)
        self.assertIsNone(self._accept_model(claim, "selection"))
        self._model_evidence_child(claim, revision, [first])
        with closing(database.connect(self.db)) as conn:
            self.assertFalse(source_models.synthesis_children(conn, rounds.active_round(conn, self.ticket.source_id)))
        [[revision]] = self._read("SELECT json_extract(metadata_json,'$.revision') FROM sources WHERE provider='tapd'")
        self._model_evidence_child(claim, revision, [first], repo_id="b")
        with closing(database.connect(self.db)) as conn:
            children = source_models.synthesis_children(conn, rounds.active_round(conn, self.ticket.source_id))
        self.assertEqual({child["repo_id"] for child in children}, {"a", "b"})
        output = {
            "kind": "answer",
            "answer": "See README.md for the two implementations.",
            "references": refs,
            "handled_event_fingerprints": [first.version.event_id],
        }
        incompatible = self._run_source_model(
            claim, "synthesis", "incompatible", output, evidence=evidence[:1], children=children
        )
        self.assertEqual(incompatible.status, "denied")
        result = self._run_source_model(claim, "synthesis", "synthesis", output, evidence=evidence, children=children)
        self.assertEqual(result.status, "succeeded", result)
        root = self._accept_model(claim, "synthesis")
        self.assertIsNotNone(root)
        [[payload]] = self._read("SELECT payload_json FROM source_results")
        self.assertEqual(json.loads(payload)["references"], refs)

    def test_unmapped_or_sensitive_model_prose_cannot_become_public_payload(self):
        first = self.comment("1")
        claim, _ = self.admit([first])
        for run_id, body in (
            ("file", "Can you open private.go?"),
            ("repo", "Is this about other/private?"),
            ("absolute", "Can you open /outside/private.py?"),
            ("drive", "Can you open C:/outside/private.py?"),
            ("unc", r"Can you open \\outside\private.py?"),
            ("file-url", "Can you open file:/outside/private.py?"),
            ("secret", "Would api_key = sensitive-example change the behavior?"),
        ):
            with self.subTest(run_id=run_id):
                result = self._run_source_model(
                    claim,
                    "identify",
                    run_id,
                    {
                        "kind": "question",
                        "question": body,
                        "handled_event_fingerprints": [first.version.event_id],
                    },
                )
                self.assertEqual(result.status, "succeeded", result)
                self.assertIsNone(self._accept_model(claim, run_id))
        self.assertEqual(self._read("SELECT state FROM request_events"), [("pending",)])
        self.assertEqual(self._read("SELECT count(*) FROM github_actions"), [(0,)])

    def test_model_root_refuses_tampered_packet_unproved_exit_and_lost_claim(self):
        from dataclasses import replace
        import json
        from unittest.mock import patch

        from robert_agent.persistence.stores import source_results as store

        first = self.comment("1")
        claim, _ = self.admit([first])
        outcome = self._run_source_model(
            claim,
            "identify",
            "question",
            {"kind": "question", "question": "Which module?", "handled_event_fingerprints": [first.version.event_id]},
        )
        self.assertEqual(outcome.status, "succeeded", outcome)
        self.assertIsNone(self._accept_model(replace(claim, token=claim.token + 1), "question"))
        [[raw_packet, raw_exit]] = self._read("SELECT input_packet_json,exit_json FROM source_model_runs")
        packet = json.loads(raw_packet)
        packet["context"]["evidence"] = [{"repo_id": "a", "path": "README.md", "revision": "forged", "text": "x"}]
        with closing(sqlite3.connect(self.db)) as raw, raw:
            raw.execute("UPDATE source_model_runs SET input_packet_json=?", (json.dumps(packet),))
        self.assertIsNone(self._accept_model(claim, "question"))
        exit_proof = {**json.loads(raw_exit), "owner_token": claim.token + 1}
        with closing(sqlite3.connect(self.db)) as raw, raw:
            raw.execute(
                "UPDATE source_model_runs SET input_packet_json=?,exit_json=?", (raw_packet, json.dumps(exit_proof))
            )
        self.assertIsNone(self._accept_model(claim, "question"))
        with closing(sqlite3.connect(self.db)) as raw, raw:
            raw.execute("UPDATE source_model_runs SET exit_json=?", (raw_exit,))
        from robert_agent.delivery.application.source_results import accept_model_result

        [[revision]] = self._read("SELECT json_extract(metadata_json,'$.revision') FROM sources WHERE provider='tapd'")
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            with patch.object(store, "consume_accepted_members", side_effect=ValueError("ledger changed")):
                with self.assertRaisesRegex(ValueError, "ledger changed"):
                    accept_model_result(
                        conn,
                        self.config,
                        self.ticket,
                        claim,
                        round_id="round-1",
                        model_run_id="question",
                        revision=revision,
                        now=datetime.now(timezone.utc).isoformat(),
                    )
        for table in ("source_results", "source_required_actions", "github_actions"):
            self.assertEqual(self._read(f"SELECT count(*) FROM {table}"), [(0,)])
        self.assertEqual(self._read("SELECT state FROM request_events"), [("pending",)])
        self.assertIsNotNone(self._accept_model(claim, "question"))

    def _publication_client(self, *, write_hook=None, read_hook=None):
        from robert_agent.adapters.tapd.publication import TapdPublisherIdentity
        from robert_agent.foundation.values import utc_now

        self.config["tapd"].update(executable=str(self.root / "mock-tapd"), cwd=str(self.root))
        calls, rows = [], []
        test = self

        class Client:
            def __init__(self, *, executable, cwd, environment):
                test.assertEqual((executable, cwd), (str(test.root / "mock-tapd"), str(test.root)))
                calls.append("client")

            def add_comment(self, workspace_id, kind, external_id, body, *, stop):
                test.assertEqual((workspace_id, kind, external_id), test.ticket.key[1:])
                self_target = test.ticket.source_id
                test.assertEqual(
                    test._read(
                        """SELECT intent.status, action.publish_status FROM publication_intents AS intent
                             JOIN github_actions AS action ON action.action_id=intent.action_id
                             JOIN source_required_actions AS required ON required.action_id=action.action_id
                             JOIN source_results AS root ON root.source_result_id=required.source_result_id
                             WHERE root.source_id=?""",
                        (self_target,),
                    ),
                    [("executing", "not_published")],
                )
                calls.append("write")
                rows.append(test._publication_comment(body))
                if write_hook:
                    write_hook(rows)
                return "3"

        def identity(client, ticket, *, project, stop):
            self.assertIsInstance(client, Client)
            self.assertEqual(project["workspace_id"], self.ticket.workspace_id)
            calls.append("identity")
            return TapdPublisherIdentity(
                ticket.host, ticket.workspace_id, "service-account-17", "login-proof", utc_now()
            )

        def observation(client, ticket, *, project, budget, limits, stop):
            self.assertIsInstance(client, Client)
            self.assertEqual(ticket, self.ticket)
            calls.append("read")
            snapshot = self._observation([self.comment("1"), self.comment("2"), *rows])
            return read_hook(snapshot) if read_hook else snapshot

        return (
            calls,
            rows,
            {"client_factory": Client, "observation_provider": observation, "identity_provider": identity},
        )

    def _publication_comment(
        self, body, *, identifier="3", creator="service-account-17", ticket=None, entry_type="bug", proof=True
    ):
        version = facts.CommentVersion(
            ticket or self.ticket,
            entry_type,
            identifier,
            self.now.strftime("%Y-%m-%d %H:%M:%S"),
            None,
            sha256(body.encode()).hexdigest(),
        )
        return facts.CommentObservation(
            version,
            body,
            "service-account-17",
            facts.CreatorEvidence(version, creator, "creator-proof") if proof else None,
            facts.OwnBodyEvidence(version, creator, "own-body-proof") if proof else None,
        )

    def _publish(self, root, action_id, collaborators, **kw):
        from robert_agent.delivery.application.source_publication import publish_tapd_action

        if "claim" not in kw:
            kw.setdefault("owner", "test")
        return publish_tapd_action(
            self.db, self.config, source_result_id=root, action_id=action_id, **collaborators, **kw
        )

    def test_tapd_publisher_commits_exact_worker_receipt_and_acknowledges_original_spool(self):
        import json
        from unittest.mock import patch

        from robert_agent.delivery.application import submission
        from robert_agent.execution import spool

        original_unlink = Path.unlink

        def leave_spool(path, *args, **kw):
            if path.parent != spool.directory(self.db) or path.suffix != ".json":
                return original_unlink(path, *args, **kw)
            return None

        with patch.object(Path, "unlink", new=leave_spool):
            _, task, root, action = self._accepted_answer_for_delivery()
        original = self._read("SELECT metadata_json FROM github_actions")
        calls, rows, collaborators = self._publication_client()
        result = self._publish(root, action, collaborators)
        self.assertEqual((result.status, result.external_id), ("published", "3"), result)
        self.assertEqual(calls, ["client", "identity", "read", "write", "read"])
        [[prepared]] = self._read("SELECT prepared_evidence_json FROM tapd_publication_preparations")
        [[remote, status]] = self._read("SELECT remote_evidence_json,status FROM publication_intents")
        evidence, receipt = json.loads(prepared), json.loads(remote)["receipt"]
        self.assertEqual(status, "published")
        self.assertEqual(evidence["publisher_identity_evidence_id"], "login-proof")
        self.assertEqual((evidence["bot_name"], evidence["reply_name"]), ("helper-team", "service-desk"))
        self.assertEqual(evidence["target"]["host"], "tapd.example")
        self.assertEqual(evidence["target"]["workspace_id"], "001")
        self.assertEqual(evidence["body"], rows[0].body_html)
        self.assertIn("<p>service-desk：</p>", evidence["body"])
        self.assertIn("Repository findings", evidence["body"])
        self.assertEqual(receipt["identity_evidence_id"], "creator-proof")
        self.assertEqual(receipt["own_body_evidence_id"], "own-body-proof")
        self.assertEqual(receipt["body_digest"], evidence["body_digest"])
        self.assertEqual(self._read("SELECT metadata_json FROM github_actions"), original)
        self.assertEqual(self._read("SELECT external_id,publish_status FROM github_actions"), [("3", "published")])
        self.assertEqual(
            submission.replay_pending_results(self.db, attempt_id=task["attempt_id"]),
            {"recorded": 1, "deferred": 0, "rejected": 0},
        )
        repeated = self._publish(root, action, {})
        self.assertEqual((repeated.status, repeated.external_id), ("deduplicated", "3"))
        self.assertEqual(self._read("SELECT status FROM source_rounds"), [("execution_finished",)])
        self.assertIsNotNone(self._read("SELECT completed_at FROM work_items")[0][0])
        self.assertEqual(
            self._read("SELECT event_type FROM work_item_events WHERE event_type='completed'"),
            [("completed",)],
        )
        self.assertEqual(calls.count("write"), 1)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE publication_intents SET remote_evidence_json=json_remove(remote_evidence_json,?)",
                ("$.receipt.own_body_evidence_id",),
            )
        unproved = self._publish(root, action, collaborators)
        self.assertEqual((unproved.status, unproved.reason), ("unknown", "published_receipt_unverified"))
        self.assertEqual(calls.count("write"), 1)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE tapd_publication_preparations SET prepared_evidence_json="
                "json_remove(prepared_evidence_json,'$.publisher_identity_evidence_id','$.publisher_observed_at')"
            )
        still_unproved = self._publish(root, action, collaborators)
        self.assertEqual((still_unproved.status, still_unproved.reason), ("unknown", "published_receipt_unverified"))
        self.assertEqual(calls.count("write"), 1)

    def test_tapd_publisher_model_question_requires_separate_scoped_identity(self):
        from dataclasses import replace

        from robert_agent.delivery.application.source_publication import publish_tapd_action

        first, second = self.comment("1"), self.comment("2")
        claim, _ = self.admit([first, second])
        outcome = self._run_source_model(
            claim,
            "identify",
            "publication-question",
            {
                "kind": "question",
                "question": "Which behavior should I investigate?",
                "handled_event_fingerprints": [first.version.event_id],
            },
        )
        self.assertEqual(outcome.status, "succeeded", outcome)
        root = self._accept_model(claim, "publication-question")
        [[action]] = self._read("SELECT action_id FROM github_actions")
        calls, rows, collaborators = self._publication_client()
        for unavailable in (None, False, {"tapd": None}):
            no_config = publish_tapd_action(
                self.root / "missing-db",
                unavailable,
                source_result_id=root,
                action_id=action,
                owner="test",
                **collaborators,
            )
            self.assertEqual((no_config.status, no_config.reason), ("deferred", "tapd_disabled"))
        self.assertFalse((self.root / "missing-db").exists())
        scope = self.config["tapd"]
        scope["enabled"] = False
        self.assertEqual(self._publish(root, action, collaborators).reason, "tapd_disabled")
        scope.update(enabled=True, host="different.example")
        self.assertEqual(self._publish(root, action, collaborators).reason, "tapd_scope_unavailable")
        scope["host"] = self.ticket.host
        self.assertEqual(self._publish(root, action, {}).status, "deferred")
        self.assertEqual(calls, [])
        identity = collaborators["identity_provider"]

        def wrong_project(*args, **kwargs):
            return replace(identity(*args, **kwargs), workspace_id="999")

        bad = self._publish(root, action, {**collaborators, "identity_provider": wrong_project})
        self.assertEqual((bad.status, bad.reason), ("deferred", "publisher_identity_unavailable"))
        self.assertEqual(self._read("SELECT status FROM publication_intents"), [])
        result = self._publish(root, action, collaborators)
        self.assertEqual(result.status, "published", result)
        self.assertIn("Which behavior should I investigate?", rows[0].body_html)
        self.assertNotIn("@helper-team", rows[0].body_html)
        self.assertEqual(self._read("SELECT COUNT(*) FROM tasks"), [(0,)])
        self.assertEqual(self._read("SELECT status FROM source_rounds"), [("waiting_for_user",)])
        self.assertEqual(self._read("SELECT count(*) FROM work_items"), [(0,)])
        self.assertEqual(calls.count("write"), 1)

    def _expire_publication_claim(self, claim):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE source_sessions SET lease_until='2000-01-01T00:00:00+00:00' WHERE source_id=? AND token=?",
                (claim.source_id, claim.token),
            )

    def test_tapd_publisher_timeout_reconciles_under_new_owner_after_revocation(self):
        from robert_agent.adapters.tapd.client import TapdError
        from robert_agent.foundation.values import utc_now
        from robert_agent.work.application import messaging
        from robert_agent.work.application.source_observation import publication_facts
        from robert_agent.work.errors import WorkItemNotFoundError, WorkItemValidationError

        claim, _, root, action = self._accepted_answer_for_delivery()

        def timeout(_rows):
            raise TapdError("timeout", effect_unknown=True)

        calls, rows, collaborators = self._publication_client(write_hook=timeout)
        first = self._publish(root, action, collaborators, claim=claim)
        self.assertEqual((first.status, first.reason), ("unknown", "write_outcome_unknown"))
        self.assertEqual(self._read("SELECT status FROM publication_intents"), [("attention",)])
        [open_notice] = self._read(
            "SELECT notification_id,status FROM notifications WHERE notification_type='source_publication_attention'"
        )
        self.assertEqual(open_notice[1], "recorded")
        actor = self._question_message_identity()
        self.assertEqual(messaging.inbox(self.config, actor)["messages"], [])
        self.config["messaging"]["repos"] = ["org/alpha"]
        [entry] = messaging.inbox(self.config, actor)["messages"]
        self.assertEqual(entry["notification_id"], open_notice[0])
        self.assertEqual(entry["target"]["publication_provider"], "tapd")
        self.assertEqual((entry["target"]["repo_id"], entry["actions"]), ("a", ["ack"]))
        response = {**actor, "notification_id": open_notice[0], "version": entry["version"], "action": "ack"}
        with self.assertRaises(WorkItemNotFoundError):
            messaging.respond(self.config, {**response, "sender_id": "43"})
        with self.assertRaises(WorkItemValidationError):
            messaging.respond(self.config, {**response, "action": "retry_publish"})
        self.assertEqual(messaging.respond(self.config, response)["status"], "acknowledged")
        self.assertEqual(self._read("SELECT status FROM publication_intents"), [("attention",)])
        self.assertEqual(self._read("SELECT resolved_at FROM source_publication_attentions"), [(None,)])
        self.assertEqual(len(messaging.inbox(self.config, actor)["messages"]), 1)
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            revoked = self._observation([self.comment("1", mention=False), self.comment("2")])
            tapd_intake.observe_ticket(
                conn, self.config["tapd"], revoked, publication_facts(conn, self.ticket), now=revoked.observed_at
            )
        self._expire_publication_claim(claim)
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            self.assertIsNotNone(
                source_sessions.claim(
                    conn, source_id=self.ticket.source_id, owner="next", now=utc_now(), lease_seconds=300
                )
            )
        self.config["repos"] = []
        self.config["tapd"].update(bot_name="replacement-bot", reply_name="replacement-reply")
        stale = self._publish(root, action, collaborators, claim=claim)
        self.assertEqual((stale.status, stale.reason), ("unknown", "source_fence_unavailable"))
        self.assertEqual(self._read("SELECT status FROM publication_intents"), [("attention",)])
        original = rows[0]
        rows[:] = [self._publication_comment(original.body_html, creator="imposter")]
        reader = {**collaborators, "identity_provider": None}
        rejected = self._publish(root, action, reader, owner="next")
        self.assertEqual(rejected.status, "unknown")
        self.assertEqual(self._read("SELECT status FROM publication_intents"), [("attention",)])
        self.assertEqual(
            self._read(
                "SELECT notification_id,status FROM notifications WHERE notification_type='source_publication_attention'"
            ),
            [open_notice],
        )
        rows[:] = [original]
        recovered = self._publish(root, action, reader, owner="next")
        self.assertEqual((recovered.status, recovered.external_id), ("published", "3"), recovered)
        self.assertEqual(calls.count("identity"), 1)
        self.assertEqual(calls.count("write"), 1)
        self.assertEqual(self._read("SELECT status FROM publication_intents"), [("published",)])
        self.assertEqual(
            self._read(
                "SELECT notification_id,status FROM notifications WHERE notification_type='source_publication_attention'"
            ),
            [(open_notice[0], "resolved")],
        )
        self.assertEqual(
            self._read(
                "SELECT json_extract(metadata_json,'$.operator_response.action') FROM notifications WHERE notification_id=?",
                (open_notice[0],),
            ),
            [("ack",)],
        )

    def test_unknown_control_notice_has_real_project_scope_and_reconciles_without_a_round(self):
        from dataclasses import replace

        from robert_agent.adapters.tapd.client import TapdError
        from robert_agent.delivery.application.source_results import accept_control_notice
        from robert_agent.work.application import messaging

        project = self.config["tapd"]["projects"][0]
        project["repositories"] = ["org/alpha", "org/beta"]
        project["rules"] = [
            {"id": key, "assignee": "owner", "label": key, "repo": f"org/{repo}", "kinds": ["bug"]}
            for key, repo in (("a", "alpha"), ("b", "beta"))
        ]
        self.item = replace(self.item, owners=("owner",), labels=("a", "b"))
        snapshot = self._observation([])
        now = snapshot.observed_at
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            current = tapd_intake.observe_ticket(conn, self.config["tapd"], snapshot, self.publication, now=now)
            claim = source_sessions.claim(
                conn, source_id=self.ticket.source_id, owner="test", now=now, lease_seconds=3600
            )
            root = accept_control_notice(conn, self.config, self.ticket, claim, revision=current.revision, now=now)
        self.assertIsNotNone(root)
        [[action]] = self._read("SELECT action_id FROM github_actions WHERE source_result_id=?", (root,))

        def timeout(_rows):
            raise TapdError("private raw write diagnostic", effect_unknown=True)

        calls, _, collaborators = self._publication_client(write_hook=timeout)
        outcome = self._publish(root, action, collaborators, claim=claim)
        self.assertEqual((outcome.status, outcome.reason), ("unknown", "write_outcome_unknown"), outcome)
        actor = self._question_message_identity()
        [entry] = messaging.inbox(self.config, actor)["messages"]
        self.assertEqual(entry["target"]["source_result_id"], root)
        self.assertNotIn("repo_id", entry["target"])
        self.assertNotIn("round_id", entry["target"])
        self.assertNotIn("private raw write diagnostic", str(entry))
        for table in ("source_rounds", "tasks", "work_items"):
            self.assertEqual(self._read(f"SELECT COUNT(*) FROM {table}"), [(0,)])
        self.assertEqual(self._publish(root, action, collaborators, claim=claim).status, "published")
        self.assertEqual(calls.count("write"), 1)
        self.assertEqual(messaging.inbox(self.config, actor)["messages"], [])
        self.assertEqual(
            self._read("SELECT status FROM notifications WHERE notification_id=?", (entry["notification_id"],)),
            [("resolved",)],
        )

    def test_unknown_intent_outbox_failure_rolls_back_then_pacing_recovers_exact_owner(self):
        from dataclasses import replace
        from unittest.mock import patch

        from robert_agent.adapters.tapd.client import TapdError
        from robert_agent.foundation.values import utc_now
        from robert_agent.persistence.stores import source_attention
        from robert_agent.work import source_attention as attention

        claim, root, action = self._model_question_for_publication()

        def timeout(_rows):
            raise TapdError("simulated", effect_unknown=True)

        calls, _, collaborators = self._publication_client(write_hook=timeout)
        save = source_attention.open_publication

        def fail_after_outbox(conn, **options):
            save(conn, **options)
            raise ValueError("simulated outbox failure")

        with (
            patch.object(source_attention, "open_publication", side_effect=fail_after_outbox),
            self.assertRaisesRegex(ValueError, "simulated outbox failure"),
        ):
            self._publish(root, action, collaborators, claim=claim)
        self.assertEqual(self._read("SELECT status FROM publication_intents"), [("executing",)])
        self.assertEqual(self._read("SELECT COUNT(*) FROM source_publication_attentions"), [(0,)])
        self.assertEqual(self._read("SELECT COUNT(*) FROM notifications"), [(0,)])
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            for owner, root_id in ((replace(claim, token=claim.token + 1), root), (claim, "wrong-root")):
                attention.observe_publication_unknown(
                    conn,
                    owner,
                    root_id=root_id,
                    action_id=action,
                    provider="tapd",
                    reason="readback_not_confirmed",
                    now=utc_now(),
                )
        self.assertEqual(self._read("SELECT COUNT(*) FROM source_publication_attentions"), [(0,)])
        [[prepared]] = self._read("SELECT prepared_evidence_json FROM tapd_publication_preparations")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE tapd_publication_preparations SET prepared_evidence_json='{}'")
        outcome = self._publish(root, action, collaborators, claim=claim, respect_backoff=True)
        self.assertEqual((outcome.status, outcome.reason), ("unknown", "prepared_scope_unavailable"), outcome)
        self.assertEqual(
            self._read("SELECT cause,version FROM source_publication_attentions"), [("prepared_scope_unavailable", 1)]
        )
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE tapd_publication_preparations SET prepared_evidence_json=?", (prepared,))
        self.assertEqual(self._publish(root, action, collaborators, claim=claim).status, "published")
        self.assertEqual(calls.count("write"), 1)
        self.assertEqual(
            self._read("SELECT status FROM notifications WHERE notification_type='source_publication_attention'"),
            [("resolved",)],
        )
        [[receipt]] = self._read("SELECT remote_evidence_json FROM publication_intents")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE publication_intents SET remote_evidence_json='{}'")
        for _ in range(2):
            outcome = self._publish(root, action, collaborators, claim=claim)
            self.assertEqual((outcome.status, outcome.reason), ("unknown", "published_receipt_unverified"))
        self.assertEqual(
            self._read("SELECT cause,version,resolved_at IS NULL FROM source_publication_attentions ORDER BY version"),
            [("prepared_scope_unavailable", 1, 0), ("published_receipt_unverified", 2, 1)],
        )
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE publication_intents SET remote_evidence_json=?", (receipt,))
        self.assertEqual(self._publish(root, action, collaborators, claim=claim).status, "deduplicated")
        self.assertEqual(
            self._read("SELECT COUNT(*) FROM source_publication_attentions WHERE resolved_at IS NULL"), [(0,)]
        )
        self.assertEqual(calls.count("write"), 1)

    def test_tapd_publisher_ack_without_scoped_unique_independent_readback_stays_unknown(self):
        from dataclasses import replace

        claim, _, root, action = self._accepted_answer_for_delivery()
        variant = "missing"

        def readback(snapshot):
            if len(snapshot.comments) == 2:
                return snapshot
            trigger, unrelated, actual = snapshot.comments
            body = actual.body_html
            alternatives = {
                "missing": (),
                "display_only": (self._publication_comment(body, proof=False),),
                "wrong_creator": (self._publication_comment(body, creator="different-creator"),),
                "wrong_own_body": (replace(actual, own_body=unrelated.own_body),),
                "wrong_target": (self._publication_comment(body, ticket=replace(self.ticket, external_id="999")),),
                "wrong_type": (self._publication_comment(body, entry_type="bug_remark"),),
                "wrong_body": (self._publication_comment(body.replace("Repository findings", "Other findings")),),
                "receipt_duplicate": (actual, self._publication_comment(body, identifier="4", proof=False)),
                "repeated_member": (actual, actual),
                "incomplete": (actual,),
            }
            return replace(
                snapshot,
                comments=(trigger, unrelated, *alternatives[variant]),
                coverage="incomplete" if variant == "incomplete" else "complete",
            )

        calls, _, collaborators = self._publication_client(read_hook=readback)
        for variant in (
            "missing",
            "display_only",
            "wrong_creator",
            "wrong_own_body",
            "wrong_target",
            "wrong_type",
            "wrong_body",
            "receipt_duplicate",
            "repeated_member",
            "incomplete",
        ):
            with self.subTest(variant=variant):
                result = self._publish(root, action, collaborators, claim=claim)
                self.assertEqual((result.status, result.external_id), ("unknown", None), result)
                self.assertEqual(self._read("SELECT status FROM publication_intents"), [("attention",)])
                self.assertEqual(
                    self._read("SELECT external_id,publish_status FROM github_actions"), [(None, "not_published")]
                )
        self.assertEqual(calls.count("write"), 1)

    def test_tapd_publisher_fence_loss_after_effect_cannot_finalize_old_owner(self):
        from robert_agent.foundation.values import utc_now

        claim, _, root, action = self._accepted_answer_for_delivery()

        def lose_fence(_rows):
            self._expire_publication_claim(claim)
            with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
                transactions.begin_immediate(conn)
                self.assertIsNotNone(
                    source_sessions.claim(
                        conn, source_id=self.ticket.source_id, owner="next", now=utc_now(), lease_seconds=300
                    )
                )

        calls, _, collaborators = self._publication_client(write_hook=lose_fence)
        result = self._publish(root, action, collaborators, claim=claim)
        self.assertEqual((result.status, result.reason), ("unknown", "source_fence_or_scope_lost"))
        self.assertEqual(self._read("SELECT status FROM publication_intents"), [("executing",)])
        self.assertEqual(self._read("SELECT external_id,publish_status FROM github_actions"), [(None, "not_published")])
        self.assertEqual(self._publish(root, action, collaborators, owner="next").status, "published")
        self.assertEqual(calls.count("write"), 1)

    def test_tapd_publisher_cannot_overwrite_revocation_committed_during_refresh(self):
        from robert_agent.work.application.source_observation import publication_facts

        claim, _, root, action = self._accepted_answer_for_delivery()

        def newer_revocation(earlier):
            with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
                transactions.begin_immediate(conn)
                later = self._observation([self.comment("1", mention=False), self.comment("2")])
                self.assertGreater(later.observed_at, earlier.observed_at)
                tapd_intake.observe_ticket(
                    conn, self.config["tapd"], later, publication_facts(conn, self.ticket), now=later.observed_at
                )
            return earlier

        calls, _, collaborators = self._publication_client(read_hook=newer_revocation)
        result = self._publish(root, action, collaborators, claim=claim)
        self.assertEqual((result.status, result.reason), ("deferred", "source_grant_unavailable"))
        self.assertEqual(calls, ["client", "identity", "read"])
        self.assertEqual(self._read("SELECT status FROM publication_intents"), [])

    def test_tapd_publisher_requires_predecessor_receipt_and_idle_historical_rounds(self):
        claim, task, root, action = self._accepted_answer_for_delivery()
        calls, _, collaborators = self._publication_client()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                """INSERT INTO source_rounds(round_id,source_id,status,capability,repo_ids_json,publication_scope_json,
                          input_version,input_snapshot_json,input_sha256,created_at,finished_at)
                   SELECT 'historic',source_id,'execution_finished',capability,repo_ids_json,publication_scope_json,
                          input_version,input_snapshot_json,input_sha256,created_at,created_at
                   FROM source_rounds WHERE round_id='round-1'"""
            )
            conn.execute(
                """INSERT INTO source_round_requests(round_id,request_id,content_version,content_snapshot_json)
                   SELECT 'historic',request_id,content_version,content_snapshot_json FROM source_round_requests
                   WHERE round_id='round-1'"""
            )
            conn.execute(
                """INSERT INTO tasks(task_id,workstream_id,task_kind,lifecycle,created_at,updated_at)
                   SELECT 'revived',workstream_id,task_kind,'completed',created_at,updated_at
                   FROM tasks WHERE task_id=?""",
                (task["task_id"],),
            )
            conn.execute(
                """INSERT INTO source_round_tasks(task_id,round_id,request_id,repo_id,capability,created_at)
                   SELECT 'revived','historic',request_id,repo_id,capability,created_at FROM source_round_tasks
                   WHERE task_id=?""",
                (task["task_id"],),
            )
            conn.execute(
                """INSERT INTO attempts(attempt_id,task_id,attempt_no,status,metadata_json)
                   VALUES('revived','revived',1,'canceled','{"dispatch":{"pid":123}}')"""
            )
        blocked = self._publish(root, action, collaborators, claim=claim)
        self.assertEqual((blocked.status, blocked.reason), ("deferred", "source_busy"), blocked)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE attempts SET metadata_json='{}' WHERE attempt_id='revived'")
            conn.execute(
                """INSERT INTO github_actions(action_id,result_id,task_id,action_type,provider,audit_status,publish_status,created_at)
                   SELECT 'preceding',result_id,task_id,'open_pr','github','accepted','published',created_at
                   FROM github_actions WHERE action_id=?""",
                (action,),
            )
            conn.execute(
                """INSERT INTO publication_intents(publication_id,action_id,idempotency_key,request_fingerprint,
                          status,created_at,updated_at)
                   VALUES('predecessor','preceding','key','fingerprint','published','now','now')"""
            )
            conn.execute("UPDATE source_required_actions SET position=1 WHERE action_id=?", (action,))
            conn.execute(
                "INSERT INTO source_required_actions(source_result_id,action_id,position) VALUES(?,'preceding',0)",
                (root,),
            )
        blocked = self._publish(root, action, collaborators, claim=claim)
        self.assertEqual((blocked.status, blocked.reason), ("deferred", "predecessor_receipt_unavailable"), blocked)
        self.assertNotIn("write", calls)
        self.assertEqual(self._read("SELECT status FROM publication_intents"), [("published",)])

    def _complete_delivery(self, claim, root, *, config=None):
        from robert_agent.delivery.application.source_completion import complete_source_result
        from robert_agent.foundation.values import utc_now

        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            return complete_source_result(
                conn, config if config is not None else self.config, claim, source_result_id=root, now=utc_now()
            )

    def test_confirmed_effect_restores_round_after_restart_and_reactivates_same_work_item(self):
        from unittest.mock import patch

        from robert_agent.delivery.application.source_completion import CompletionOutcome
        from robert_agent.foundation.values import utc_now
        from robert_agent.persistence.queries import source_results
        from robert_agent.readmodels import board
        from robert_agent.work.application.source_observation import publication_facts

        claim, task, root, action = self._accepted_answer_for_delivery()
        calls, rows, collaborators = self._publication_client()
        with patch(
            "robert_agent.delivery.application.source_completion.complete_source_result",
            return_value=CompletionOutcome("held", "simulated_older_controller"),
        ):
            published = self._publish(root, action, collaborators, claim=claim)
        self.assertEqual((published.status, published.completion.status), ("published", "held"))
        self.assertEqual(self._read("SELECT status FROM source_rounds"), [("active",)])
        [[item_id]] = self._read("SELECT work_item_id FROM work_items")
        with closing(database.connect(self.db)) as conn:
            args = {"scopes": [(self.ticket.host, self.ticket.workspace_id)], "now": utc_now(), "limit": 1}
            self.assertEqual(source_results.completion_candidates(conn, owner="foreign", **args), ())
            found = source_results.completion_candidates(conn, owner="test", **args)
            self.assertEqual([row["source_result_id"] for row in found], [root])
            self.assertFalse(
                source_results.completion_candidates(
                    conn,
                    owner="test",
                    after=(found[0]["accepted_at"], root),
                    **args,
                )
            )
            self.assertFalse(source_results.completion_candidates(conn, owner="test", **{**args, "scopes": []}))
        from robert_agent.persistence.stores import source_coordination

        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            now = utc_now()
            self.assertTrue(
                source_coordination.save(
                    conn,
                    claim,
                    now=now,
                    next_due_at="2099-01-01T00:00:00+00:00",
                    status="held",
                    round_id="round-1",
                )
            )
            self.assertFalse(source_results.completion_candidates(conn, owner="test", **{**args, "now": now}))

        self._expire_publication_claim(claim)
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            next_owner = source_sessions.claim(
                conn, source_id=self.ticket.source_id, owner="next", now=utc_now(), lease_seconds=300
            )
        self.assertIsNotNone(next_owner)
        self.assertEqual(self._complete_delivery(claim, root).reason, "source_fence_unavailable")
        self.assertEqual(self._complete_delivery(next_owner, root).status, "completed")
        self.assertEqual(board.get_work_item_detail(self.db, item_id)["column"], "done")
        self.assertEqual(self._publish(root, action, {}, claim=next_owner).completion.status, "already_completed")
        self.assertEqual(calls.count("write"), 1)
        next_snapshot = self._observation([self.comment("1"), self.comment("2"), self.comment("4"), *rows])
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            next_observation = tapd_intake.observe_ticket(
                conn,
                self.config["tapd"],
                next_snapshot,
                publication_facts(conn, self.ticket),
                now=next_snapshot.observed_at,
            )
            next_round = tapd_intake.admit_round(
                conn,
                self.config["tapd"],
                self.ticket,
                next_owner,
                round_id="round-2",
                revision=next_observation.revision,
                now=next_snapshot.observed_at,
            )
        self.assertIsNotNone(next_round)
        second = self._prepare(next_owner, next_observation.revision, round_id="round-2", role="answer")
        self.assertIsNotNone(second)
        self.assertNotEqual(second["task_id"], task["task_id"])
        self.assertEqual(self._read("SELECT work_item_id,completed_at FROM work_items"), [(item_id, None)])
        self.assertEqual(board.get_work_item_detail(self.db, item_id)["column"], "todo")
        self.assertEqual(self._complete_delivery(next_owner, root).status, "already_completed")
        self.assertEqual(self._read("SELECT status FROM source_rounds WHERE round_id='round-2'"), [("active",)])
        self.assertEqual(self._read("SELECT completed_at FROM work_items"), [(None,)])
        self.assertEqual(self._read("SELECT count(*) FROM work_item_events WHERE event_type='completed'"), [(1,)])

    def test_readback_revocation_preserves_receipt_without_successful_completion(self):
        from dataclasses import replace

        claim, _, root, action = self._accepted_answer_for_delivery()

        def revoke_in_readback(snapshot):
            if len(snapshot.comments) == 3:
                return replace(snapshot, comments=(self.comment("1", mention=False), *snapshot.comments[1:]))
            return snapshot

        calls, _, collaborators = self._publication_client(read_hook=revoke_in_readback)
        result = self._publish(root, action, collaborators, claim=claim)
        self.assertEqual((result.status, result.external_id), ("published", "3"), result)
        self.assertEqual(result.completion.reason, "source_round_canceled_or_inactive")
        self.assertEqual(calls.count("write"), 1)
        self.assertEqual(self._read("SELECT status FROM publication_intents"), [("published",)])
        self.assertEqual(self._read("SELECT status FROM source_rounds"), [("active",)])
        self.assertEqual(self._read("SELECT reason FROM source_round_cancellations"), [("request_revoked",)])
        self.assertEqual(self._read("SELECT completed_at FROM work_items"), [(None,)])
        self.assertEqual(
            self._read("SELECT state FROM request_events WHERE source_result_id=?", (root,)), [("consumed",)]
        )
        self.assertEqual(self._complete_delivery(claim, root).status, "held")

    def test_readback_persistence_conflict_invalidates_cached_authority_after_confirmed_receipt(self):
        from dataclasses import replace

        claim, _, root, action = self._accepted_answer_for_delivery()

        def conflicting_readback(snapshot):
            if len(snapshot.comments) == 3:
                absence = facts.CommentAbsence(self.ticket, "bug", "1", "provider-deletion")
                return replace(snapshot, absences=(absence,))
            return snapshot

        calls, _, collaborators = self._publication_client(read_hook=conflicting_readback)
        result = self._publish(root, action, collaborators, claim=claim)
        self.assertEqual(
            (result.status, result.completion.reason), ("published", "source_readback_unavailable"), result
        )
        self.assertEqual(self._read("SELECT status FROM publication_intents"), [("published",)])
        self.assertEqual(self._read("SELECT json_extract(metadata_json,'$.revision') FROM sources"), [(None,)])
        self.assertEqual(self._complete_delivery(claim, root).reason, "source_delivery_scope_changed")
        self.assertEqual(self._read("SELECT completed_at FROM work_items"), [(None,)])
        self.assertEqual(calls.count("write"), 1)

    def test_worker_question_projects_waiting_on_original_item_with_open_pr(self):
        from unittest.mock import patch

        from robert_agent.persistence.stores import source_attention
        from robert_agent.readmodels import board
        from robert_agent.work.application import messaging

        first, second = self.comment("1"), self.comment("2")
        claim, revision = self.admit([first, second])
        task = self._prepare(claim, revision, role="question")
        calls, outcomes = self._dispatch(task, refresh=self._refresh(claim, [first, second]), spawn=self._started)
        self.assertEqual(calls, 1, outcomes)
        # Model a preexisting linked PR workstream for the completed Worker.
        # Publication and WorkItem ownership still belong to the TAPD origin.
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                """INSERT INTO sources(source_id,source_key,provider,host,project_key,source_type,
                          external_id,repo_id,number,state)
                   VALUES('linked-pr','github:org/alpha!42','github','github.com','org/alpha','pull_request',
                          '42','a',42,'open')"""
            )
            conn.execute(
                """INSERT INTO workstreams(workstream_id,repo_id,primary_source_id,origin_workstream_id,
                          lifecycle,active_task_id,created_at,updated_at)
                   VALUES('derived','a','linked-pr',?,'active',?,'now','now')""",
                (task["workstream_id"], task["task_id"]),
            )
            conn.execute(
                """INSERT INTO workstream_sources(workstream_id,source_id,relationship,created_at)
                   VALUES(?,'linked-pr','derived_pr','now')""",
                (task["workstream_id"],),
            )
            conn.execute("UPDATE tasks SET workstream_id='derived' WHERE task_id=?", (task["task_id"],))
            conn.execute("UPDATE workstreams SET active_task_id=NULL WHERE workstream_id=?", (task["workstream_id"],))
        receipt = self._submit(
            task,
            [first],
            output="waiting_for_user",
            extra={
                "planned_actions": [
                    {"provider": "tapd", "type": "comment", "target_id": "ticket", "body": "Which module?"}
                ],
                "operator_question": {
                    "kind": "clarification",
                    "summary": "Which module?",
                    "choices": [{"id": "a", "label": "Alpha"}, {"id": "b", "label": "Beta"}],
                },
            },
        )
        self.assertEqual(receipt["status"], "recorded", receipt)
        self._stop(task)
        self.assertEqual(self._audit(), 1)
        [[latest]] = self._read("SELECT json_extract(metadata_json,'$.revision') FROM sources WHERE provider='tapd'")
        root = self._accept_root(claim, receipt["result_id"], latest)
        self.assertIsNotNone(root)
        [[action]] = self._read("SELECT action_id FROM github_actions")
        _, _, collaborators = self._publication_client()
        record = source_attention.open_question

        def fail_after_outbox(conn, **values):
            record(conn, **values)
            raise ValueError("injected completion fault")

        stream_before = self._read("SELECT lifecycle FROM workstreams ORDER BY workstream_id")
        with patch.object(source_attention, "open_question", side_effect=fail_after_outbox):
            publication = self._publish(root, action, collaborators, claim=claim)
        self.assertEqual(
            (publication.status, publication.completion.reason), ("published", "source_completion_inconsistent")
        )
        self.assertEqual(self._read("SELECT status FROM source_rounds"), [("active",)])
        self.assertEqual(self._read("SELECT lifecycle FROM workstreams ORDER BY workstream_id"), stream_before)
        self.assertEqual(self._read("SELECT event_type FROM work_item_events"), [("tapd_intake",)])
        self.assertEqual(self._read("SELECT COUNT(*) FROM source_attentions"), [(0,)])
        self.assertEqual(self._read("SELECT COUNT(*) FROM source_question_attentions"), [(0,)])
        self.assertEqual(
            self._read("SELECT COUNT(*) FROM notifications WHERE notification_type='source_ordinary_question'"), [(0,)]
        )
        [[attempt_metadata]] = self._read(
            "SELECT metadata_json FROM attempts WHERE attempt_id=?", (task["attempt_id"],)
        )
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE attempts SET metadata_json=json_remove(metadata_json,'$.process_stopped_at') WHERE attempt_id=?",
                (task["attempt_id"],),
            )
        self.assertEqual(self._complete_delivery(claim, root).reason, "source_busy")
        self.assertEqual(self._read("SELECT COUNT(*) FROM source_question_attentions"), [(0,)])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE attempts SET metadata_json=? WHERE attempt_id=?", (attempt_metadata, task["attempt_id"])
            )
        self.assertEqual(self._complete_delivery(claim, root).reason, "waiting_for_user")
        self.assertEqual(self._complete_delivery(claim, root).status, "already_completed")
        self.assertEqual(
            self._read("SELECT kind,repo_id,task_id FROM source_attentions"),
            [("ordinary_question", "a", task["task_id"])],
        )
        [[item_id, completed_at]] = self._read("SELECT work_item_id,completed_at FROM work_items")
        self.assertIsNone(completed_at)
        card = board.get_work_item_detail(self.db, item_id)
        self.assertEqual((card["column"], card["reason_code"]), ("waiting", "waiting_for_user"))
        self.assertEqual(card["pr"]["number"], 42)
        self.assertEqual(self._read("SELECT lifecycle FROM workstreams WHERE workstream_id='derived'"), [("active",)])
        self.assertEqual(self._read("SELECT lifecycle FROM tasks"), [("completed",)])
        self.assertEqual(
            self._read("SELECT event_type FROM work_item_events ORDER BY created_at"),
            [
                ("tapd_intake",),
                ("tapd_question_delivered",),
            ],
        )

        identity = self._question_message_identity()
        self.assertEqual(messaging.inbox(self.config, identity)["messages"], [])
        self.config["messaging"]["repos"] = ["org/alpha"]
        [entry] = messaging.inbox(self.config, identity)["messages"]
        self.assertEqual(
            (entry["summary"], entry["target"]["repo_id"], entry["actions"]), ("Which module?", "a", ["ack"])
        )

    def test_completion_rejects_false_partial_foreign_and_out_of_order_receipts(self):
        from unittest.mock import patch

        from robert_agent.delivery.application.source_completion import CompletionOutcome

        claim, _, root, action = self._accepted_answer_for_delivery()
        _, _, collaborators = self._publication_client()
        with patch(
            "robert_agent.delivery.application.source_completion.complete_source_result",
            return_value=CompletionOutcome("held", "simulated_older_controller"),
        ):
            self.assertEqual(self._publish(root, action, collaborators, claim=claim).status, "published")
        [[remote]] = self._read("SELECT remote_evidence_json FROM publication_intents")
        [[intent_finished_at]] = self._read("SELECT finished_at FROM publication_intents")
        mutations = (
            ("UPDATE publication_intents SET remote_evidence_json='{}'", ()),
            (
                "UPDATE publication_intents SET remote_evidence_json=json_remove(?, '$.receipt.own_body_evidence_id')",
                (remote,),
            ),
            (
                "UPDATE publication_intents SET remote_evidence_json=json_set(?, '$.receipt.target.host', 'foreign.example')",
                (remote,),
            ),
            ("UPDATE publication_intents SET finished_at='2000-01-01T00:00:00+00:00'", ()),
            ("UPDATE source_required_actions SET position=1", ()),
            ("UPDATE github_actions SET publish_status='skipped'", ()),
        )
        for query, params in mutations:
            with self.subTest(query=query):
                with closing(sqlite3.connect(self.db)) as conn, conn:
                    conn.execute(
                        "UPDATE publication_intents SET remote_evidence_json=?,finished_at=?",
                        (remote, intent_finished_at),
                    )
                    conn.execute("UPDATE source_required_actions SET position=0")
                    conn.execute("UPDATE github_actions SET publish_status='published'")
                    conn.execute(query, params)
                result = self._complete_delivery(claim, root)
                self.assertEqual((result.status, result.reason), ("held", "required_receipts_unavailable"), result)
        self.assertEqual(self._read("SELECT status FROM source_rounds"), [("active",)])
        self.assertEqual(self._read("SELECT completed_at FROM work_items"), [(None,)])

    def test_confirmed_receipt_survives_revived_historical_worker_and_model_during_effect(self):
        claim, task, root, action = self._accepted_answer_for_delivery()

        def revive(_rows):
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute(
                    """INSERT INTO source_rounds(round_id,source_id,status,capability,repo_ids_json,publication_scope_json,
                              input_version,input_snapshot_json,input_sha256,created_at,finished_at)
                       SELECT 'historic',source_id,'execution_finished',capability,repo_ids_json,publication_scope_json,
                              input_version,input_snapshot_json,input_sha256,created_at,created_at
                       FROM source_rounds WHERE round_id='round-1'"""
                )
                conn.execute(
                    """INSERT INTO source_round_requests(round_id,request_id,content_version,content_snapshot_json)
                       SELECT 'historic',request_id,content_version,content_snapshot_json FROM source_round_requests
                       WHERE round_id='round-1'"""
                )
                conn.execute(
                    """INSERT INTO tasks(task_id,workstream_id,task_kind,lifecycle,created_at,updated_at)
                       SELECT 'revived',workstream_id,task_kind,'completed',created_at,updated_at
                       FROM tasks WHERE task_id=?""",
                    (task["task_id"],),
                )
                conn.execute(
                    """INSERT INTO source_round_tasks(task_id,round_id,request_id,repo_id,capability,created_at)
                       SELECT 'revived','historic',request_id,repo_id,capability,created_at FROM source_round_tasks
                       WHERE task_id=?""",
                    (task["task_id"],),
                )
                conn.execute(
                    """INSERT INTO attempts(attempt_id,task_id,attempt_no,status,metadata_json)
                       VALUES('revived','revived',1,'canceled','{"dispatch":{"pid":123}}')"""
                )
                conn.execute(
                    """INSERT INTO source_model_runs(run_id,round_id,request_id,stage,worker_id,status,slot_reserved,
                              input_version,prompt_sha256,created_at,updated_at)
                       SELECT 'revived-model','historic',request_id,'identify','worker','starting',1,
                              'version','digest',created_at,created_at
                       FROM source_round_tasks WHERE task_id='revived'"""
                )

        calls, _, collaborators = self._publication_client(write_hook=revive)
        publication = self._publish(root, action, collaborators, claim=claim)
        self.assertEqual((publication.status, publication.completion.reason), ("published", "source_busy"), publication)
        self.assertEqual(self._read("SELECT status FROM publication_intents"), [("published",)])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE attempts SET metadata_json='{}' WHERE attempt_id='revived'")
        self.assertEqual(self._complete_delivery(claim, root).reason, "source_busy")
        self.assertEqual(self._read("SELECT slot_reserved FROM source_model_runs"), [(1,)])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("DELETE FROM source_model_runs WHERE run_id='revived-model'")
            conn.execute(
                """INSERT INTO github_actions(action_id,task_id,action_type,provider,audit_status,publish_status,created_at)
                   VALUES('historic-github','revived','open_pr','github','accepted','published','now')"""
            )
            conn.execute(
                """INSERT INTO publication_intents(publication_id,action_id,idempotency_key,request_fingerprint,
                          status,created_at,updated_at)
                   VALUES('historic-effect','historic-github','key','fingerprint','published','now','now')"""
            )
        self.assertEqual(self._complete_delivery(claim, root).reason, "source_busy")

        self.assertEqual(self._read("SELECT status FROM source_rounds WHERE round_id='round-1'"), [("active",)])
        self.assertEqual(self._read("SELECT completed_at FROM work_items"), [(None,)])
        self.assertEqual(calls.count("write"), 1)

    def _publish_ready(self, collaborators=(), **kwargs):
        from robert_agent.delivery.application.publish import publish_ready_actions

        for name, collaborator in dict(collaborators).items():
            kwargs.setdefault("tapd_" + name, collaborator)
        kwargs.setdefault("source_owner", "test")
        return publish_ready_actions(self.db, config=self.config, **kwargs)

    def test_public_native_worker_publishes_selected_action_and_closes_round(self):
        claim, task, _, action = self._accepted_answer_for_delivery()
        calls, _, collaborators = self._publication_client()
        dry = self._publish_ready(collaborators, dry_run=True, task_ids=[task["task_id"]], action_ids=[action], limit=1)
        self.assertEqual((dry["pending_count"], dry["published_count"]), (1, 0), dry)
        self.assertEqual(calls, [])
        self.assertEqual(self._read("SELECT status FROM source_rounds"), [("active",)])
        self.assertEqual(self._read("SELECT status FROM publication_intents"), [])
        self.assertEqual(self._read("SELECT owner,token FROM source_sessions"), [(claim.owner, claim.token)])
        result = self._publish_ready(collaborators, task_ids=[task["task_id"]], action_ids=[action], limit=1)
        self.assertEqual((result["pending_count"], result["published_count"]), (1, 1), result)
        self.assertEqual(self._read("SELECT status FROM source_rounds"), [("execution_finished",)])
        self.assertEqual(self._read("SELECT external_id,publish_status FROM github_actions"), [("3", "published")])
        self.assertEqual(self._read("SELECT COUNT(*) FROM attempts"), [(1,)])
        repeated = self._publish_ready(collaborators, action_ids=[action])
        self.assertEqual(repeated["pending_count"], 0, repeated)
        self.assertEqual(calls.count("write"), 1)
        from unittest.mock import patch

        from robert_agent.delivery.application import source_publication_queue

        stale_action = {
            "native_source_result_id": self._read("SELECT source_result_id FROM source_results")[0][0],
            "action_id": action,
            "provider": "tapd",
            "created_at": self.now.isoformat(),
        }
        with patch.object(source_publication_queue, "source_publication_page", return_value=((stale_action,), ())):
            dedup = self._publish_ready(collaborators, action_ids=[action])
        self.assertEqual(
            (dedup["published_count"], dedup["deduplicated_count"], dedup["native_deduplicated_count"]),
            (1, 1, 1),
            dedup,
        )
        self.assertEqual(calls.count("write"), 1)

    def _native_ready(self, collaborators=(), **kwargs):
        from robert_agent.delivery.application.source_publication_queue import source_publication_ready

        for name, collaborator in dict(collaborators).items():
            if name != "client_factory":
                kwargs.setdefault("tapd_" + name, collaborator)
        kwargs.setdefault("source_owner", "test")
        with closing(database.connect(self.db)) as conn:
            return source_publication_ready(conn, self.config, **kwargs)

    def _model_question_for_publication(self):
        first, second = self.comment("1"), self.comment("2")
        claim, _ = self.admit([first, second])
        outcome = self._run_source_model(
            claim,
            "identify",
            "public-question",
            {
                "kind": "question",
                "question": "Which behavior should I investigate?",
                "handled_event_fingerprints": [first.version.event_id],
            },
        )
        self.assertEqual(outcome.status, "succeeded", outcome)
        root = self._accept_model(claim, "public-question")
        [[action]] = self._read("SELECT action_id FROM github_actions WHERE source_result_id=?", (root,))
        return claim, root, action

    def _question_message_identity(self, repos=()):
        identity = {
            "protocol": "robert.message.v1",
            "provider": "openclaw",
            "channel": "telegram",
            "conversation": "telegram:42",
            "thread": "",
            "account": "test-account",
            "sender_id": "42",
        }
        self.config.update(db_path=str(self.db))
        self.config["messaging"] = {
            **identity,
            "enabled": True,
            "allowed_senders": ["42", "43"],
            "send_after": "2026-07-01T00:00:00Z",
            "target": "42",
            "repos": list(repos),
            "tapd_projects": [{"host": self.ticket.host, "workspace_id": self.ticket.workspace_id}],
        }
        self.config["tapd"]["projects"][0]["operators"] = ["openclaw:telegram:test-account:42"]
        return identity

    def test_delivered_model_question_ack_uses_exact_root_and_filters_forged_outbox(self):
        import json

        from robert_agent.delivery import messages
        from robert_agent.execution.notifications import has_pending_local_delivery
        from robert_agent.foundation.values import utc_now
        from robert_agent.persistence.stores import notifications
        from robert_agent.work.application import messaging
        from robert_agent.work.errors import WorkItemNotFoundError, WorkItemValidationError

        claim, root, action = self._model_question_for_publication()
        _, _, collaborators = self._publication_client()
        self.assertEqual(self._publish(root, action, collaborators, claim=claim).completion.reason, "waiting_for_user")
        self.assertEqual(self._complete_delivery(claim, root).status, "already_completed")
        [[identifier]] = self._read("SELECT notification_id FROM source_question_attentions")
        identity = self._question_message_identity()
        with closing(database.connect(self.db)) as conn, conn:
            for index in range(101):
                notifications.record_notification(
                    conn,
                    f"forged-{index}",
                    None,
                    "source_ordinary_question",
                    "local",
                    "recorded",
                    "2029-11-17T00:00:00Z",
                    json.dumps({"source_attention_id": identifier}) if index % 2 else "{}",
                )
        [entry] = messaging.inbox(self.config, identity)["messages"]
        self.assertEqual(entry["notification_id"], identifier)
        self.assertEqual(entry["summary"], "Which behavior should I investigate?")
        self.assertEqual(entry["target"]["source_result_id"], root)
        self.assertEqual(entry["actions"], ["ack"])
        self.assertEqual(entry["version"], 1)
        response = {**identity, "notification_id": identifier, "version": entry["version"], "action": "ack"}
        self.assertEqual(messaging.inbox(self.config, {**identity, "sender_id": "43"})["messages"], [])
        with self.assertRaises(WorkItemNotFoundError):
            messaging.respond(self.config, {**response, "sender_id": "43"})
        with self.assertRaises(WorkItemValidationError):
            messaging.respond(self.config, {**response, "action": "reply", "body": "Example answer"})
        self.assertEqual(messaging.respond(self.config, response)["status"], "acknowledged")
        self.assertEqual(messaging.respond(self.config, response)["status"], "acknowledged")
        [open_question] = messaging.inbox(self.config, identity)["messages"]
        self.assertEqual(open_question["notification_id"], identifier)
        self.assertEqual(self._read("SELECT version,resolved_at FROM source_attentions"), [(1, None)])
        with closing(database.connect(self.db)) as conn:
            self.assertIsNone(messages.envelope(conn, identifier, config=self.config))
            self.assertTrue(has_pending_local_delivery(conn, ["unrelated"], utc_now(), self.config))
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE publication_intents SET remote_evidence_json=json_remove(remote_evidence_json,'$.receipt.own_body_evidence_id')"
            )
        self.assertEqual(messaging.inbox(self.config, identity)["messages"], [])
        with closing(database.connect(self.db)) as conn:
            self.assertFalse(has_pending_local_delivery(conn, ["unrelated"], utc_now(), self.config))
        with self.assertRaises(WorkItemNotFoundError):
            messaging.respond(self.config, response)

    def test_synthesis_question_requires_every_selected_repo_in_inbox_and_readiness(self):
        from robert_agent.adapters.git.source_evidence import EvidenceReader
        from robert_agent.execution.application import source_models
        from robert_agent.execution.notifications import has_pending_local_delivery
        from robert_agent.foundation.values import utc_now
        from robert_agent.persistence.queries import source_sessions as rounds
        from robert_agent.work.application import messaging
        from robert_agent.work.errors import WorkItemNotFoundError

        beta = {**self.repo, "full_name": "org/beta", "repo_root": str(self.root / "beta")}
        self.config["repos"].append(beta)
        self.config["tapd"]["projects"][0]["repositories"].append("org/beta")
        Path(beta["repo_root"]).mkdir()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) VALUES('b',?,?,?,?,?)",
                tuple(beta.values()),
            )
        for repo in (self.repo, beta):
            (Path(repo["repo_root"]) / "README.md").write_text("Evidence\n")
        with EvidenceReader({"a": self.repo["repo_root"], "b": beta["repo_root"]}) as reader:
            evidence = tuple(reader.read(repo_id, "README.md") for repo_id in ("a", "b"))
        refs = [{"repo_id": proof.repo_id, "path": proof.path, "revision": proof.revision} for proof in evidence]
        first, second = self.comment("1"), self.comment("2")
        comments = [first, second]
        claim, revision = self.admit(comments)
        result = self._run_source_model(
            claim,
            "identify",
            "selection",
            {"kind": "selection", "repositories": ["a", "b"], "references": refs},
            evidence=evidence,
        )
        self.assertEqual(result.status, "succeeded", result)
        for repo_id in ("a", "b"):
            self._model_evidence_child(claim, revision, comments, repo_id=repo_id, question="Which component?")
            [[revision]] = self._read(
                "SELECT json_extract(metadata_json,'$.revision') FROM sources WHERE provider='tapd'"
            )
        with closing(database.connect(self.db)) as conn:
            children = source_models.synthesis_children(conn, rounds.active_round(conn, self.ticket.source_id))
        result = self._run_source_model(
            claim,
            "synthesis",
            "synthesized-question",
            {
                "kind": "question",
                "question": "Which behavior spans both repositories?",
                "handled_event_fingerprints": [first.version.event_id],
            },
            children=children,
        )
        self.assertEqual(result.status, "succeeded", result)
        root = self._accept_model(claim, "synthesized-question")
        self.assertIsNotNone(root)
        [[action]] = self._read("SELECT action_id FROM github_actions WHERE source_result_id=?", (root,))
        _, _, collaborators = self._publication_client()
        publication = self._publish(root, action, collaborators, claim=claim)
        self.assertEqual(publication.completion.reason, "waiting_for_user")
        identity = self._question_message_identity(["org/alpha", "org/beta"])
        [entry] = messaging.inbox(self.config, identity)["messages"]
        self.assertEqual(entry["target"]["repo_ids"], ["a", "b"])
        self.assertEqual(entry["actions"], ["ack"])
        with closing(database.connect(self.db)) as conn:
            self.assertTrue(has_pending_local_delivery(conn, ["a", "b"], utc_now(), self.config))
            self.assertFalse(has_pending_local_delivery(conn, ["a"], utc_now(), self.config))
        self.config["messaging"]["repos"] = ["org/alpha"]
        self.assertEqual(messaging.inbox(self.config, identity)["messages"], [])
        with closing(database.connect(self.db)) as conn:
            self.assertFalse(has_pending_local_delivery(conn, ["a", "b"], utc_now(), self.config))
        with self.assertRaises(WorkItemNotFoundError):
            messaging.respond(
                self.config,
                {**identity, "notification_id": entry["notification_id"], "version": entry["version"], "action": "ack"},
            )

    def test_model_question_readback_unknown_then_confirmed_after_revocation_has_no_attention(self):
        from dataclasses import replace

        claim, root, action = self._model_question_for_publication()

        def remove_proof(rows):
            rows[0] = self._publication_comment(rows[0].body_html, proof=False)

        calls, rows, collaborators = self._publication_client(write_hook=remove_proof)
        result = self._publish(root, action, collaborators, claim=claim)
        self.assertEqual(result.status, "unknown", result)
        self.assertEqual(self._read("SELECT COUNT(*) FROM source_question_attentions"), [(0,)])
        rows[0] = self._publication_comment(rows[0].body_html)
        observe = collaborators["observation_provider"]

        def revoke(*args, **kw):
            snapshot = observe(*args, **kw)
            return replace(snapshot, comments=(self.comment("1", mention=False), *snapshot.comments[1:]))

        result = self._publish(root, action, {**collaborators, "observation_provider": revoke}, claim=claim)
        self.assertEqual(result.status, "published", result)
        self.assertEqual(result.completion.reason, "source_round_canceled_or_inactive")
        self.assertEqual(calls.count("write"), 1)
        self.assertEqual(self._read("SELECT COUNT(*) FROM source_question_attentions"), [(0,)])

    def test_public_taskless_question_is_project_scoped_and_needs_owner_and_capability(self):
        from threading import Event

        from robert_agent.delivery.lock import source_publication_lock

        claim, _, action = self._model_question_for_publication()
        calls, _, collaborators = self._publication_client()
        self.assertFalse(self._native_ready(collaborators, repo_id="a"))
        self.assertFalse(self._native_ready(collaborators, task_ids=["absent"]))
        self.assertFalse(self._native_ready(collaborators, source_owner="foreign-owner"))
        self.assertFalse(self._native_ready(collaborators, source_owner=None))
        self.assertFalse(
            self._native_ready(
                observation_provider_only := {"observation_provider": collaborators["observation_provider"]}
            )
        )
        self.assertEqual(self._publish_ready(observation_provider_only, provider="tapd")["pending_count"], 0)
        self.assertEqual(self._publish_ready(collaborators, provider="github")["pending_count"], 0)
        with self.assertRaisesRegex(ValueError, "Unsupported publication provider"):
            self._publish_ready(collaborators, provider="invalid")
        self.assertEqual(calls, [])
        self.assertEqual(self._read("SELECT owner,token FROM source_sessions"), [(claim.owner, claim.token)])
        self.assertTrue(self._native_ready(collaborators, action_ids=[action]))
        stop = Event()
        stop.set()
        self.assertEqual(self._publish_ready(collaborators, provider="tapd", stop=stop)["published_count"], 0)
        self.assertEqual(calls, [])
        with source_publication_lock(self.db, self.ticket.source_id) as locked:
            self.assertTrue(locked)
            held = self._publish_ready(collaborators, provider="tapd")
        self.assertEqual((held["published_count"], held["native_deferred_count"]), (0, 1))
        result = self._publish_ready(collaborators, provider="tapd")
        self.assertEqual((result["native_published_count"], result["native_completed_count"]), (1, 1), result)
        self.assertEqual(self._read("SELECT status FROM source_rounds"), [("waiting_for_user",)])
        self.assertEqual(self._read("SELECT COUNT(*) FROM tasks"), [(0,)])
        self.assertEqual(
            self._read("SELECT kind,repo_id,task_id FROM source_attentions"),
            [("ordinary_question", None, None)],
        )

    def test_public_unknown_effect_rechecks_old_page_and_reconciles_after_due_without_write(self):
        from unittest.mock import patch

        from robert_agent.adapters.tapd.client import TapdError
        from robert_agent.delivery.application import source_publication_queue

        _, _, action = self._model_question_for_publication()

        def timeout(_rows):
            raise TapdError("timeout", effect_unknown=True)

        calls, _, collaborators = self._publication_client(write_hook=timeout)
        with closing(database.connect(self.db)) as conn:
            stale_page = source_publication_queue.source_publication_page(
                conn,
                self.config,
                source_owner="test",
                limit=1,
                tapd_observation_provider=collaborators["observation_provider"],
                tapd_identity_provider=collaborators["identity_provider"],
            )
        self.assertEqual(len(stale_page[0]), 1)
        result = self._publish_ready(collaborators, provider="tapd", limit=1)
        self.assertEqual((result["native_unknown_count"], result["failed_count"]), (1, 1), result)
        self.assertFalse(self._native_ready(collaborators))
        with patch.object(source_publication_queue, "source_publication_page", return_value=stale_page):
            stale = self._publish_ready(collaborators, provider="tapd", limit=1)
        self.assertEqual((stale["native_deferred_count"], calls.count("read"), calls.count("write")), (1, 1, 1))
        self.assertEqual(self._read("SELECT status FROM publication_intents"), [("attention",)])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE github_actions SET metadata_json=json_set(metadata_json,'$.publish.next_retry_at',?) WHERE action_id=?",
                ("2000-01-01T00:00:00+00:00", action),
            )
        collaborators.pop("identity_provider")
        self.assertTrue(self._native_ready(collaborators))
        recovered = self._publish_ready(collaborators, provider="tapd", limit=1)
        self.assertEqual((recovered["native_published_count"], recovered["native_completed_count"]), (1, 1), recovered)
        self.assertEqual((calls.count("read"), calls.count("write")), (2, 1))
        self.assertEqual(self._read("SELECT status FROM source_rounds"), [("waiting_for_user",)])

    def test_public_lost_claim_during_failed_transport_cannot_save_old_pacing(self):
        from robert_agent.adapters.tapd.client import TapdError
        from robert_agent.foundation.values import utc_now

        claim, _, action = self._model_question_for_publication()

        def lose_claim(_rows):
            self._expire_publication_claim(claim)
            with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
                transactions.begin_immediate(conn)
                self.assertIsNotNone(
                    source_sessions.claim(
                        conn, source_id=self.ticket.source_id, owner="next", now=utc_now(), lease_seconds=300
                    )
                )
            raise TapdError("timeout", effect_unknown=True)

        calls, _, collaborators = self._publication_client(write_hook=lose_claim)
        result = self._publish_ready(collaborators, provider="tapd")
        self.assertEqual(result["native_unknown_count"], 1, result)
        self.assertEqual(self._read("SELECT status FROM publication_intents"), [("executing",)])
        self.assertEqual(
            self._read(
                "SELECT json_extract(metadata_json,'$.publish.next_retry_at') FROM github_actions WHERE action_id=?",
                (action,),
            ),
            [(None,)],
        )
        recovered = self._publish_ready(collaborators, provider="tapd", source_owner="next")
        self.assertEqual(recovered["native_published_count"], 1, recovered)
        self.assertEqual(calls.count("write"), 1)

    def test_public_cold_completion_scopes_already_published_action_at_zero_effect_budget(self):
        from unittest.mock import patch

        from robert_agent.delivery.application.source_completion import CompletionOutcome

        claim, task, root, action = self._accepted_answer_for_delivery()
        calls, _, collaborators = self._publication_client()
        with patch(
            "robert_agent.delivery.application.source_completion.complete_source_result",
            return_value=CompletionOutcome("held", "simulated_crash"),
        ):
            self.assertEqual(self._publish(root, action, collaborators, claim=claim).status, "published")
        calls.clear()
        before = self._read("SELECT owner,token,updated_at FROM source_sessions")
        for scope in ({"repo_id": "other"}, {"task_ids": ["other"]}, {"action_ids": ["other"]}):
            self.assertFalse(self._native_ready(**scope))
            outcome = self._publish_ready(provider="tapd", limit=0, **scope)
            self.assertEqual(outcome["native_completed_count"], 0, outcome)
        self.assertTrue(self._native_ready(repo_id="a", task_ids=[task["task_id"]], action_ids=[action]))
        dry = self._publish_ready(provider="tapd", dry_run=True, limit=0, action_ids=[action])
        self.assertEqual(dry["native_completed_count"], 0)
        self.assertEqual(self._read("SELECT owner,token,updated_at FROM source_sessions"), before)
        self.assertEqual(self._read("SELECT status FROM source_rounds"), [("active",)])
        finished = self._publish_ready(provider="tapd", limit=0, repo_id="a", action_ids=[action])
        self.assertEqual((finished["pending_count"], finished["native_completed_count"]), (0, 1), finished)
        self.assertEqual(self._read("SELECT status FROM source_rounds"), [("execution_finished",)])
        self.assertEqual(calls, [])
        self.assertFalse(self._native_ready(action_ids=[action]))

    def _seed_legacy_publication(self):
        from tests.support.summary_database import SummaryFixture

        legacy = self.root / "legacy.sqlite"

        class LegacyFeed:
            root = self.root
            _dd_comment_body = SummaryFixture._dd_comment_body

        SummaryFixture._init_summary_db(LegacyFeed(), legacy)
        self.config["repos"].append(
            {"full_name": "example/backend", "github_account": "robert-bot", "trusted_actors": ["wklken"]}
        )
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("ATTACH DATABASE ? AS legacy", (str(legacy),))
            for table in (
                "repos",
                "sources",
                "source_events",
                "workstreams",
                "workstream_sources",
                "tasks",
                "attempts",
                "route_decisions",
                "task_events",
                "worker_results",
                "github_actions",
            ):
                conn.execute(f"INSERT INTO main.{table} SELECT * FROM legacy.{table}")
            conn.execute("UPDATE github_actions SET created_at='2000-01-01T00:00:00+00:00' WHERE provider='github'")
            conn.execute(
                """UPDATE source_events SET payload_json=json_set(payload_json,'$.body','@robert-bot please inspect',
                                                                    '$.body_authorization_complete',json('true'),
                                                                    '$.workstream_id','ws-1','$.actor_login','wklken',
                                                                    '$.event_type','comment')
                     WHERE event_id='event-1'"""
            )

    def test_public_merged_limit_counts_native_and_legacy_in_age_order(self):
        from types import SimpleNamespace

        self._accepted_answer_for_delivery()
        self._seed_legacy_publication()
        calls, _, collaborators = self._publication_client()
        github = []

        def github_runner(command, **_):
            github.append(command)
            listing = "?per_page=100" in command[2]
            return SimpleNamespace(
                returncode=0,
                stdout="[]" if listing else '{"id":987,"html_url":"https://github.com/x/y/issues/1#issuecomment-987"}',
                stderr="",
            )

        first = self._publish_ready(collaborators, limit=1, run_command=github_runner)
        self.assertEqual(
            (first["pending_count"], first["published_count"], first["native_published_count"]), (1, 1, 0), first
        )
        self.assertEqual(calls, [])
        self.assertGreaterEqual(len(github), 2)
        second = self._publish_ready(collaborators, limit=1000, run_command=github_runner)
        self.assertEqual(
            (second["pending_count"], second["published_count"], second["native_published_count"]), (1, 1, 1), second
        )
        self.assertEqual(calls.count("write"), 1)
        self.assertEqual(
            self._read("SELECT provider,publish_status FROM github_actions ORDER BY provider"),
            [("github", "published"), ("tapd", "published")],
        )

    def _seed_older_source_publications(self, *, unsettled_count=0, malformed_count=0):
        import json

        older = "2000-01-01T00:00:00+00:00"
        sources = []
        with closing(sqlite3.connect(self.db)) as conn, conn:
            for index in range(unsettled_count + malformed_count):
                is_malformed = index >= unsettled_count
                mode = index % 3
                workspace = "999" if not is_malformed and mode == 0 else self.ticket.workspace_id
                ticket = facts.TicketIdentity(self.ticket.host, workspace, "bug", str(1000 + index))
                root_id, action_id, round_id = f"old-result-{index}", f"old-action-{index}", f"old-round-{index}"
                sources.append(ticket.source_id)
                conn.execute(
                    """INSERT INTO sources(source_id,source_key,source_type,provider,host,project_key,external_id)
                         VALUES(?,?,'bug','tapd',?,?,?)""",
                    (ticket.source_id, ticket.source_id, ticket.host, ticket.workspace_id, ticket.external_id),
                )
                owner = "foreign-owner" if not is_malformed and mode == 1 else "test"
                conn.execute(
                    "INSERT INTO source_sessions(source_id,owner,token,lease_until,updated_at) VALUES(?,?,1,?,?)",
                    (ticket.source_id, owner, "2099-01-01T00:00:00+00:00", older),
                )
                target = json.dumps(
                    {
                        "provider": "tapd",
                        "host": ticket.host,
                        "workspace_id": workspace,
                        "kind": "bug",
                        "external_id": ticket.external_id,
                    }
                )
                if is_malformed:
                    conn.execute(
                        """INSERT INTO source_rounds(round_id,source_id,status,capability,repo_ids_json,publication_scope_json,
                                  input_version,input_snapshot_json,input_sha256,created_at)
                             VALUES(?,?,'active','readonly','[]',?,'v','{}','wrong-sha',?)""",
                        (round_id, ticket.source_id, target, older),
                    )
                    # Coarse rows copied from historical storage can be malformed.
                    # They are deliberately missing the model FK and receipt proof.
                    conn.execute(
                        """INSERT INTO source_results(source_result_id,source_id,round_id,kind,input_version,idempotency_key,
                                  model_run_id,evidence_json,payload_json,accepted_at)
                             VALUES(?,?,?,'question','v',?,?,'{}','{}',?)""",
                        (root_id, ticket.source_id, round_id, root_id, f"missing-model-{index}", older),
                    )
                else:
                    conn.execute(
                        """INSERT INTO source_results(source_result_id,source_id,kind,input_version,idempotency_key,evidence_json,
                                  payload_json,accepted_at) VALUES(?,?,'control_notice','v',?,'{}','{}',?)""",
                        (root_id, ticket.source_id, root_id, older),
                    )
                conn.execute(
                    """INSERT INTO github_actions(action_id,source_result_id,provider,action_type,target_json,audit_status,
                              publish_status,metadata_json,created_at)
                         VALUES(?,?,'tapd','comment',?,'accepted',?,?,?)""",
                    (
                        action_id,
                        root_id,
                        target,
                        "published" if is_malformed else "not_published",
                        '{"publish":{"retryable":false}}' if not is_malformed and mode == 2 else "{}",
                        older,
                    ),
                )
                conn.execute(
                    "INSERT INTO source_required_actions(action_id,source_result_id,position) VALUES(?,?,0)",
                    (action_id, root_id),
                )
        return sources

    def test_public_effect_filters_older_wrong_projects_foreign_leases_and_attention_before_limit(self):
        _, _, action = self._model_question_for_publication()
        calls, _, collaborators = self._publication_client()
        self._seed_older_source_publications(unsettled_count=36)
        # Keep exact write/receipt verification, with multiple pending rows in the same DB.
        result = self._publish_ready(collaborators, limit=1)
        self.assertEqual((result["pending_count"], result["published_count"]), (1, 1), result)
        self.assertEqual(
            self._read("SELECT action_id FROM github_actions WHERE publish_status='published'"), [(action,)]
        )
        self.assertEqual(calls.count("write"), 1)
        self.assertFalse(self._native_ready(collaborators))

    def test_public_malformed_completion_pages_back_off_and_do_not_starve_later_root(self):
        from unittest.mock import patch

        from robert_agent.delivery.application.source_completion import CompletionOutcome

        claim, _, root, action = self._accepted_answer_for_delivery()
        calls, _, collaborators = self._publication_client()
        with patch(
            "robert_agent.delivery.application.source_completion.complete_source_result",
            return_value=CompletionOutcome("held", "simulated_crash"),
        ):
            self.assertEqual(self._publish(root, action, collaborators, claim=claim).status, "published")
        calls.clear()
        self._seed_older_source_publications(malformed_count=25)
        self.assertTrue(self._native_ready())
        first = self._publish_ready(provider="tapd", limit=0)
        self.assertEqual((first["native_completed_count"], first["native_completion_held_count"]), (0, 20), first)
        self.assertTrue(self._native_ready())
        second = self._publish_ready(provider="tapd", limit=0)
        self.assertEqual((second["native_completed_count"], second["native_completion_held_count"]), (1, 5), second)
        self.assertEqual(calls, [])
        self.assertFalse(self._native_ready())
        self.assertEqual(self._read("SELECT COUNT(*) FROM tapd_coordination WHERE status='held'"), [(25,)])
        self.assertEqual(
            self._read("SELECT status FROM source_rounds WHERE round_id='round-1'"), [("execution_finished",)]
        )

    def test_public_invalid_persisted_source_identity_records_attention_once(self):
        self._model_question_for_publication()
        calls, _, collaborators = self._publication_client()
        [bad_source] = self._seed_older_source_publications(unsettled_count=1)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE sources SET project_key=? WHERE source_id=?", (self.ticket.workspace_id, bad_source))
            conn.execute(
                """UPDATE github_actions SET target_json=json_set(target_json,'$.workspace_id',?)
                   WHERE action_id='old-action-0'""",
                (self.ticket.workspace_id,),
            )
        first = self._publish_ready(collaborators, limit=1)
        self.assertEqual((first["native_invalid_count"], first["published_count"]), (1, 0), first)
        self.assertEqual(calls, [])
        self.assertEqual(
            self._read(
                """SELECT json_extract(metadata_json,'$.publish.status'),json_extract(metadata_json,'$.publish.retryable')
                   FROM github_actions WHERE action_id='old-action-0'"""
            ),
            [("attention", 0)],
        )
        second = self._publish_ready(collaborators, limit=1)
        self.assertEqual(second["published_count"], 1, second)
        self.assertEqual(calls.count("write"), 1)

    def test_public_large_limit_only_uses_provable_sorted_prefix_when_native_page_is_full(self):
        from unittest.mock import patch

        from robert_agent.delivery.application import source_publication_queue
        from robert_agent.delivery.application.publish import publish_ready_actions

        self._model_question_for_publication()
        self._seed_legacy_publication()
        rows = tuple(
            {"native_source_result_id": f"root-{i}", "action_id": f"action-{i:03}", "created_at": "1999-01-01"}
            for i in range(source_publication_queue.MAX_NATIVE_PAGE)
        )
        with patch.object(source_publication_queue, "source_publication_page", return_value=(rows, ())):
            plan = publish_ready_actions(self.db, dry_run=True, limit=1000, config=self.config, source_owner="test")
        self.assertEqual(plan["pending_count"], source_publication_queue.MAX_NATIVE_PAGE)

    def test_publish_lane_keeps_actual_taskless_receipt_on_github_budget_refusal(self):
        from unittest.mock import patch

        from robert_agent.application import publication_cycle
        from robert_agent.delivery.application import lane
        from robert_agent.execution import source_controller

        claim, _, _ = self._model_question_for_publication()
        self._expire_publication_claim(claim)
        calls, _, collaborators = self._publication_client()
        config = {**self.config, "ok": True, "db_path": self.db, "config_path": "unused"}
        with (
            patch.object(lane.validate_config, "validate_config", return_value=config),
            patch.object(lane.github_read, "GitHubReadSession"),
            patch.object(lane.github_budget, "budget", return_value=False),
            patch.object(publication_cycle.dispatch_requests, "advance") as dispatch,
        ):
            result = publication_cycle.publish(
                "unused", limit=1, **{"tapd_" + key: value for key, value in collaborators.items()}
            )
        self.assertEqual((result["ok"], result["status"]), (False, "core_rate_limit_floor"))
        self.assertEqual((result["pending_count"], result["native_published_count"]), (1, 1), result)
        self.assertEqual(self._read("SELECT status FROM source_rounds"), [("waiting_for_user",)])
        self.assertEqual(
            self._read("SELECT owner FROM source_sessions"), [(source_controller.standalone_owner(self.db),)]
        )
        self.assertEqual(self._read("SELECT count(*) FROM tasks"), [(0,)])
        self.assertEqual(calls.count("write"), 1)
        dispatch.assert_not_called()

    def test_cycle_native_taskless_pass_spends_shared_github_budget_once(self):
        from unittest.mock import patch

        from robert_agent.application import repository_cycle
        from robert_agent.delivery.application import lane, publish
        from robert_agent.runtime import cycle

        claim, _, _ = self._model_question_for_publication()
        self._expire_publication_claim(claim)
        calls, _, collaborators = self._publication_client()
        self._seed_legacy_publication()
        for repo in self.config["repos"]:
            repo.setdefault("repo_root", str(self.root))
            repo.setdefault("worktree_root", str(self.root / "trees"))
            repo.setdefault("default_base_branch", "main")
        config = {
            **self.config,
            "ok": True,
            "data_dir": self.root,
            "db_path": self.db,
            "model_max_concurrency": 1,
            "max_concurrency": 1,
            "python_bin": "python3",
        }
        github = []
        actual = publish.publish_ready_actions

        def publish_scoped(db_path, **kw):
            if kw.get("provider") == "github":
                github.append((kw["repo_id"], kw["limit"]))
                return {"ok": True, "status": "published", "pending_count": 1, "published_count": 1}
            return actual(db_path, **kw)

        def repo_pipeline(conn, **kw):
            summary = {
                key: 0
                for key in (
                    "supervised_count",
                    "failed_timeout_recovered_count",
                    "audited_result_count",
                    "reconciled_source_count",
                    "raw_event_count",
                    "decision_count",
                    "current_state_closed_skip_count",
                    "publish_pending_count",
                    "published_count",
                    "publish_skipped_count",
                    "publish_failed_count",
                    "finalized_task_count",
                )
            }
            limit = kw["publish_budget"]["remaining"]
            if limit:
                receipt = repository_cycle._publish_ready_actions_for_repo(
                    kw["db_path"],
                    False,
                    kw["repo_id"],
                    kw["repo_count"],
                    limit=limit,
                    config_result=kw["config_result"],
                )
                summary["publish_pending_count"] = receipt["pending_count"]
                summary["published_count"] = receipt["published_count"]
                kw["publish_budget"]["remaining"] -= receipt["pending_count"]
            return {**summary, "status": "completed", "lease_status": "acquired", "safe_error": None}

        options = {"tapd_" + key: value for key, value in collaborators.items()}
        with (
            patch.object(cycle.validate_config, "validate_config", return_value=config),
            patch.object(cycle.native_sources, "run_once", return_value={}),
            patch.object(cycle.discover, "collect_account_notifications", return_value={}),
            patch.object(cycle.dispatcher, "dispatch_queued", return_value=[]),
            patch.object(repository_cycle, "run_repo_pipeline", side_effect=repo_pipeline),
            patch.object(publish, "publish_ready_actions", side_effect=publish_scoped),
            patch.object(lane, "publish_sources", wraps=lane.publish_sources) as native_pass,
        ):
            result = cycle.run_once("unused", dry_run=False, max_publish_actions=2, **options)
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            (result["publish_result"]["pending_count"], result["publish_result"]["native_published_count"]), (2, 1)
        )
        self.assertEqual(calls.count("write"), 1)
        self.assertEqual(len(github), 1)
        self.assertEqual(github[0][1], 1)
        self.assertEqual(native_pass.call_count, 1)
        self.assertEqual(self._read("SELECT status FROM source_rounds"), [("waiting_for_user",)])

    def test_direct_cli_and_zero_budget_loop_recover_cold_root_without_provider(self):
        import io
        import json
        from contextlib import redirect_stdout
        from unittest.mock import patch

        from robert_agent.delivery.application import publish
        from robert_agent.delivery.application.source_completion import CompletionOutcome
        from robert_agent.runtime import cycle, loop

        claim, root, action = self._model_question_for_publication()
        _, _, collaborators = self._publication_client()
        with patch(
            "robert_agent.delivery.application.source_completion.complete_source_result",
            return_value=CompletionOutcome("held", "simulated_old_controller"),
        ):
            self.assertEqual(self._publish(root, action, collaborators, claim=claim).status, "published")
        self._expire_publication_claim(claim)
        config = {
            **self.config,
            "ok": True,
            "data_dir": self.root,
            "db_path": self.db,
            "model_max_concurrency": 1,
            "max_concurrency": 1,
            "python_bin": "python3",
        }
        baseline = self.root / "receipt-before-local-completion.sqlite3"
        with closing(sqlite3.connect(self.db)) as original, closing(sqlite3.connect(baseline)) as saved:
            original.backup(saved)
        output = io.StringIO()
        with (
            patch.object(publish.supervision_publication, "fresh_config", return_value=config),
            redirect_stdout(output),
        ):
            self.assertEqual(publish.main(["--db", str(self.db), "--config", "unused", "--limit", "0"]), 0)
        self.assertEqual(json.loads(output.getvalue())["native_completed_count"], 1)
        self.assertEqual(self._read("SELECT status FROM source_rounds"), [("waiting_for_user",)])
        # Exercise the loop from the same consistent pre-completion crash state.
        # Resetting just the round would retain already-committed attention and its receipt.
        with closing(sqlite3.connect(baseline)) as saved, closing(sqlite3.connect(self.db)) as restored:
            saved.backup(restored)
        with (
            patch.object(cycle.validate_config, "validate_config", return_value=config),
            patch.object(cycle.readiness, "has_local_work", return_value=False),
            patch.object(loop, "has_runnable_local_work", return_value=False),
            patch.object(cycle.native_sources, "run_once", return_value={}),
        ):
            result = loop.run_loop(
                "unused",
                dry_run=False,
                skip_external=True,
                consume_observations=True,
                max_publish_actions=0,
            )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["cycles"], 1)
        self.assertEqual(result["budgets"]["max_publish_actions_used"], 0)
        self.assertEqual(result["cycle_results"][0]["publish_result"]["native_completed_count"], 1)
        self.assertEqual(self._read("SELECT count(*) FROM tasks"), [(0,)])

    def test_cycle_publish_modes_suppress_remote_native_and_keep_zero_budget_exact(self):
        from unittest.mock import patch

        from robert_agent.runtime import cycle

        claim, _, _ = self._model_question_for_publication()
        self._expire_publication_claim(claim)
        calls, _, collaborators = self._publication_client()
        config = {
            **self.config,
            "ok": True,
            "data_dir": self.root,
            "db_path": self.db,
            "model_max_concurrency": 1,
            "max_concurrency": 1,
            "python_bin": "python3",
        }
        options = {"tapd_" + key: value for key, value in collaborators.items()}
        with (
            patch.object(cycle.validate_config, "validate_config", return_value=config),
            patch.object(cycle.native_sources, "run_once", return_value={}),
            patch.object(cycle.readiness, "has_local_work", return_value=False),
        ):
            for mode in (
                {"dry_run": True},
                {"skip_external": True},
                {"skip_publish": True},
                {"manage_sources": False},
                {"max_publish_actions": 0},
            ):
                with self.subTest(mode=mode):
                    outcome = cycle.run_once(
                        "unused",
                        consume_observations=True,
                        **{"dry_run": False, **mode},
                        **options,
                    )
                    self.assertTrue(outcome["ok"], outcome)
                    self.assertEqual(calls, [])
        self.assertEqual(self._read("SELECT publish_status FROM github_actions"), [("not_published",)])

    def test_paused_scan_publisher_only_reconciles_an_existing_unknown_effect(self):
        import io
        import json
        from contextlib import redirect_stdout
        from unittest.mock import patch

        from robert_agent.adapters.tapd.client import TapdError
        from robert_agent.delivery.application import lane
        from robert_agent.runtime import scan

        claim, _, action = self._model_question_for_publication()

        def timeout(_rows):
            raise TapdError("timeout", effect_unknown=True)

        calls, _, collaborators = self._publication_client(write_hook=timeout)
        self._expire_publication_claim(claim)
        config = {**self.config, "ok": True, "db_path": self.db, "config_path": "unused"}
        options = {"tapd_" + key: value for key, value in collaborators.items()}
        command = ["--config", "unused", "--publish", "--source-only"]

        def invoke(*flags):
            output = io.StringIO()
            with redirect_stdout(output):
                scan.main([*command, *flags], **options)
            return json.loads(output.getvalue())

        with patch.object(lane.validate_config, "validate_config", return_value=config):
            self.assertEqual(invoke("--source-reconcile-only")["status"], "idle")
            self.assertEqual(calls, [])
            first = invoke()
            self.assertEqual((first["native_unknown_count"], first["failed_count"]), (1, 1), first)
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute(
                    "UPDATE github_actions SET metadata_json=json_set(metadata_json,'$.publish.next_retry_at',?) WHERE action_id=?",
                    ("2000-01-01T00:00:00+00:00", action),
                )
            recovered = invoke("--source-reconcile-only")
        self.assertEqual((recovered["native_published_count"], recovered["native_completed_count"]), (1, 1), recovered)
        self.assertEqual(calls.count("identity"), 1)
        self.assertEqual(calls.count("write"), 1)
        self.assertEqual(calls.count("read"), 2)
        self.assertEqual(self._read("SELECT status FROM source_rounds"), [("waiting_for_user",)])
