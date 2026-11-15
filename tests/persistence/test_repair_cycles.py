"""Migration of legacy repair counters into authoritative cycle storage."""

from contextlib import closing
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from robert_agent.persistence.schema.database import init_database


class RepairCycleMigrationTests(unittest.TestCase):
    def test_old_episode_count_clock_and_observations_survive(self):
        fixture = Path(__file__).resolve().parents[1] / "fixtures/persistence/pre_orm_5e0e952.sql"
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "state.sqlite3"
            with closing(sqlite3.connect(db)) as conn, conn:
                conn.executescript(fixture.read_text())
                stream = conn.execute("SELECT workstream_id FROM workstreams LIMIT 1").fetchone()[0]
                conn.execute(
                    "INSERT INTO pr_remediation_episodes(episode_id,workstream_id,episode_kind,subject_key,"
                    "observed_head_sha,observed_base_sha,status,attempt_count,first_seen_at,updated_at)"
                    " VALUES('episode',?,'ci','head:base','head','base','exhausted',2,'2026-11-01','2026-11-03')",
                    (stream,),
                )
                conn.execute(
                    "INSERT INTO pr_ci_observations(observation_id,episode_id,source_kind,external_id,check_name,status)"
                    " VALUES('observation','episode','check_run','17','unit','completed')"
                )
            for _ in range(2):
                init_database(db)
            with closing(sqlite3.connect(db)) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT episode_id,legacy_attempt_count,started_at FROM repair_cycles WHERE scope='github'"
                    ).fetchall(),
                    [("episode", 2, "2026-11-01")],
                )
                self.assertEqual(
                    conn.execute("SELECT observation_id,episode_id FROM pr_ci_observations").fetchall(),
                    [("observation", "episode")],
                )
                self.assertNotIn(
                    "attempt_count", {row[1] for row in conn.execute("PRAGMA table_info(pr_remediation_episodes)")}
                )
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])


class NativeRepairCycleTests(unittest.TestCase):
    def setUp(self):
        from robert_agent.work import tapd_authority

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / "native.sqlite3"
        init_database(self.db)
        self.ticket = tapd_authority.TicketIdentity("tapd.example", "1", "bug", "981")
        self.policy = {
            "max_attempts": 3,
            "ci": {
                "enabled": True,
                "check_allowlist": ["unit"],
                "max_wall_minutes": 20,
                "max_total_tokens": 100,
                "max_cost_usd": 10,
            },
            "conflict": {"enabled": True, "max_wall_minutes": 10},
        }
        self.config = {
            "enabled": True,
            "host": "tapd.example",
            "api_timezone": "UTC",
            "bot_name": "robert",
            "reply_name": "robert",
            "projects": [
                {
                    "workspace_id": "1",
                    "trusted_author": "reviewer",
                    "repositories": ["org/repo"],
                    "mentions": {"enabled": True, "activated_after": self.now(0)},
                    "rules": [
                        {
                            "id": "rule",
                            "assignee": "owner",
                            "label": "queue",
                            "repo": "org/repo",
                            "kinds": ["bug"],
                            "repair": self.policy,
                        }
                    ],
                }
            ],
        }
        self.conn = sqlite3.connect(self.db)
        self.addCleanup(self.conn.close)
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript("""
            INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root)
              VALUES('repo','org/repo','bot','main','/repo','/trees');
            INSERT INTO sources(source_id,repo_id,source_key,provider,host,project_key,source_type,external_id,
                                number,html_url,state)
              VALUES('pr','repo','github:org/repo!7','github','github.com','org/repo','pull_request','7',7,
                     'https://github.com/org/repo/pull/7','open');
            """)
        self.conn.execute("BEGIN IMMEDIATE")
        from robert_agent.persistence.stores import source_sessions
        from robert_agent.work.application.tapd_intake import observe_ticket

        self.facts = tapd_authority.TicketFacts(
            self.ticket, "Bug", "Body", "open", ("owner",), ("queue",), False, ("closed",)
        )
        result = observe_ticket(
            self.conn,
            self.config,
            tapd_authority.TicketObservation(self.facts, self.now(0), coverage="complete"),
            tapd_authority.PublicationFacts(self.ticket),
            now=self.now(0),
        )
        self.revision = result.revision
        self.first_request = result.request_ids[0]
        self.claim = source_sessions.claim(
            self.conn, source_id=self.ticket.source_id, owner="owner", now=self.now(0), lease_seconds=3600
        )
        from robert_agent.work.application.commands import ensure_tapd_work_item

        item = ensure_tapd_work_item(self.conn, repo_id="repo", source_id=self.ticket.source_id, now=self.now(0))
        self.origin = item["workstream_id"]
        self.conn.execute(
            "INSERT INTO workstreams(workstream_id,repo_id,primary_source_id,origin_workstream_id,created_at,updated_at)"
            " VALUES('pr-stream','repo','pr',?,?,?)",
            (self.origin, self.now(0), self.now(0)),
        )
        self._record_canonical_creation()

    def _record_canonical_creation(self):
        from robert_agent.persistence import execution_control
        from robert_agent.persistence.queries import source_github_publication as github, source_pr_lineage
        from robert_agent.persistence.queries.ingress_requests import find_by_id
        from robert_agent.persistence.rounds.source_evidence import context_digest
        from robert_agent.persistence.stores import source_github_publication, source_pr_lineage as lineage
        from robert_agent.persistence.stores import source_results, source_sessions

        now = self.now(0)
        request = find_by_id(self.conn, self.first_request)
        self.assertIsNotNone(
            source_sessions.start_round(
                self.conn,
                self.claim,
                round_id="creation-round",
                member_versions={self.first_request: request["content_version"]},
                input_version="creation-input",
                input_snapshot={},
                capability="development",
                repo_ids=("repo",),
                publication_scope={},
                now=now,
            )
        )
        self.conn.execute(
            "INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at)"
            " VALUES('creation-task',?,'queued',?,?)",
            (self.origin, now, now),
        )
        self.assertTrue(
            source_sessions.bind_task(
                self.conn,
                self.claim,
                round_id="creation-round",
                request_id=self.first_request,
                task_id="creation-task",
                capability="development",
                now=now,
            )
        )
        self.conn.execute("UPDATE tasks SET lifecycle='completed' WHERE task_id='creation-task'")
        self.conn.execute(
            "INSERT INTO attempts(attempt_id,task_id,attempt_no,status)"
            " VALUES('creation-attempt','creation-task',1,'completed')"
        )
        self.conn.execute(
            "INSERT INTO worker_results(result_id,task_id,attempt_id,output_type,created_at)"
            " VALUES('creation-result','creation-task','creation-attempt','new_pr',?)",
            (now,),
        )
        source_results.insert_result(
            self.conn,
            source_result_id="creation-root",
            source_id=self.ticket.source_id,
            round_id="creation-round",
            kind="answer",
            input_version="creation-input",
            idempotency_key="creation-result",
            now=now,
            evidence={
                "task_id": "creation-task",
                "attempt_id": "creation-attempt",
                "repo_id": "repo",
                "submission_fingerprint": "e" * 64,
            },
            payload={},
            worker_result_id="creation-result",
        )
        target = {
            "provider": "github",
            "host": "github.com",
            "kind": "repository",
            "repo_id": "repo",
            "repository": "org/repo",
        }
        self.conn.execute(
            "INSERT INTO github_actions(action_id,result_id,task_id,action_type,provider,target_json,audit_status,created_at)"
            " VALUES('creation-action','creation-result','creation-task','open_pr','github',?,'accepted',?)",
            (json.dumps(target), now),
        )
        source_results.attach_required_actions(
            self.conn, source_result_id="creation-root", action_ids=("creation-action",)
        )
        row = github.required_action(self.conn, "creation-root", "creation-action")
        identity = github.request_identity(row)
        marker = f"<!-- robert-workstream effect={context_digest(identity)[:32]} -->"
        body = f"Code changes\n\n{marker}"
        packet = {
            "version": 1,
            "publisher": "bot",
            "identity": identity,
            "audit_scope_sha256": "f" * 64,
            "marker": marker,
            "body_sha256": sha256(body.encode()).hexdigest(),
            "effect": {
                "repo": "org/repo",
                "remote": "origin",
                "head_repository": "bot/repo",
                "head_sha": "a" * 40,
                "base_sha": "b" * 40,
                "branch": "work",
                "head": "work",
                "base": "main",
                "title": "Code changes",
                "body": body,
            },
        }
        proof = {
            "pr": {
                "number": 7,
                "url": "https://github.com/org/repo/pull/7",
                "author": "bot",
                "head_repository": "bot/repo",
                "head_branch": "work",
                "head_sha": "a" * 40,
                "base_repository": "org/repo",
                "base_branch": "main",
                "state": "open",
                "title": "Code changes",
                "body_sha256": packet["body_sha256"],
                "marker": marker,
            }
        }
        fingerprint = context_digest(packet)
        intent = execution_control.prepare_publication_intent(
            self.conn, "creation-action", "creation-intent", fingerprint, now
        )
        self.creation_intent_id = intent["publication_id"]
        source_github_publication.insert_preparation(
            self.conn,
            source_result_id="creation-root",
            action_id="creation-action",
            publication_id=self.creation_intent_id,
            packet=packet,
        )
        execution_control.transition_publication_intent(self.conn, self.creation_intent_id, "executing", now)
        receipt = source_github_publication.confirm_effect(
            self.conn,
            source_result_id="creation-root",
            action_id="creation-action",
            publication_id=self.creation_intent_id,
            fingerprint=fingerprint,
            proof=proof,
            observed_at=now,
            now=now,
        )
        self.assertIsNotNone(receipt)
        self.assertTrue(
            lineage.attach_creation(
                self.conn,
                stream_id="pr-stream",
                repo_id="repo",
                source_id="pr",
                origin_id=self.origin,
                creation=source_pr_lineage.creation_evidence(receipt, packet),
            )
        )
        lineage.relate_pr_source(self.conn, stream_id="pr-stream", source_id="pr", now=now)
        self.assertTrue(
            source_sessions.finish_round(
                self.conn, self.claim, round_id="creation-round", outcome="execution_finished", now=now
            )
        )

    @staticmethod
    def now(minute):
        return f"2026-11-14T12:{minute:02}:00+00:00"

    def refresh_ticket(self, minute):
        from robert_agent.work import tapd_authority
        from robert_agent.work.application.tapd_intake import observe_ticket

        observed = observe_ticket(
            self.conn,
            self.config,
            tapd_authority.TicketObservation(self.facts, self.now(minute), coverage="complete"),
            tapd_authority.PublicationFacts(self.ticket),
            now=self.now(minute),
        )
        self.revision = observed.revision

    def attach(self, index, kind, minute, *, head=None, base=None):
        from robert_agent.persistence.queries.ingress_requests import find_by_id
        from robert_agent.persistence.stores import ingress_requests, source_sessions

        self.round_id, self.task_id, self.episode_id = f"round-{index}", f"task-{index}", f"episode-{index}"
        self.request_id = self.first_request if index == 1 else f"request-{index}"
        self.head = head or sha256(f"head-{index}".encode()).hexdigest()[:40]
        self.base = base or sha256(f"base-{index}".encode()).hexdigest()[:40]
        if index != 1:
            # Represent a later pending native repair request already admitted by
            # the future controller. Use the verified existing authority snapshot.
            first = find_by_id(self.conn, self.first_request)
            member = self.conn.execute(
                "SELECT event_id FROM request_events WHERE request_id=?", (self.first_request,)
            ).fetchone()[0]
            ingress_requests.enqueue(
                self.conn,
                request_id=self.request_id,
                source_id=self.ticket.source_id,
                provider="tapd",
                auth_type=first["auth_type"],
                authorization=json.loads(first["authorization_json"]),
                content_snapshot=json.loads(first["content_snapshot_json"]),
                content_version=first["content_version"],
                event_ids=(member,),
                now=self.now(minute),
            )
        request = find_by_id(self.conn, self.request_id)
        self.assertIsNotNone(
            source_sessions.start_round(
                self.conn,
                self.claim,
                round_id=self.round_id,
                member_versions={self.request_id: request["content_version"]},
                input_version="snapshot",
                input_snapshot={},
                capability="development",
                repo_ids=("repo",),
                publication_scope={},
                now=self.now(minute),
            )
        )
        self.conn.execute(
            "INSERT INTO pr_remediation_episodes(episode_id,workstream_id,episode_kind,subject_key,observed_head_sha,"
            "observed_base_sha,status,first_seen_at,updated_at) VALUES(?,'pr-stream',?,?,?,?,'open',?,?)",
            (
                self.episode_id,
                kind,
                f"{self.head}:{self.base}",
                self.head,
                self.base,
                self.now(minute),
                self.now(minute),
            ),
        )
        self.conn.execute(
            "INSERT INTO tasks(task_id,workstream_id,task_kind,lifecycle,created_at,updated_at,metadata_json)"
            " VALUES(?,'pr-stream',?,'queued',?,?,json_object('remediation_episode_id',?))",
            (self.task_id, f"{kind}_remediation", self.now(minute), self.now(minute), self.episode_id),
        )
        self.assertTrue(
            source_sessions.bind_task(
                self.conn,
                self.claim,
                round_id=self.round_id,
                request_id=self.request_id,
                task_id=self.task_id,
                capability="development",
                now=self.now(minute),
            )
        )

    def observation(self, minute, *, mergeable=False, base_ref="main", checks=(), checks_complete=False):
        from robert_agent.work.application.repair_cycles import PullObservation

        return PullObservation(
            "pr",
            "github.com",
            "org/repo",
            7,
            self.head,
            self.base,
            base_ref,
            "open",
            False,
            mergeable,
            self.now(minute),
            checks,
            checks_complete,
        )

    def charge(self, minute, observation=None):
        from robert_agent.work.application.repair_cycles import claim_repair

        return claim_repair(
            self.conn,
            self.config,
            self.ticket,
            self.claim,
            round_id=self.round_id,
            request_id=self.request_id,
            revision=self.revision,
            episode_id=self.episode_id,
            task_id=self.task_id,
            observation=observation or self.observation(minute),
            now=self.now(minute),
        )

    def observe(self, minute, observation):
        from robert_agent.work.application.repair_cycles import apply_observation

        return apply_observation(
            self.conn,
            self.config,
            self.ticket,
            self.claim,
            revision=self.revision,
            observation=observation,
            now=self.now(minute),
        )

    def settle(self, minute, cycle_id, *, tokens=3, cost=0.5):
        from robert_agent.persistence.stores import source_sessions
        from robert_agent.work.application.repair_cycles import record_usage

        self.conn.execute("UPDATE tasks SET lifecycle='completed' WHERE task_id=?", (self.task_id,))
        attempt = f"attempt-{self.task_id}"
        self.conn.execute(
            "INSERT INTO attempts(attempt_id,task_id,attempt_no,status) VALUES(?,?,1,'completed')",
            (attempt, self.task_id),
        )
        if cycle_id:
            self.assertTrue(
                record_usage(
                    self.conn,
                    self.claim,
                    cycle_id=cycle_id,
                    attempt_id=attempt,
                    tokens=tokens,
                    cost_usd=cost,
                    now=self.now(minute),
                )
            )
        self.assertTrue(
            source_sessions.finish_round(
                self.conn,
                self.claim,
                round_id=self.round_id,
                outcome="execution_finished",
                now=self.now(minute),
            )
        )

    def test_initial_retarget_cannot_set_budget_base_or_charge(self):
        self.config["enabled"] = True
        self.attach(1, "merge_conflict", 1)
        decision = self.charge(1, self.observation(1, base_ref="unexpected-target"))
        self.assertEqual(decision.status, "base_ref_changed")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM repair_cycles WHERE scope='tapd'").fetchone(), (0,))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM repair_cycle_claims").fetchone(), (0,))
        self.assertEqual(self.charge(1).status, "claimed")
        self.assertEqual(
            self.conn.execute("SELECT authorized_base_ref FROM repair_cycles WHERE scope='tapd'").fetchone(),
            ("main",),
        )

    def test_disabled_config_cannot_charge(self):
        self.attach(1, "merge_conflict", 1)
        self.config["enabled"] = False
        self.assertEqual(self.charge(1).status, "authority_unavailable")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM repair_cycle_claims").fetchone(), (0,))

    def test_observation_predating_creation_cannot_charge(self):
        self.attach(1, "merge_conflict", 1)
        stale = replace(self.observation(1), observed_at="2026-11-14T11:59:59+00:00")
        self.assertEqual(self.observe(1, stale).status, "stale_health")
        self.assertEqual(self.charge(1, stale).status, "stale_health")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM repair_cycle_claims").fetchone(), (0,))

    def test_metadata_and_broken_creation_receipt_cannot_authorize_repair(self):
        self.attach(1, "merge_conflict", 1)
        [[saved]] = self.conn.execute(
            "SELECT remote_evidence_json FROM publication_intents WHERE publication_id=?", (self.creation_intent_id,)
        ).fetchall()
        for broken in (
            "{}",
            json.dumps({**json.loads(saved), "receipt": {**json.loads(saved)["receipt"], "proof": {}}}),
        ):
            self.conn.execute(
                "UPDATE publication_intents SET remote_evidence_json=? WHERE publication_id=?",
                (broken, self.creation_intent_id),
            )
            self.assertEqual(self.charge(1).status, "authority_unavailable")
        self.conn.execute(
            "UPDATE publication_intents SET remote_evidence_json=? WHERE publication_id=?",
            (saved, self.creation_intent_id),
        )
        for changed in ({"repo": "other/repo"}, {"pr_source_id": "other-pr"}, {"number": 8}, {"base_sha": "not-a-sha"}):
            with self.subTest(changed=changed):
                self.assertEqual(
                    self.charge(1, replace(self.observation(1), **changed)).status, "authority_unavailable"
                )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM repair_cycles").fetchone(), (0,))

    def test_missing_writer_or_disabled_repository_cannot_charge(self):
        self.attach(1, "merge_conflict", 1)
        self.conn.commit()
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.charge(1).status, "authority_unavailable")
        self.assertFalse(self.conn.in_transaction)
        self.conn.execute("BEGIN IMMEDIATE")
        self.conn.execute("UPDATE repos SET enabled=0 WHERE repo_id='repo'")
        self.assertEqual(self.charge(1).status, "authority_unavailable")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM repair_cycle_claims").fetchone(), (0,))

    def test_current_repository_identity_and_publisher_must_still_match_creation(self):
        self.attach(1, "merge_conflict", 1)
        self.conn.execute("UPDATE repos SET full_name='different/repo' WHERE repo_id='repo'")
        self.assertEqual(self.charge(1).status, "authority_unavailable")
        self.conn.execute("UPDATE repos SET full_name='org/repo',github_account='different' WHERE repo_id='repo'")
        self.assertEqual(self.charge(1).status, "authority_unavailable")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM repair_cycle_claims").fetchone(), (0,))
        self.conn.execute("UPDATE repos SET github_account='BOT' WHERE repo_id='repo'")
        self.assertEqual(self.charge(1).status, "claimed")

    def test_revoked_or_consumed_member_cannot_charge(self):
        self.attach(1, "merge_conflict", 1)
        self.conn.execute("UPDATE request_events SET state='consumed' WHERE request_id=?", (self.request_id,))
        self.assertEqual(self.charge(1).status, "authority_unavailable")
        self.facts = replace(self.facts, labels=())
        self.refresh_ticket(2)
        self.assertEqual(self.charge(2).status, "authority_unavailable")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM repair_cycle_claims").fetchone(), (0,))

    def test_retirement_of_claimed_round_does_not_hide_busy_later_source_round(self):
        from robert_agent.work.application.repair_cycles import CheckObservation

        self.attach(1, "merge_conflict", 1)
        cycle = self.charge(1)
        self.settle(2, cycle.cycle_id)
        self.attach(2, "merge_conflict", 3)
        healthy = self.observation(
            4,
            mergeable=True,
            checks_complete=True,
            checks=(CheckObservation("unit", self.head, "completed", "success"),),
        )
        self.assertEqual(self.observe(4, healthy).status, "health_waits_for_work")
        self.settle(5, None)
        self.assertEqual(self.observe(6, replace(healthy, observed_at=self.now(6))).status, "health_recovered")

    def test_wrong_saved_base_cannot_reauthorize_even_if_current_base_matches_it(self):
        self.attach(1, "merge_conflict", 1)
        cycle = self.charge(1)
        self.conn.execute(
            "UPDATE repair_cycles SET authorized_base_ref='unexpected-target' WHERE cycle_id=?", (cycle.cycle_id,)
        )
        moved = self.observation(2, base_ref="unexpected-target")
        self.assertEqual(self.charge(2, moved).status, "base_ref_changed")
        self.assertEqual(self.charge(3).status, "base_ref_changed")
        self.assertEqual(
            self.conn.execute(
                "SELECT status,authorized_base_ref,started_at FROM repair_cycles WHERE cycle_id=?", (cycle.cycle_id,)
            ).fetchone(),
            ("paused", "unexpected-target", self.now(1)),
        )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM repair_cycle_claims").fetchone(), (1,))

    def test_three_shared_claims_survive_stable_retries_and_new_head_and_base(self):
        from robert_agent.work.application.repair_cycles import CheckObservation

        self.attach(1, "merge_conflict", 1)
        first = self.charge(1)
        self.assertEqual(first.status, "claimed")
        self.assertEqual(self.charge(1).status, "already_claimed")
        self.settle(2, first.cycle_id)
        self.conn.commit()
        init_database(self.db)
        self.conn.execute("BEGIN IMMEDIATE")
        self.attach(2, "ci", 3)
        failure = CheckObservation("unit", self.head, "completed", "failure")
        self.assertEqual(
            self.charge(3, self.observation(3, checks=(failure,), checks_complete=True)).cycle_id, first.cycle_id
        )
        self.settle(4, first.cycle_id)
        self.attach(3, "merge_conflict", 5)
        self.assertEqual(self.charge(5).status, "claimed")
        self.settle(6, first.cycle_id)
        self.attach(4, "merge_conflict", 7)
        self.assertEqual(self.charge(7).status, "max_attempts")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM repair_cycle_claims").fetchone(), (3,))
        self.assertEqual(
            self.conn.execute("SELECT started_at FROM repair_cycles WHERE scope='tapd'").fetchone(), (self.now(1),)
        )

    def test_prerelease_claim_key_upgrade_preserves_mixed_rows_and_refuses_live_worker(self):
        from robert_agent.persistence.queries.repair_cycles import cycle_attempts
        from robert_agent.persistence.schema.database import schema_sql
        from robert_agent.persistence.schema.repair_cycles import has_global_claim_task_key
        from robert_agent.persistence.stores.repair_cycles import record_episode_claim
        from robert_agent.workflows.pr_remediation import load_episode

        self.attach(1, "merge_conflict", 1)
        native = self.charge(1)
        self.assertEqual(native.status, "claimed")
        self.settle(2, native.cycle_id, tokens=7)
        self.conn.executescript("""
            INSERT INTO pr_remediation_episodes(episode_id,workstream_id,episode_kind,subject_key,observed_head_sha,
                observed_base_sha,status,first_seen_at,updated_at)
                VALUES('legacy','pr-stream','merge_conflict','other:base','other','base','open','time','time');
            INSERT INTO tasks(task_id,workstream_id,task_kind,lifecycle,created_at,updated_at)
                VALUES('legacy-task','pr-stream','merge_conflict_remediation','completed','time','time');
            INSERT INTO attempts(attempt_id,task_id,attempt_no,status)
                VALUES('legacy-attempt','legacy-task',1,'completed');
            INSERT INTO repair_cycles(cycle_id,scope,episode_id,status,legacy_attempt_count,created_at,updated_at)
                VALUES('legacy','github','legacy','active',4,'time','time');
            INSERT INTO repair_cycle_claims(cycle_id,request_key,episode_id,task_id,created_at)
                VALUES('legacy','legacy-task','legacy','legacy-task','time');
        """)
        self.conn.commit()
        self.conn.execute("PRAGMA foreign_keys=OFF")
        ddl = schema_sql().partition("CREATE TABLE IF NOT EXISTS repair_cycle_claims (")[2].partition("\n);")[0]
        self.conn.executescript(
            """
            ALTER TABLE repair_cycle_claims RENAME TO saved_claims;
            CREATE TABLE repair_cycle_claims (
        """
            + ddl.replace("PRIMARY KEY(cycle_id,request_key)", "PRIMARY KEY(cycle_id,request_key), UNIQUE(task_id)")
            + """
            );
            INSERT INTO repair_cycle_claims SELECT * FROM saved_claims;
            DROP TABLE saved_claims;
            CREATE UNIQUE INDEX idx_repair_cycle_claims_native_request ON repair_cycle_claims(request_key)
                WHERE round_id IS NOT NULL;
            UPDATE attempts SET status='running' WHERE attempt_id='legacy-attempt';
        """
        )
        before = self.conn.execute("SELECT * FROM repair_cycle_claims ORDER BY cycle_id,request_key").fetchall()
        usages = self.conn.execute("SELECT * FROM repair_cycle_usage").fetchall()
        self.assertTrue(has_global_claim_task_key(self.conn))
        with self.assertRaisesRegex(RuntimeError, "Worker attempt.*legacy-attempt"):
            init_database(self.db)
        self.assertTrue(has_global_claim_task_key(self.conn))
        self.assertEqual(
            self.conn.execute("SELECT * FROM repair_cycle_claims ORDER BY cycle_id,request_key").fetchall(), before
        )
        self.conn.execute("UPDATE attempts SET status='completed' WHERE attempt_id='legacy-attempt'")
        self.conn.commit()
        for _ in range(2):
            init_database(self.db)
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.assertFalse(has_global_claim_task_key(self.conn))
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(
            self.conn.execute("SELECT * FROM repair_cycle_claims ORDER BY cycle_id,request_key").fetchall(), before
        )
        self.assertEqual(self.conn.execute("SELECT * FROM repair_cycle_usage").fetchall(), usages)
        self.assertEqual(load_episode(self.conn, "legacy")["attempt_count"], 5)
        self.assertTrue(record_episode_claim(self.conn, episode_id="legacy", task_id="legacy-task", now=self.now(3)))
        self.assertEqual(load_episode(self.conn, "legacy")["attempt_count"], 6)
        self.assertEqual(len(cycle_attempts(self.conn, "legacy")), 1)
        self.assertEqual(len(cycle_attempts(self.conn, native.cycle_id)), 1)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "UNIQUE constraint failed.*task_id"):
            self.conn.execute(
                "INSERT INTO repair_cycle_claims(cycle_id,request_key,episode_id,task_id,round_id,created_at)"
                " VALUES(?,'different-native-request',?,?,?,'time')",
                (native.cycle_id, self.episode_id, self.task_id, self.round_id),
            )

    def test_legacy_and_native_paths_cannot_both_charge_one_task(self):
        from robert_agent.persistence.stores.repair_cycles import record_episode_claim, record_native_claim
        from robert_agent.workflows.pr_remediation import load_episode

        self.attach(1, "merge_conflict", 1)
        self.assertFalse(
            record_episode_claim(self.conn, episode_id=self.episode_id, task_id=self.task_id, now=self.now(1))
        )
        native = self.charge(1)
        self.assertEqual(native.status, "claimed")
        self.assertFalse(
            record_episode_claim(self.conn, episode_id=self.episode_id, task_id=self.task_id, now=self.now(1))
        )
        self.conn.execute(
            "INSERT INTO tasks(task_id,workstream_id,task_kind,lifecycle,created_at,updated_at)"
            " VALUES('legacy-task','pr-stream','merge_conflict_remediation','completed','time','time')"
        )
        self.assertTrue(
            record_episode_claim(self.conn, episode_id=self.episode_id, task_id="legacy-task", now=self.now(2))
        )
        self.assertFalse(
            record_native_claim(
                self.conn,
                cycle_id=native.cycle_id,
                request_id="would-double-charge",
                episode_id=self.episode_id,
                task_id="legacy-task",
                round_id=self.round_id,
                now=self.now(2),
            )
        )
        self.assertEqual(load_episode(self.conn, self.episode_id)["attempt_count"], 2)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM repair_cycle_claims").fetchone(), (2,))

    def test_retarget_sticky_pause_and_stale_health_do_not_reset_budget(self):
        from robert_agent.work.application.repair_cycles import CheckObservation

        self.attach(1, "merge_conflict", 1)
        cycle = self.charge(1)
        stale = self.observation(
            0,
            mergeable=True,
            checks_complete=True,
            checks=(CheckObservation("unit", self.head, "completed", "success"),),
        )
        self.assertEqual(self.observe(2, stale).status, "stale_health")
        self.assertEqual(self.observe(2, self.observation(2, base_ref="release")).status, "base_ref_changed")
        good = self.observation(
            3,
            mergeable=True,
            checks_complete=True,
            checks=(CheckObservation("unit", self.head, "completed", "success"),),
        )
        self.assertEqual(self.observe(3, good).status, "paused")
        self.assertEqual(self.charge(4).status, "paused")
        self.assertEqual(
            self.conn.execute(
                "SELECT cycle_id,authorized_base_ref,status FROM repair_cycles WHERE scope='tapd'"
            ).fetchone(),
            (cycle.cycle_id, "main", "paused"),
        )

    def test_missing_measurement_and_waiting_clock_prevent_new_claim(self):
        from robert_agent.work.application.repair_cycles import CheckObservation

        self.attach(1, "merge_conflict", 1)
        cycle = self.charge(1)
        self.settle(2, cycle.cycle_id, tokens=None)
        self.attach(2, "ci", 3)
        check = CheckObservation("unit", self.head, "completed", "failure")
        self.assertEqual(
            self.charge(3, self.observation(3, checks=(check,), checks_complete=True)).status,
            "usage_unavailable",
        )
        self.assertEqual(
            self.charge(22, self.observation(22, checks=(check,), checks_complete=True)).status,
            "max_wall_minutes",
        )

    def test_health_tracks_without_pending_request_and_requires_all_current_and_initial_checks(self):
        from robert_agent.work.application.repair_cycles import CheckObservation

        self.attach(1, "merge_conflict", 1)
        cycle = self.charge(1)
        initial = CheckObservation("unit", self.head, "completed", "success")
        self.assertEqual(
            self.observe(2, self.observation(2, mergeable=True, checks=(initial,), checks_complete=True)).status,
            "health_waits_for_work",
        )
        self.settle(3, cycle.cycle_id)
        self.conn.execute("UPDATE request_events SET state='consumed' WHERE request_id=?", (self.request_id,))
        self.policy["ci"]["check_allowlist"] = ["unit", "style"]
        self.refresh_ticket(4)
        mismatched = CheckObservation("style", "older-head", "completed", "success")
        partial = self.observation(4, mergeable=True, checks_complete=True, checks=(initial, mismatched))
        self.assertEqual(self.observe(4, partial).status, "repair_pending")
        self.assertEqual(
            self.observe(4, replace(partial, checks=(initial, replace(mismatched, head_sha=self.head)))).status,
            "stale_health",
        )
        complete = replace(partial, observed_at=self.now(5), checks=(initial, replace(mismatched, head_sha=self.head)))
        self.assertEqual(self.observe(5, complete).status, "health_recovered")
        self.assertEqual(
            self.conn.execute("SELECT status,close_reason FROM repair_cycles WHERE scope='tapd'").fetchone(),
            ("closed", "health_recovered"),
        )
        self.attach(2, "merge_conflict", 6)
        self.assertEqual(self.charge(6, self.observation(6, base_ref="different")).status, "base_ref_changed")
        next_cycle = self.charge(7)
        self.assertEqual(next_cycle.status, "claimed")
        self.assertNotEqual(next_cycle.cycle_id, cycle.cycle_id)

    def test_tightening_limits_and_gates_survives_later_config_increase(self):
        self.attach(1, "merge_conflict", 1)
        cycle = self.charge(1)
        self.policy["conflict"]["max_wall_minutes"] = 2
        self.policy["ci"]["enabled"] = False
        self.refresh_ticket(2)
        self.assertEqual(self.observe(2, self.observation(2)).status, "repair_pending")
        self.policy["conflict"]["max_wall_minutes"] = 20
        self.policy["ci"]["enabled"] = True
        self.refresh_ticket(3)
        self.assertEqual(self.charge(3).status, "max_wall_minutes")
        row = self.conn.execute(
            "SELECT policy_json,policy_cap_json FROM repair_cycles WHERE cycle_id=?", (cycle.cycle_id,)
        ).fetchone()
        self.assertEqual(json.loads(row[0])["conflict"]["max_wall_minutes"], 10)
        self.assertEqual(json.loads(row[1])["conflict"]["max_wall_minutes"], 2)
        self.assertFalse(json.loads(row[1])["ci"]["enabled"])

    def test_native_claim_cannot_cross_fence_host_or_bound_episode(self):
        from robert_agent.work.application.repair_cycles import claim_repair

        self.attach(1, "merge_conflict", 1)
        self.assertEqual(self.charge(1, replace(self.observation(1), host="elsewhere")).status, "authority_unavailable")
        valid = self.claim
        self.claim = replace(self.claim, token=self.claim.token + 1)
        self.assertEqual(self.charge(1).status, "authority_unavailable")
        self.claim = valid
        reported = claim_repair(
            self.conn,
            self.config,
            self.ticket,
            self.claim,
            round_id=self.round_id,
            request_id=self.request_id,
            revision=self.revision,
            episode_id="unrelated",
            task_id=self.task_id,
            observation=self.observation(1),
            now=self.now(1),
        )
        self.assertEqual(reported.status, "episode_unavailable")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM repair_cycles").fetchone(), (0,))
