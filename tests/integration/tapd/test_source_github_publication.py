"""Exercise native GitHub publication from accepted audited local Git results."""

from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

from robert_agent.adapters.tapd.client import TapdError
from robert_agent.adapters.tapd.publication import TapdPublisherIdentity
from robert_agent.delivery import native_contract, tapd_policy
from robert_agent.delivery.application import source_completion, source_publication_queue
from robert_agent.delivery.application.source_publication import publish_tapd_action
from robert_agent.execution import workspace
from robert_agent.foundation.values import utc_now
from robert_agent.work import tapd_authority as tapd_facts
from robert_agent.persistence import database
from robert_agent.persistence.queries import source_github_publication
from tests.support.source_github_lifecycle import SourceGitHubLifecycleFixture


class SourceGitHubPublicationTests(SourceGitHubLifecycleFixture):
    def test_accepted_native_push_and_pr_record_positive_independent_receipts(self):
        self.accepted_development()
        original = self.read("SELECT action_id,metadata_json,target_json FROM github_actions ORDER BY action_id")
        calls, _, _, run = self.runner()
        blocked = self.publish("open_pr", run)
        self.assertEqual((blocked.status, blocked.reason, calls), ("deferred", "required_predecessor_unconfirmed", []))
        pushed = self.publish("push_existing_pr", run)
        self.assertEqual(pushed.status, "published", pushed)
        opened = self.publish("open_pr", run)
        self.assertEqual(opened.status, "published", opened)
        self.assertEqual(opened.receipt.url, "https://github.com/org/alpha/pull/17")
        self.assertEqual(
            self.read("SELECT action_id,metadata_json,target_json FROM github_actions ORDER BY action_id"), original
        )
        self.assertEqual(self.read("SELECT COUNT(*) FROM github_publication_preparations"), [(2,)])
        with closing(database.connect(self.db)) as conn:
            self.assertEqual(
                source_github_publication.confirmed_effect(conn, self.root_id, self.action_ids["open_pr"]),
                opened.receipt,
            )
        before = len(calls)
        self.assertEqual(self.publish("open_pr", run, observation_provider=None).status, "deduplicated")
        self.assertEqual(len(calls), before)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE github_actions SET target_url='https://github.com/org/other/pull/17' WHERE action_id=?",
                (self.action_ids["open_pr"],),
            )
        with closing(database.connect(self.db)) as conn:
            self.assertIsNone(
                source_github_publication.confirmed_effect(conn, self.root_id, self.action_ids["open_pr"])
            )

    def test_partial_development_requires_canonical_github_receipts_before_dormancy(self):
        self.accepted_development()
        calls, _, _, run = self.runner()
        self.assertEqual(self.publish("push_existing_pr", run).status, "published")
        self.assertEqual(self.publish("open_pr", run).status, "published")
        root_id = self.root_id
        open_action_id = self.action_ids["open_pr"]
        proof = self.read("SELECT action_id,status,remote_evidence_json FROM publication_intents ORDER BY action_id")
        saved = next(remote for action_id, _, remote in proof if action_id == open_action_id)
        bad = json.loads(saved)
        bad["receipt"]["request_fingerprint"] = "mismatch"
        self.labels = ()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE publication_intents SET remote_evidence_json=? WHERE action_id=?",
                (json.dumps(bad), open_action_id),
            )
        self.assertEqual(self.tick("held", ticket=self.ticket).reason, "source_publication_settlement_required")
        self.assertEqual(self.read("SELECT COUNT(*) FROM source_delivery_pauses"), [(0,)])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE publication_intents SET remote_evidence_json=? WHERE action_id=?", (saved, open_action_id)
            )
        paused = self.tick("canceled", ticket=self.ticket)
        self.assertEqual((paused.reason, paused.identity), ("accepted_delivery_paused", root_id))
        self.assertEqual(
            self.read("SELECT action_id,status,remote_evidence_json FROM publication_intents ORDER BY action_id"), proof
        )
        [[encoded]] = self.read("SELECT proof_json FROM source_delivery_pauses WHERE source_result_id=?", (root_id,))
        dispositions = [item["disposition"] for item in json.loads(encoded)["effects"]]
        self.assertEqual(dispositions, ["confirmed", "confirmed", "local_never_started"])
        before = len(calls)
        self.assertEqual(self.publish("open_pr", run).status, "deduplicated")
        self.assertEqual(len(calls), before)

    def _recovery_comment(self, identifier, body, principal):
        version = tapd_facts.CommentVersion(
            self.ticket, "bug", str(identifier), utc_now(), None, sha256(body.encode()).hexdigest()
        )
        comment = tapd_facts.CommentObservation(
            version,
            body,
            principal,
            tapd_facts.CreatorEvidence(version, principal, "creator"),
            tapd_facts.OwnBodyEvidence(version, principal, "own-body"),
        )
        self.comments.append(comment)
        return comment

    def _recovery_tapd_client(self, run, writes):
        fixture = self

        class Client:
            def __init__(self, **_):
                pass

            def add_comment(self, workspace_id, kind, external_id, body, *, stop):
                fixture.assertEqual((workspace_id, kind, external_id), fixture.ticket.key[1:])
                identifier = str(10000 + len(fixture.comments))
                writes.append(body)
                fixture._recovery_comment(identifier, body, "service-publisher")
                if getattr(fixture, "write_unknown", False):
                    fixture.write_unknown = False
                    raise TapdError("timeout", effect_unknown=True)
                return identifier

        def publisher(_client, ticket, **_):
            return TapdPublisherIdentity(
                ticket.host, ticket.workspace_id, "service-publisher", "publisher-proof", utc_now()
            )

        def send(root, action):
            return publish_tapd_action(
                fixture.db,
                fixture.configured,
                source_result_id=root,
                action_id=action,
                owner="test-coordinator",
                client_factory=Client,
                observation_provider=fixture.provider,
                identity_provider=publisher,
                github_runner=run,
            )

        return send, publisher

    def test_public_missing_tapd_recovery_reconciles_unknown_and_completes_after_restart(self):
        self.accepted_development()
        calls, state, _, run = self.runner()
        self.assertEqual(self.publish("push_existing_pr", run).status, "published")
        self.assertEqual(self.publish("open_pr", run).status, "published")
        settled_github = self.read("SELECT * FROM publication_intents ORDER BY action_id")
        ledger = self.read("SELECT * FROM request_events")
        old_root = self.read("SELECT * FROM source_results WHERE source_result_id=?", (self.root_id,))
        started = len(self.started)
        before = sum(call[:2] == ("git", "push") or call[:3] == ("gh", "pr", "create") for call in calls)
        [[action]] = self.read(
            "SELECT a.action_id FROM source_required_actions a JOIN github_actions g ON g.action_id=a.action_id "
            "WHERE a.source_result_id=? AND g.provider='tapd'",
            (self.root_id,),
        )
        writes = []
        send, publisher = self._recovery_tapd_client(run, writes)
        self.labels = ()
        paused = self.tick("canceled", ticket=self.ticket)
        cancellation = self.read("SELECT * FROM source_round_cancellations")
        self.assertEqual(send(self.root_id, action).status, "deferred")
        self.assertFalse(writes)
        self.labels = ("robot",)
        notice = self.tick("publication_pending", ticket=self.ticket)
        [[question, version, notice_action]] = self.read(
            "SELECT question_id,version,action_id FROM source_confirmation_questions WHERE source_result_id=?",
            (notice.identity,),
        )
        self.assertEqual(send(notice.identity, notice_action).status, "published")
        self._recovery_comment(207, f"@test-helper ROBERT-CHOICE {question} v{version} resume", "requester")

        state["sha"] = "0" * 40
        held = self.tick("attention", ticket=self.ticket, github_discussion_runner=run)
        self.assertEqual(held.reason, "source_delivery_reprocess_required")
        self.assertEqual(self.read("SELECT consumer_id FROM source_confirmation_answers"), [(None,)])
        state["sha"] = self.head

        def incomplete(command, **options):
            result = run(command, **options)
            facts = json.loads(result.stdout)
            facts.pop("merged")
            return SimpleNamespace(returncode=0, stdout=json.dumps(facts), stderr="")

        incomplete_result = self.tick("held", ticket=self.ticket, github_discussion_runner=incomplete)
        self.assertEqual(incomplete_result.reason, "source_delivery_observation_unavailable")
        self.assertEqual(self.read("SELECT consumer_id FROM source_confirmation_answers"), [(None,)])

        def supersede_fence(command, **options):
            result = run(command, **options)
            # A different claim token arrives while the writer is released for the GitHub read.
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute("UPDATE source_sessions SET token=token+1 WHERE source_id=?", (self.ticket.source_id,))
            return result

        raced = self.tick("held", ticket=self.ticket, github_discussion_runner=supersede_fence)
        self.assertEqual(raced.reason, "source_delivery_recovery_changed")
        self.assertEqual(self.read("SELECT consumer_id FROM source_confirmation_answers"), [(None,)])
        self.assertEqual(self.read("SELECT COUNT(*) FROM source_delivery_recoveries"), [(0,)])

        self.assertEqual(
            self.tick("publication_pending", ticket=self.ticket, github_discussion_runner=run).identity, self.root_id
        )
        with closing(database.connect(self.db)) as conn:
            actions, _ = source_publication_queue.source_publication_page(
                conn,
                self.configured,
                source_owner="test-coordinator",
                limit=5,
                tapd_observation_provider=self.provider,
                tapd_identity_provider=publisher,
            )
        self.assertEqual([(row["provider"], row["action_id"]) for row in actions], [("tapd", action)])
        self.write_unknown = True
        self.assertEqual(send(self.root_id, action).status, "unknown")
        before_readback = len(calls)
        with patch.object(
            source_completion,
            "complete_source_result",
            return_value=source_completion.CompletionOutcome("held", "restart_test"),
        ):
            self.assertEqual(send(self.root_id, action).status, "published")
        self.assertEqual(len(calls), before_readback)
        self.assertEqual(self.read("SELECT completed_at FROM source_delivery_recoveries"), [(None,)])
        self.assertEqual(self.tick("publication_pending", ticket=self.ticket).identity, self.root_id)
        with closing(database.connect(self.db)) as conn:
            _, completions = source_publication_queue.source_publication_page(
                conn,
                self.configured,
                source_owner="test-coordinator",
                limit=0,
            )
        [candidate] = [row for row in completions if row["source_result_id"] == self.root_id]
        finalized = source_publication_queue.recover_source_completion(
            self.db, self.configured, candidate, source_owner="test-coordinator"
        )
        self.assertEqual(finalized.status, "completed")
        self.assertEqual(send(self.root_id, action).status, "deduplicated")
        self.assertEqual(
            self.read("SELECT status FROM source_rounds WHERE round_id=?", (paused.round_id,)), [("canceled",)]
        )
        self.assertEqual(self.read("SELECT * FROM source_round_cancellations"), cancellation)
        self.assertEqual(self.read("SELECT * FROM request_events"), ledger)
        self.assertEqual(self.read("SELECT * FROM source_results WHERE source_result_id=?", (self.root_id,)), old_root)
        self.assertEqual(
            self.read(
                "SELECT i.* FROM publication_intents i JOIN github_actions a ON a.action_id=i.action_id "
                "WHERE a.provider='github' ORDER BY i.action_id"
            ),
            settled_github,
        )
        self.assertEqual(len(self.started), started)
        self.assertEqual(len(writes), 2)
        self.assertEqual(
            sum(call[:2] == ("git", "push") or call[:3] == ("gh", "pr", "create") for call in calls), before
        )

    def _recovery_after_independent_question(self, *, prepare_child, retire=False):
        from copy import deepcopy

        self.accepted_development()
        _, _, _, run = self.runner()
        self.assertEqual(self.publish("push_existing_pr", run).status, "published")
        self.assertEqual(self.publish("open_pr", run).status, "published")
        [[action]] = self.read(
            "SELECT a.action_id FROM source_required_actions a JOIN github_actions g ON g.action_id=a.action_id "
            "WHERE a.source_result_id=? AND g.provider='tapd'",
            (self.root_id,),
        )
        send, _ = self._recovery_tapd_client(run, [])
        self.labels = ()
        self.tick("canceled", ticket=self.ticket)

        if not prepare_child:
            candidate = deepcopy(self.configured["repos"][0])
            candidate.update(
                full_name="org/beta", repo_root=str(self.root / "beta"), worktree_root=str(self.root / "beta-trees")
            )
            (Path(candidate["repo_root"]) / ".git").mkdir(parents=True)
            (Path(candidate["repo_root"]) / "README.md").write_text("Second repository evidence")
            self.configured["repos"].append(candidate)
            self.configured["tapd"]["projects"][0]["repositories"].append(candidate["full_name"])
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute(
                    "INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        "repo:org/beta",
                        candidate["full_name"],
                        candidate["github_account"],
                        candidate["default_base_branch"],
                        candidate["repo_root"],
                        candidate["worktree_root"],
                    ),
                )
        trigger = self._recovery_comment(206, "@test-helper Investigate another component.", "requester")
        self.comments = [trigger, *self.comments[:-1]]
        self.model_kind = "question"
        future = self.tick("admitted", ticket=self.ticket)
        if prepare_child:
            self.tick("prepared")
            self.tick("dispatched")
            checkout = Path(self.configured["repos"][0]["repo_root"])
            self.submit(
                "local_result",
                evidence=[
                    {
                        "repo_id": "repo:org/alpha",
                        "path": "README.md",
                        "revision": "sha256:" + sha256((checkout / "README.md").read_bytes()).hexdigest(),
                    }
                ],
            )
            self.tick("waiting")
            self.tick("audited")
        self.tick("stage_recorded")
        answer = self.tick("publication_pending")
        [[question_action]] = self.read(
            "SELECT action_id FROM source_required_actions WHERE source_result_id=?", (answer.identity,)
        )
        self.assertEqual(send(answer.identity, question_action).completion.status, "completed")
        self.assertEqual(
            bool(self.read("SELECT task_id FROM source_round_tasks WHERE round_id=?", (future.round_id,))),
            prepare_child,
        )
        newer_item = self.read("SELECT * FROM work_items WHERE repo_id='repo:org/alpha'")

        self.labels = ("robot",)
        notice = self.tick("publication_pending", ticket=self.ticket)
        [[question, version, notice_action]] = self.read(
            "SELECT question_id,version,action_id FROM source_confirmation_questions WHERE source_result_id=?",
            (notice.identity,),
        )
        self.assertEqual(send(notice.identity, notice_action).status, "published")
        self._recovery_comment(207, f"@test-helper ROBERT-CHOICE {question} v{version} resume", "requester")
        self.tick("publication_pending", ticket=self.ticket, github_discussion_runner=run)
        if retire:
            from robert_agent.persistence import transactions
            from robert_agent.persistence.queries import source_delivery_pauses
            from robert_agent.work import source_attention

            with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
                transactions.begin_immediate(conn)
                pause = source_delivery_pauses.latest(conn, self.root_id)
                self.assertTrue(
                    source_attention.open_delivery_recovery(
                        conn, pause, reason="source_delivery_reprocess_required", now=utc_now()
                    )
                )
            ledger = self.read("SELECT * FROM request_events")
            original = self.read("SELECT * FROM publication_intents ORDER BY action_id")
            self.terminal = True
            self.assertEqual(self.tick("canceled", ticket=self.ticket).reason, "accepted_delivery_retired")
            self.assertEqual(
                self.read("SELECT completed_at,suspended_at IS NOT NULL FROM source_delivery_recoveries"), [(None, 1)]
            )
            self.assertEqual(
                self.read(
                    "SELECT status FROM notifications WHERE notification_type='source_delivery_recovery_attention'"
                ),
                [("resolved",)],
            )
            self.assertEqual(self.read("SELECT * FROM request_events"), ledger)
            self.assertEqual(self.read("SELECT * FROM publication_intents ORDER BY action_id"), original)
            self.assertEqual(
                self.read("SELECT publish_status FROM github_actions WHERE action_id=?", (action,)), [("skipped",)]
            )
            self.assertEqual(send(self.root_id, action).status, "deferred")
            self.assertEqual(self.read("SELECT * FROM work_items WHERE repo_id='repo:org/alpha'"), newer_item)
            self.assertEqual(
                self.read("SELECT status FROM source_rounds WHERE round_id=?", (future.round_id,)),
                [("waiting_for_user",)],
            )
            return
        self.assertEqual(send(self.root_id, action).completion.status, "completed")
        [[completed]] = self.read("SELECT completed_at FROM work_items WHERE repo_id='repo:org/alpha'")
        if prepare_child:
            self.assertEqual(self.read("SELECT * FROM work_items WHERE repo_id='repo:org/alpha'"), newer_item)
        else:
            self.assertIsNotNone(completed)
        self.assertEqual(
            self.read("SELECT status FROM source_rounds WHERE round_id=?", (future.round_id,)), [("waiting_for_user",)]
        )

    def test_recovery_completes_item_when_new_question_only_considered_its_repository(self):
        self._recovery_after_independent_question(prepare_child=False)

    def test_recovery_preserves_item_owned_by_newer_actual_readonly_work(self):
        self._recovery_after_independent_question(prepare_child=True)

    def test_permanent_revocation_retires_recovering_root_without_overwriting_newer_item(self):
        self._recovery_after_independent_question(prepare_child=True, retire=True)

    def _recover_missing_github_prefix(self, *, push_already_confirmed):
        self.accepted_development()
        calls, _, _, run = self.runner(pr_edit=lambda pr: pr.update(state="open"))
        if push_already_confirmed:
            self.assertEqual(self.publish("push_existing_pr", run).status, "published")
        previous = self.read("SELECT * FROM publication_intents ORDER BY action_id")
        ledger = self.read("SELECT * FROM request_events")
        started = len(self.started)
        self.labels = ()
        old = self.tick("canceled", ticket=self.ticket)
        send, publisher = self._recovery_tapd_client(run, [])
        self.labels = ("robot",)
        notice = self.tick("publication_pending", ticket=self.ticket)
        [[question, version, action]] = self.read(
            "SELECT question_id,version,action_id FROM source_confirmation_questions WHERE source_result_id=?",
            (notice.identity,),
        )
        self.assertEqual(send(notice.identity, action).status, "published")
        self._recovery_comment(207, f"@test-helper ROBERT-CHOICE {question} v{version} resume", "requester")
        self.assertEqual(
            self.tick("publication_pending", ticket=self.ticket, github_discussion_runner=run).identity, self.root_id
        )
        with closing(database.connect(self.db)) as conn:
            candidates, _ = source_publication_queue.source_publication_page(
                conn,
                self.configured,
                source_owner="test-coordinator",
                limit=5,
                tapd_observation_provider=self.provider,
                tapd_identity_provider=publisher,
            )
        expected = "open_pr" if push_already_confirmed else "push_existing_pr"
        self.assertEqual(
            [(row["provider"], row["action_id"]) for row in candidates], [("github", self.action_ids[expected])]
        )
        if not push_already_confirmed:
            self.assertEqual(self.publish("push_existing_pr", run).status, "published")
        self.assertEqual(self.publish("open_pr", run).status, "published")
        [[tapd_action]] = self.read(
            "SELECT a.action_id FROM source_required_actions a JOIN github_actions g ON g.action_id=a.action_id "
            "WHERE a.source_result_id=? AND g.provider='tapd'",
            (self.root_id,),
        )
        self.assertEqual(send(self.root_id, tapd_action).completion.status, "completed")
        self.assertEqual(self.read("SELECT * FROM request_events"), ledger)
        self.assertEqual(len(self.started), started)
        self.assertEqual(sum(call[:2] == ("git", "push") for call in calls), 1)
        self.assertEqual(sum(call[:3] == ("gh", "pr", "create") for call in calls), 1)
        self.assertEqual(
            self.read("SELECT status FROM source_rounds WHERE round_id=?", (old.round_id,)), [("canceled",)]
        )
        if previous:
            self.assertEqual(
                self.read(
                    "SELECT * FROM publication_intents WHERE action_id=?", (self.action_ids["push_existing_pr"],)
                ),
                previous,
            )

    def test_recovery_publishes_missing_github_effects_without_reexecuting_worker(self):
        self._recover_missing_github_prefix(push_already_confirmed=False)

    def test_recovery_preserves_confirmed_push_and_creates_only_missing_pr(self):
        self._recover_missing_github_prefix(push_already_confirmed=True)

    def _offer_missing_github_recovery(self, run):
        self.labels = ()
        self.tick("canceled", ticket=self.ticket)
        send, _ = self._recovery_tapd_client(run, [])
        self.labels = ("robot",)
        notice = self.tick("publication_pending", ticket=self.ticket)
        [[question, version, action]] = self.read(
            "SELECT question_id,version,action_id FROM source_confirmation_questions WHERE source_result_id=?",
            (notice.identity,),
        )
        self.assertEqual(send(notice.identity, action).status, "published")
        self._recovery_comment(207, f"@test-helper ROBERT-CHOICE {question} v{version} resume", "requester")

    def test_missing_github_recovery_rechecks_remote_base_before_claim_and_effect(self):
        self.accepted_development()
        calls, _, _, run = self.runner()
        self._offer_missing_github_recovery(run)
        divergent = True

        def observe(command, **options):
            self.assertNotIn("stop", options)
            result = run(command, **options)
            if command[-1] == "repos/org/alpha/git/matching-refs/heads/main" and divergent:
                value = json.loads(result.stdout)
                value[0]["object"]["sha"] = "0" * 40
                return SimpleNamespace(returncode=0, stdout=json.dumps(value), stderr="")
            return result

        held = self.tick("attention", ticket=self.ticket, github_discussion_runner=observe)
        self.assertEqual(held.reason, "source_delivery_reprocess_required")
        notices = self.read(
            "SELECT notification_id,status FROM notifications WHERE notification_type='source_delivery_recovery_attention'"
        )
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0][1], "recorded")
        self.assertEqual(self.read("SELECT consumer_id FROM source_confirmation_answers"), [(None,)])
        self.assertFalse(self.read("SELECT * FROM source_delivery_recoveries"))
        divergent = False
        self.tick("publication_pending", ticket=self.ticket, github_discussion_runner=observe)
        divergent = True
        self.assertEqual(self.publish("push_existing_pr", observe).reason, "source_delivery_reprocess_required")
        self.assertFalse(
            self.read("SELECT * FROM publication_intents WHERE action_id=?", (self.action_ids["push_existing_pr"],))
        )
        self.assertFalse([call for call in calls if call[:2] == ("git", "push")])

    def test_missing_github_recovery_final_guard_rejects_changed_claim(self):
        self.accepted_development()
        calls, _, _, run = self.runner()
        self._offer_missing_github_recovery(run)
        self.tick("publication_pending", ticket=self.ticket, github_discussion_runner=run)
        changed = False

        def supersede(command, **options):
            nonlocal changed
            result = run(command, **options)
            if command[-1] == "repos/org/alpha/git/matching-refs/heads/main":
                started = self.read(
                    "SELECT status FROM publication_intents WHERE action_id=?", (self.action_ids["push_existing_pr"],)
                )
                if started == [("executing",)] and not changed:
                    changed = True
                    with closing(sqlite3.connect(self.db)) as conn, conn:
                        conn.execute(
                            "UPDATE source_sessions SET token=token+1 WHERE source_id=?", (self.ticket.source_id,)
                        )
            return result

        self.assertNotEqual(self.publish("push_existing_pr", supersede).status, "published")
        self.assertTrue(changed)
        self.assertFalse([call for call in calls if call[:2] == ("git", "push")])
        self.assertEqual(
            self.read(
                "SELECT status FROM publication_intents WHERE action_id=?", (self.action_ids["push_existing_pr"],)
            ),
            [("executing",)],
        )

    def test_recovery_losing_authority_again_pauses_remaining_effects_for_new_choice(self):
        self.accepted_development()
        calls, _, _, run = self.runner(pr_edit=lambda pr: pr.update(state="open"))
        self._offer_missing_github_recovery(run)
        self.tick("publication_pending", ticket=self.ticket, github_discussion_runner=run)
        self.assertEqual(self.publish("push_existing_pr", run).status, "published")
        pushed = self.read(
            "SELECT * FROM publication_intents WHERE action_id=?", (self.action_ids["push_existing_pr"],)
        )
        worker_count = len(self.started)
        cancellation = self.read("SELECT * FROM source_round_cancellations")
        self.labels = ()
        self.assertEqual(self.tick("held", ticket=self.ticket).reason, "accepted_delivery_repaused")
        self.assertEqual(self.read("SELECT generation FROM source_delivery_pauses ORDER BY generation"), [(1,), (2,)])
        self.assertEqual(
            self.read("SELECT COUNT(*) FROM source_delivery_recoveries WHERE suspended_at IS NOT NULL"), [(1,)]
        )
        self.assertNotEqual(self.publish("open_pr", run).status, "published")
        self.labels = ("robot",)
        notice = self.tick("publication_pending", ticket=self.ticket)
        [[question, version, action]] = self.read(
            "SELECT question_id,version,action_id FROM source_confirmation_questions WHERE source_result_id=?",
            (notice.identity,),
        )
        self.assertEqual(version, 2)
        send, _ = self._recovery_tapd_client(run, [])
        self.assertEqual(send(notice.identity, action).status, "published")
        self.assertNotEqual(self.tick("held", ticket=self.ticket).identity, self.root_id)
        self._recovery_comment(208, f"@test-helper ROBERT-CHOICE {question} v{version} resume", "requester")
        self.assertEqual(
            self.tick("publication_pending", ticket=self.ticket, github_discussion_runner=run).identity, self.root_id
        )
        self.assertEqual(self.publish("open_pr", run).status, "published")
        [[tapd_action]] = self.read(
            "SELECT r.action_id FROM source_required_actions r JOIN github_actions a ON a.action_id=r.action_id WHERE r.source_result_id=? AND a.provider='tapd'",
            (self.root_id,),
        )
        self.assertEqual(send(self.root_id, tapd_action).completion.status, "completed")
        self.assertEqual(
            self.read("SELECT * FROM publication_intents WHERE action_id=?", (self.action_ids["push_existing_pr"],)),
            pushed,
        )
        self.assertEqual(self.read("SELECT * FROM source_round_cancellations"), cancellation)
        self.assertEqual(len(self.started), worker_count)
        self.assertEqual(sum(call[:2] == ("git", "push") for call in calls), 1)
        self.assertEqual(sum(call[:3] == ("gh", "pr", "create") for call in calls), 1)

    def test_recovery_keeps_unknown_effect_owned_until_positive_readback(self):
        self.accepted_development()
        calls, state, _, run = self.runner(pr_edit=lambda pr: pr.update(state="open"))
        self._offer_missing_github_recovery(run)
        self.tick("publication_pending", ticket=self.ticket, github_discussion_runner=run)

        def hide_readback(command, **options):
            result = run(command, **options)
            if "repos/bot/alpha/git/matching-refs/" in command[-1] and state["sha"]:
                return SimpleNamespace(returncode=0, stdout="[]", stderr="")
            return result

        self.assertEqual(self.publish("push_existing_pr", hide_readback).status, "unknown")
        self.labels = ()
        self.tick("publication_pending", ticket=self.ticket)
        self.assertEqual(self.read("SELECT generation FROM source_delivery_pauses"), [(1,)])
        self.assertEqual(self.read("SELECT suspended_at FROM source_delivery_recoveries"), [(None,)])
        self.assertEqual(self.publish("push_existing_pr", run).status, "published")
        self.assertEqual(self.tick("held", ticket=self.ticket).reason, "accepted_delivery_repaused")
        self.assertEqual(sum(call[:2] == ("git", "push") for call in calls), 1)
        self.assertEqual(self.read("SELECT generation FROM source_delivery_pauses ORDER BY generation"), [(1,), (2,)])

    def test_changed_configured_publisher_blocks_new_effect_from_old_accepted_result(self):
        self.accepted_development()
        self.configured["repos"][0]["github_account"] = "replacement"
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE repos SET github_account='replacement'")
        calls, _, _, run = self.runner()
        outcome = self.publish("push_existing_pr", run)
        self.assertNotEqual(outcome.status, "published", outcome)
        self.assertFalse([cmd for cmd in calls if cmd[:2] == ("git", "push")])

    def test_unknown_remote_push_reconciles_after_source_revocation_and_local_remote_change(self):
        self.accepted_development()
        calls, state, _, run = self.runner()
        lost = False

        def unreliable(command, **kwargs):
            nonlocal lost
            if command[:2] == ["git", "push"]:
                run(command, **kwargs)
                lost = True
                raise subprocess.TimeoutExpired(command, 20)
            if lost and command[:2] == ["gh", "api"]:
                raise OSError("simulated readback outage")
            return run(command, **kwargs)

        first = self.publish("push_existing_pr", unreliable)
        self.assertEqual(first.status, "unknown", first)
        self.assertEqual(state["sha"], self.head)
        self.assertEqual(self.read("SELECT status FROM publication_intents"), [("attention",)])
        self.terminal = True
        self.assertEqual(self.tick("held", ticket=self.ticket).reason, "source_publication_settlement_required")
        revoked = self.publish("open_pr", run)
        self.assertEqual(revoked.status, "deferred")
        self.git(self.tree, "remote", "set-url", "origin", "https://github.com/other/alpha.git")
        recovered = self.publish("push_existing_pr", run, observation_provider=None)
        self.assertEqual(recovered.status, "published", recovered)
        self.assertEqual(len([cmd for cmd in calls if cmd[:2] == ("git", "push")]), 1)

        self.assertEqual(self.tick("canceled", ticket=self.ticket).reason, "accepted_delivery_retired")
        self.assertEqual(
            self.read("SELECT action_type,publish_status FROM github_actions ORDER BY action_type"),
            [("comment", "skipped"), ("open_pr", "skipped"), ("push_existing_pr", "published")],
        )
        self.assertEqual(self.read("SELECT status FROM publication_intents"), [("published",)])
        self.assertEqual(self.publish("open_pr", run).status, "deferred")
        self.assertNotIn(("gh", "pr", "create"), [cmd[:3] for cmd in calls])

    def test_terminal_retirement_preserves_prepared_github_packet_without_a_write(self):
        from robert_agent.persistence import execution_control
        from robert_agent.persistence.queries import source_retirement

        self.accepted_development()
        calls, _, _, run = self.runner()
        self.assertEqual(self.publish("push_existing_pr", run).status, "published")
        transition = execution_control.transition_publication_intent

        def preserve_prepared(conn, publication_id, status, now, **fields):
            if status == "executing":
                return None
            return transition(conn, publication_id, status, now, **fields)

        with patch.object(execution_control, "transition_publication_intent", side_effect=preserve_prepared):
            self.assertEqual(self.publish("open_pr", run).status, "unknown")
        action = self.action_ids["open_pr"]
        [[fingerprint, packet]] = self.read(
            """SELECT i.request_fingerprint,p.prepared_evidence_json
               FROM publication_intents i JOIN github_publication_preparations p USING(publication_id)
               WHERE i.action_id=? AND i.status='prepared' AND i.started_at IS NULL""",
            (action,),
        )
        self.terminal = True
        self.assertEqual(self.tick("canceled", ticket=self.ticket).reason, "accepted_delivery_retired")
        self.assertEqual(
            self.read(
                "SELECT status,request_fingerprint,started_at,remote_evidence_json FROM publication_intents WHERE action_id=?",
                (action,),
            ),
            [("failed", fingerprint, None, "{}")],
        )
        self.assertEqual(
            self.read(
                "SELECT prepared_evidence_json FROM github_publication_preparations p "
                "JOIN publication_intents i USING(publication_id) WHERE i.action_id=?",
                (action,),
            ),
            [(packet,)],
        )
        with closing(database.connect(self.db)) as conn:
            self.assertTrue(source_retirement.is_retired(conn, self.root_id))
        self.assertEqual(self.publish("open_pr", run).status, "deferred")
        self.assertFalse([call for call in calls if call[:3] == ("gh", "pr", "create")])

    def test_acknowledged_pr_with_wrong_body_stays_unknown_through_later_reconciliation(self):
        self.accepted_development()
        calls, _, pulls, run = self.runner(pr_edit=lambda pr: pr.update(body=pr["body"] + "\nPrivate injected prose"))
        self.assertEqual(self.publish("push_existing_pr", run).status, "published")
        first = self.publish("open_pr", run)
        self.assertEqual(first.status, "unknown", first)
        for change in (
            {"html_url": "https://other.example/org/alpha/pull/17"},
            {"html_url": "https://github.com/org/alpha/pull/17", "title": "Changed title"},
        ):
            pulls[0].update(change)
            self.assertEqual(self.publish("open_pr", run, observation_provider=None).status, "unknown")
        self.assertEqual(len([cmd for cmd in calls if cmd[:3] == ("gh", "pr", "create")]), 1)
        self.assertTrue(any("state=all" in cmd[-1] for cmd in calls if cmd[:2] == ("gh", "api")))

    def test_fresh_effect_refuses_changed_git_and_missing_or_revoked_source_grant(self):
        from robert_agent.delivery.lock import source_publication_lock

        self.accepted_development()
        calls, _, _, run = self.runner()
        with source_publication_lock(self.db, self.ticket.source_id) as held:
            self.assertTrue(held)
            self.assertEqual(self.publish("push_existing_pr", run).reason, "source_publication_locked")
        self.assertEqual(self.publish("push_existing_pr", run, owner="foreign").reason, "source_fence_unavailable")
        self.assertEqual(
            self.publish("push_existing_pr", run, observation_provider=None).reason, "native_proof_unavailable"
        )
        self.assertEqual(calls, [])
        self.terminal = True
        refused = self.publish("push_existing_pr", run)
        self.assertEqual(refused.reason, "current_source_grant_unavailable", refused)
        self.assertEqual(self.read("SELECT COUNT(*) FROM publication_intents"), [(0,)])
        self.terminal = False
        (self.tree / "README.md").write_text("Later unreviewed edit")
        self.git(self.tree, "add", "README.md")
        self.git(self.tree, "commit", "-m", tapd_policy.NATIVE_COMMIT_MESSAGE)
        self.assertEqual(self.publish("push_existing_pr", run).reason, "native_preflight_refused")
        self.assertFalse(any(cmd[:2] == ("git", "push") for cmd in calls))

    def test_stale_owner_cannot_record_successful_remote_effect(self):
        from robert_agent.foundation import values
        from robert_agent.persistence import transactions
        from robert_agent.persistence.stores import source_sessions

        self.accepted_development()
        calls, _, _, run = self.runner()

        def takeover(command, **kwargs):
            result = run(command, **kwargs)
            if command[:2] == ["git", "push"]:
                with closing(sqlite3.connect(self.db)) as conn, conn:
                    conn.execute("UPDATE source_sessions SET lease_until='2000-01-01T00:00:00+00:00'")
                with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
                    transactions.begin_immediate(conn)
                    self.assertIsNotNone(
                        source_sessions.claim(
                            conn,
                            source_id=self.ticket.source_id,
                            owner="successor",
                            now=values.utc_now(),
                            lease_seconds=300,
                        )
                    )
            return result

        first = self.publish("push_existing_pr", takeover)
        self.assertEqual((first.status, first.reason), ("unknown", "source_fence_or_scope_lost"))
        self.assertEqual(self.read("SELECT status FROM publication_intents"), [("executing",)])
        revived = self.publish("push_existing_pr", run, owner="successor", observation_provider=None)
        self.assertEqual(revived.status, "published", revived)
        self.assertEqual(len([cmd for cmd in calls if cmd[:2] == ("git", "push")]), 1)

    def test_existing_native_pr_requires_authenticated_owner_and_exact_lease(self):
        self.accepted_development(existing=True)
        calls, _, _, wrong = self.runner(publisher="other-account")
        denied = self.publish("push_existing_pr", wrong)
        self.assertEqual(denied.reason, "native_preflight_refused", denied)
        self.assertFalse(any(cmd[:2] == ("git", "push") for cmd in calls))
        calls, state, _, run = self.runner()
        published = self.publish("push_existing_pr", run)
        self.assertEqual(published.status, "published", (published, calls, state))
        self.assertEqual(published.receipt.url, "https://github.com/org/alpha/pull/11")
        self.assertEqual(state["sha"], self.head)
        self.assertEqual(len([cmd for cmd in calls if cmd[:2] == ("git", "push")]), 1)

    def test_started_checkpoint_reauthorizes_after_newer_source_revocation(self):
        from robert_agent.foundation.values import utc_now
        from robert_agent.persistence import transactions
        from robert_agent.work.application import source_observation, tapd_intake

        self.accepted_development()
        calls, _, _, run = self.runner()
        revoked = False

        def observe_newer_removal(command, **kwargs):
            nonlocal revoked
            if not revoked and self.read("SELECT status FROM publication_intents") == [("executing",)]:
                revoked = True
                self.terminal = True
                with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
                    transactions.begin_immediate(conn)
                    observation = self.provider(None, self.ticket)
                    tapd_intake.observe_ticket(
                        conn,
                        self.configured["tapd"],
                        observation,
                        source_observation.publication_facts(conn, self.ticket),
                        now=utc_now(),
                    )
            return run(command, **kwargs)

        result = self.publish("push_existing_pr", observe_newer_removal)
        self.assertTrue(revoked)
        self.assertEqual(result.status, "unknown", result)
        self.assertFalse(any(cmd[:2] == ("git", "push") for cmd in calls))
        self.assertEqual(self.read("SELECT status FROM publication_intents"), [("attention",)])

    def test_publisher_change_after_checkpoint_blocks_write_but_keeps_unknown_readback(self):
        self.accepted_development()
        calls, state, _, run = self.runner()
        changed = False

        def change_publisher(command, **kwargs):
            nonlocal changed
            if not changed and self.read("SELECT status FROM publication_intents") == [("executing",)]:
                changed = True
                self.configured["repos"][0]["github_account"] = "replacement"
                with closing(sqlite3.connect(self.db)) as conn, conn:
                    conn.execute("UPDATE repos SET github_account='replacement'")
            return run(command, **kwargs)

        result = self.publish("push_existing_pr", change_publisher)
        self.assertTrue(changed)
        self.assertEqual(result.status, "unknown", result)
        self.assertFalse(any(cmd[:2] == ("git", "push") for cmd in calls))
        state["sha"] = self.head
        reconciled = self.publish("push_existing_pr", run, observation_provider=None)
        self.assertEqual(reconciled.status, "published", reconciled)
        self.assertFalse(any(cmd[:2] == ("git", "push") for cmd in calls))

    def test_additive_schema_upgrade_preserves_existing_action_and_intent_identity(self):
        from robert_agent.persistence.schema.database import init_database

        self.accepted_development()
        action_id = self.action_ids["push_existing_pr"]
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("DROP TABLE github_publication_preparations")
            conn.execute(
                """INSERT INTO publication_intents(publication_id,action_id,idempotency_key,request_fingerprint,status,created_at,updated_at)
                    VALUES('pre-unit-intent',?,'pre-unit-key','pre-unit-fingerprint','attention','then','then')""",
                (action_id,),
            )
        before = self.read("SELECT * FROM publication_intents")
        init_database(self.db)
        init_database(self.db)
        self.assertEqual(self.read("SELECT * FROM publication_intents"), before)
        self.assertEqual(self.read("SELECT COUNT(*) FROM github_publication_preparations"), [(0,)])
        self.assertEqual(self.read("PRAGMA foreign_key_check"), [])

    def test_generic_guard_refuses_native_target_without_mutating_its_intent(self):
        from robert_agent.delivery.application.guards import publication_scope_guard

        self.accepted_development()
        action_id = self.action_ids["push_existing_pr"]
        with closing(database.connect(self.db)) as conn:
            action = source_github_publication.required_action(conn, self.root_id, action_id)
            refused = publication_scope_guard(conn, dict(action))
        self.assertEqual(refused["status"], "native_scope_rejected")
        self.assertEqual(self.read("SELECT COUNT(*) FROM publication_intents"), [(0,)])

    def _followup_classification(self, recommendation):
        from dataclasses import replace
        from robert_agent.foundation.values import utc_now

        followup = self.comment("2", suffix=" after first delivery")
        version = replace(followup.version, created=utc_now())
        self.comments.append(
            replace(
                followup,
                version=version,
                creator=replace(followup.creator, observation=version),
                own_body=replace(followup.own_body, observation=version),
            )
        )
        admitted = self.tick("admitted", ticket=self.ticket)
        self.tick("prepared")
        self.tick("dispatched")
        self.submit("classification_result", recommended_route=recommendation)
        self.tick("waiting")
        self.tick("audited")
        return admitted.round_id

    def test_linked_pr_mention_is_held_once_under_its_real_github_event(self):
        self._delivered_previous_pr()
        self._admit_github_discussion([self._github_discussion()], dry_run=True)
        self.assertFalse(self.read("SELECT event_id FROM source_events WHERE event_id='event:comment:617'"))
        self.assertFalse(self.read("SELECT * FROM source_github_ingress"))
        self._admit_github_discussion([self._github_discussion()], budget=0)
        self.assertEqual(
            self.read("SELECT provider,source_id,phase FROM ingress_requests WHERE auth_type='github_mention'"),
            [("github", "source:github:org/alpha!17", "held")],
        )
        self._admit_github_discussion([self._github_discussion()])
        self.assertEqual(
            self.read(
                "SELECT request_events.event_id,state FROM request_events JOIN ingress_requests USING(request_id) "
                "WHERE auth_type='github_mention'"
            ),
            [("event:comment:617", "pending")],
        )

        [[request_id]] = self.read("SELECT request_id FROM ingress_requests WHERE auth_type='github_mention'")
        [[receipt]] = self.read(
            "SELECT metadata_json FROM supervision_checkpoints WHERE scope_id='event:comment:617' AND rule_id='admission'"
        )
        evidence = json.loads(receipt)
        self.assertEqual(
            (evidence["disposition"], evidence["target_type"], evidence["target_id"]),
            ("linked_pending", "ingress_request", request_id),
        )

    def test_bridge_recovers_first_observation_after_real_receipt_then_canonical_link(self):
        from robert_agent.persistence.queries import source_github_ingress
        from robert_agent.persistence.schema.database import init_database

        self.accepted_development()
        _, _, _, github = self.runner(pr_edit=lambda pr: pr.update(state="open"))
        self.assertEqual(self.publish("push_existing_pr", github).status, "published")
        self.assertEqual(self.publish("open_pr", github).status, "published")
        self.assertFalse(self.read("SELECT source_id FROM sources WHERE source_type='pull_request'"))
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("DROP TABLE source_github_ingress")
        init_database(self.db)

        raw = self._github_discussion()
        self._admit_github_discussion([raw])
        self.assertEqual(
            self.read("SELECT reason,request_id,tapd_source_id FROM source_github_ingress"),
            [("source_github_lineage_unproved", None, self.ticket.source_id)],
        )
        [[original]] = self.read("SELECT first_observation_json FROM source_github_ingress")
        _, tapd = self._tapd_publisher()
        self.assertEqual(tapd()["native_completed_count"], 1)
        edited = "2026-11-14T20:01:00Z"
        raw.update(
            body="@bot Please inspect the corrected request.",
            body_updated_at=edited,
            body_edited_at=edited,
            body_editor_login="owner",
        )
        raw["github_discussion_item"]["updated_at"] = edited
        self._admit_github_discussion([raw])
        self._admit_github_discussion([raw])
        [[request_id]] = self.read("SELECT request_id FROM ingress_requests WHERE auth_type='github_mention'")
        with closing(database.connect(self.db)) as conn:
            bridge = source_github_ingress.for_request(conn, request_id)
        self.assertEqual(bridge["first_observation_json"], original)
        [[snapshot]] = self.read("SELECT content_snapshot_json FROM ingress_requests WHERE request_id=?", (request_id,))
        self.assertEqual(json.loads(snapshot)["body"], raw["body"])
        target = json.loads(bridge["target_json"])
        self.assertEqual(
            (target["provider"], target["host"], target["repository"], target["pr_number"], target["event_url"]),
            ("github", "github.com", "org/alpha", 17, "https://github.com/org/alpha/pull/17#issuecomment-617"),
        )
        self.assertEqual(target["tapd_source_id"], self.ticket.source_id)
        self.assertEqual(target["creation_result_id"], self.root_id)
        self.assertEqual(target["creation_action_id"], self.action_ids["open_pr"])
        [[publication, fingerprint]] = self.read(
            "SELECT publication_id,request_fingerprint FROM publication_intents WHERE action_id=?",
            (self.action_ids["open_pr"],),
        )
        self.assertEqual(
            (target["creation_publication_id"], target["creation_request_fingerprint"]), (publication, fingerprint)
        )
        self.assertEqual(
            self.read("SELECT state FROM request_events WHERE request_id=?", (request_id,)), [("pending",)]
        )

    def test_public_github_discussion_producer_preserves_supported_authority_before_intake(self):
        from robert_agent.adapters.github import discovery

        self._delivered_previous_pr()
        pr_url = "https://github.com/org/alpha/pull/17"
        made = "2026-11-14T20:00:00Z"
        edited = "2026-11-14T20:01:00Z"
        author = {"login": "owner", "type": "User"}
        comment = {
            "id": 617,
            "node_id": "controller-observed-node",
            "body": "@bot please inspect the edited text",
            "html_url": pr_url + "#issuecomment-617",
            "user": author,
            "author_association": "OWNER",
            "created_at": made,
            "updated_at": edited,
        }
        reviews = [
            {**comment, "id": 618, "body": "@bot please review this", "html_url": pr_url + "#pullrequestreview-618"},
            {**comment, "id": 619, "body": "@bot vote for A", "html_url": pr_url + "#discussion_r619"},
        ]

        def run(cmd, **_kwargs):
            if "--assignee" in cmd or "--mentions" in cmd:
                payload = []
            elif cmd[2] == "notifications":
                payload = [
                    {
                        "id": "notification",
                        "reason": "mention",
                        "updated_at": edited,
                        "subject": {"type": "PullRequest", "url": "https://api.github.com/repos/org/alpha/pulls/17"},
                    }
                ]
            elif cmd[2] == "graphql":
                payload = {
                    "data": {
                        "node": {
                            "id": comment["node_id"],
                            "body": comment["body"],
                            "author": author,
                            "editor": author,
                            "lastEditedAt": edited,
                            "updatedAt": edited,
                        }
                    }
                }
            elif cmd[2] == "repos/org/alpha/issues/17/comments":
                payload = [comment]
            elif cmd[2] == "repos/org/alpha/pulls/17/reviews":
                payload = [reviews[0]]
            elif cmd[2] == "repos/org/alpha/pulls/17/comments":
                payload = [reviews[1]]
            elif cmd[2] in {"repos/org/alpha/issues/17", "repos/org/alpha/pulls/17"}:
                payload = {"number": 17, "state": "open", "html_url": pr_url, "user": {"login": "bot"}}
            else:
                self.fail(f"Unexpected GitHub read {cmd}")
            return SimpleNamespace(stdout=json.dumps(payload), returncode=0, stderr="")

        raw = discovery.collect_live_events(
            self.configured["repos"][0], runner=run, known_workstreams={"github:org/alpha!17"}
        )
        discussion = [item for item in raw if item.get("github_discussion_item")]
        self.assertEqual({item["event_type"] for item in discussion}, {"comment", "review", "review_comment"})
        self._admit_github_discussion(discussion)
        self.assertEqual(
            self.read("SELECT event_id FROM source_github_ingress WHERE request_id IS NOT NULL ORDER BY event_id"),
            [("event:comment:617",), ("event:review:618",), ("event:review_comment:619",)],
        )
        self.assertEqual(self.read("SELECT COUNT(*) FROM review_sessions"), [(0,)])
        self.assertEqual(self.read("SELECT COUNT(*) FROM vote_sessions"), [(0,)])

    def test_bridge_holds_context_forgery_disabled_scope_and_task_bound_event(self):
        from copy import deepcopy

        self._delivered_previous_pr()
        settings = deepcopy(self.configured)
        settings["tapd"]["enabled"] = False
        self._admit_github_discussion([self._github_discussion(identifier=620)], config=settings)
        settings["tapd"]["enabled"] = True
        settings["tapd"]["projects"][0]["repositories"] = ["org/another"]
        self._admit_github_discussion([self._github_discussion(identifier=621)], config=settings)
        wrong_item = self._github_discussion(identifier=622)
        wrong_item["github_discussion_item"]["url"] = "https://github.com/foreign/alpha/pull/17#issuecomment-622"
        forged = self._github_discussion(
            identifier=623,
            number=19,
            actor="outsider",
            url="https://github.com/org/alpha/pull/19",
            pr_author_login="bot",
            metadata={"dd_workstream": {"origin_workstream_id": self.ticket.source_id}},
        )
        self._admit_github_discussion(
            [
                wrong_item,
                forged,
                self._github_discussion(identifier=624, body="> @bot Please inspect this."),
                self._github_discussion(identifier=625, actor="outsider"),
                self._github_discussion(identifier=626, actor="bot"),
                self._github_discussion(identifier=627, actor_kind="github_system"),
                self._github_discussion(identifier=628, event_type="review_request"),
            ]
        )
        self.assertFalse(self.read("SELECT event_id FROM source_github_ingress WHERE event_id='event:comment:623'"))
        self.assertFalse(self.read("SELECT request_id FROM ingress_requests WHERE auth_type='github_mention'"))
        with self.assertRaises(ValueError):
            self._admit_github_discussion(
                [self._github_discussion(identifier=629, url="https://ghe.example/org/alpha/pull/17")]
            )

        from robert_agent.adapters.github.discovery import normalize_events
        from robert_agent.foundation import values
        from robert_agent.persistence.stores.task_events import bind_task_event
        from robert_agent.security.authorization import authorize_events
        from robert_agent.work.application import source_events

        raw = self._github_discussion(identifier=630)
        repo = self.configured["repos"][0]
        [decision] = authorize_events(normalize_events([raw], repo), repo)
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            _, event_id = source_events.insert_source_and_event(
                conn, values.repository_id(repo["full_name"]), decision, values.utc_now()
            )
            [[task_id]] = self.read(
                "SELECT task_id FROM github_actions WHERE action_id=?", (self.action_ids["open_pr"],)
            )
            bind_task_event(conn, task_id, event_id, "trigger", values.utc_now())
        self._admit_github_discussion([raw])
        self.assertEqual(
            self.read("SELECT request_id,reason FROM source_github_ingress WHERE event_id=?", (event_id,)),
            [(None, "source_github_event_already_bound")],
        )

    def test_fresh_observation_controls_held_bridge_without_rearming_edited_context(self):
        from robert_agent.work import source_policy

        self._delivered_previous_pr()
        old = self._github_discussion(identifier=630, body="Earlier context")
        new = self._github_discussion(identifier=631)
        repo = self.configured["repos"][0]
        replay = self._github_discussion(identifier=632)
        [replayed] = source_policy.refresh_pending_events([], [replay], {("pull_request", 17)}, repo)
        old_shape = self._github_discussion(identifier=633)
        complete_shape = self._github_discussion(identifier=633)
        del old_shape["github_discussion_item"]
        legacy_replay = self._github_discussion(identifier=635, body="@bot cached earlier request")
        del legacy_replay["github_discussion_item"]
        [legacy_replayed] = source_policy.refresh_pending_events([], [legacy_replay], {("pull_request", 17)}, repo)
        self._admit_github_discussion([old, new, replayed, old_shape, legacy_replayed], known=False)
        self.assertEqual(
            self.read(
                "SELECT first_observation_json,request_id FROM source_github_ingress WHERE event_id='event:comment:632'"
            ),
            [(None, None)],
        )
        self._admit_github_discussion(
            [replay, complete_shape, self._github_discussion(identifier=635, body="@bot actual current request")],
            known=False,
        )

        edited_at = "2026-11-14T20:01:00Z"
        for raw, body in [(old, "@bot please start"), (new, "Removed mention")]:
            raw.update(body=body, body_updated_at=edited_at, body_edited_at=edited_at, body_editor_login="owner")
            raw["github_discussion_item"]["updated_at"] = edited_at
        self._admit_github_discussion([old, new], known=False)
        self._admit_github_discussion([old, new], known=False)
        self.assertEqual(
            self.read("SELECT event_id FROM source_github_ingress WHERE request_id IS NOT NULL ORDER BY event_id"),
            [("event:comment:631",), ("event:comment:632",), ("event:comment:633",), ("event:comment:635",)],
        )
        [[first, latest, canonical]] = self.read(
            "SELECT first_observation_json,latest_observation_json,payload_json FROM source_github_ingress "
            "JOIN source_events USING(event_id) WHERE event_id='event:comment:631'"
        )
        self.assertIn("@bot", json.loads(first)["body"])
        self.assertEqual(json.loads(latest)["body"], "Removed mention")
        self.assertIn("@bot", json.loads(canonical)["body"])

    def test_partial_receipt_refuses_generic_dispatch_and_bridge_failure_is_atomic(self):
        self._delivered_previous_pr()
        [[receipt]] = self.read(
            "SELECT remote_evidence_json FROM publication_intents WHERE action_id=?", (self.action_ids["open_pr"],)
        )
        update = "UPDATE publication_intents SET remote_evidence_json=? WHERE action_id=?"
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(update, ("{}", self.action_ids["open_pr"]))
        self._admit_github_discussion([self._github_discussion()])
        self.assertEqual(
            self.read("SELECT request_id,reason FROM source_github_ingress"), [(None, "source_github_lineage_unproved")]
        )
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(update, (receipt, self.action_ids["open_pr"]))

        from robert_agent.work.application import source_github_ingress
        from robert_agent.persistence.queries import request_events

        def failed_write(conn, **kwargs):
            self.assertEqual(len(request_events.for_ingress_request(conn, kwargs["request_id"])), 1)
            return False

        raw = self._github_discussion(identifier=634)
        with (
            patch.object(source_github_ingress.write, "insert_observation", side_effect=failed_write),
            self.assertRaisesRegex(ValueError, "bridge could not be recorded"),
        ):
            self._admit_github_discussion([raw])
        self.assertFalse(self.read("SELECT request_id FROM ingress_requests WHERE auth_type='github_mention'"))
        self.assertFalse(self.read("SELECT event_id FROM request_events WHERE event_id='event:comment:634'"))
        self.assertFalse(self.read("SELECT event_id FROM source_github_ingress WHERE event_id='event:comment:634'"))
        self._admit_github_discussion([raw])
        self.assertTrue(self.read("SELECT request_id FROM ingress_requests WHERE auth_type='github_mention'"))

    def test_held_github_request_selects_finished_tapd_source_without_pending_tapd_request(self):
        from robert_agent.application.source_cycle import has_due_source
        from robert_agent.persistence.schema.database import init_database

        self._delivered_previous_pr()
        self._admit_github_discussion([self._github_discussion()])
        self.assertFalse(
            self.read(
                "SELECT request_id FROM ingress_requests WHERE provider='tapd' AND phase='pending'"
                " AND request_id IN (SELECT request_id FROM request_events WHERE state='pending')"
            )
        )
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE tapd_coordination SET next_due_at='2000-01-01T00:00:00+00:00'")
        old_request = self.read("SELECT * FROM source_github_ingress")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("DROP TABLE source_github_observations")
        self.assertFalse(has_due_source(self.db, self.configured, owner="test-coordinator"))
        init_database(self.db)
        self.assertEqual(self.read("SELECT * FROM source_github_ingress"), old_request)
        self.assertTrue(has_due_source(self.db, self.configured, owner="test-coordinator"))

    def _github_observation(self):
        [[operation, status, data]] = self.read(
            "SELECT operation_id,status,evidence_json FROM source_github_observations"
        )
        return operation, status, json.loads(data)

    def test_public_coordinator_observes_current_github_body_and_rejects_stale_operation(self):
        from robert_agent.foundation import values
        from robert_agent.persistence import transactions
        from robert_agent.persistence.stores.source_sessions import SourceClaim
        from robert_agent.work.application import source_github_observation

        self._delivered_previous_pr()
        self._admit_github_discussion([self._github_discussion()])
        original = self.read("SELECT target_json,first_observation_json FROM source_github_ingress")
        original_requests = self.read("SELECT * FROM ingress_requests WHERE provider='github'")
        request_id = original_requests[0][0]
        ledger = self.read("SELECT * FROM request_events WHERE request_id=?", (request_id,))
        work = self.read("SELECT task_id FROM tasks")
        self.configured["repos"][0]["trusted_actors"] += ["editor"]

        def free_writer(endpoint):
            if endpoint == "repos/org/alpha/pulls/17":
                with closing(sqlite3.connect(self.db, timeout=0)) as conn, conn:
                    conn.execute(
                        "UPDATE source_sessions SET updated_at=updated_at WHERE source_id=?", (self.ticket.source_id,)
                    )

        calls, run = self._current_github_runner(actor="editor", during_read=free_writer, parent_state="closed")
        with patch.dict("os.environ", {"GH_HOST": "ghe.example", "GH_REPO": "outside/repo"}):
            step = self.tick("admitted", github_discussion_runner=run)
        self.assertEqual(calls, ["repos/org/alpha/pulls/17", "repos/org/alpha/issues/comments/617", "graphql"])
        operation, status, first = self._github_observation()
        self.assertEqual(
            (status, first["reason"], first["item"]["actor"]), ("present", "github_current_mention", "editor")
        )
        self.assertEqual(first["parent"]["state"], "closed")
        self.assertEqual(
            self.read("SELECT capability FROM source_rounds WHERE round_id=?", (step.round_id,)), [("readonly",)]
        )
        self.assertEqual(
            self.read("SELECT phase FROM ingress_requests WHERE request_id=?", (request_id,)), [("pending",)]
        )
        promoted = self.read("SELECT * FROM ingress_requests WHERE provider='github'")
        [[owner, token]] = self.read(
            "SELECT owner,token FROM source_sessions WHERE source_id=?", (self.ticket.source_id,)
        )
        [[metadata]] = self.read("SELECT metadata_json FROM sources WHERE source_id=?", (self.ticket.source_id,))
        claim = SourceClaim(self.ticket.source_id, owner, token)

        def proof(operation_id):
            with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
                transactions.begin_immediate(conn)
                return source_github_observation.current_batch(
                    conn,
                    self.configured,
                    claim,
                    self.ticket,
                    revision=json.loads(metadata)["revision"],
                    operation_id=operation_id,
                    request_ids=(request_id,),
                    now=values.utc_now(),
                )

        def read_again(runner):
            return source_github_observation.refresh_batch(
                self.db,
                self.configured,
                claim,
                self.ticket,
                revision=json.loads(metadata)["revision"],
                request_ids=(request_id,),
                runner=runner,
            )

        first_proof = proof(operation)
        self.assertIsNotNone(first_proof)
        self.assertEqual(first_proof[0].disposition, "github_current_grant")
        refreshed = read_again(run)
        operation2, _, second = self._github_observation()
        self.assertEqual(operation2, refreshed.operation_id)
        self.assertNotEqual(operation, operation2)
        self.assertEqual(first["semantic_version"], second["semantic_version"])
        self.assertIsNone(proof(operation))
        self.assertEqual(proof(operation2)[0].item.body, first["item"]["body"])
        _, failed = self._current_github_runner(parent_status=403)
        self.assertIsNotNone(read_again(failed).operation_id)
        _, status, unavailable = self._github_observation()
        self.assertEqual(status, "unavailable")
        self.assertIsNone(unavailable["item"]["body"])
        self.assertIsNone(proof(operation2))
        self.assertEqual(self.read("SELECT * FROM ingress_requests WHERE provider='github'"), promoted)
        self.assertEqual(self.read("SELECT * FROM request_events WHERE request_id=?", (request_id,)), ledger)
        self.assertEqual(self.read("SELECT target_json,first_observation_json FROM source_github_ingress"), original)
        self.assertEqual(self.read("SELECT task_id FROM tasks"), work)

    def test_public_coordinator_observes_review_and_inline_comment_with_edited_first_mention(self):
        self._delivered_previous_pr()
        edited = "2026-11-14T20:01:00Z"
        comment = self._github_discussion(body_edited_at=edited, body_updated_at=edited, body_editor_login="owner")
        comment["github_discussion_item"]["updated_at"] = edited
        raw = [comment]
        for kind, anchor in (("review", "pullrequestreview-617"), ("review_comment", "discussion_r617")):
            event = self._github_discussion(event_type=kind)
            del event["body_authorization_complete"]
            event["github_discussion_item"]["url"] = f"https://github.com/org/alpha/pull/17#{anchor}"
            raw.append(event)
        self._admit_github_discussion(raw)
        calls, run = self._current_github_runner()
        step = self.tick("admitted", github_discussion_runner=run)
        self.assertEqual(
            self.read("SELECT COUNT(*) FROM source_round_requests WHERE round_id=?", (step.round_id,)), [(3,)]
        )
        self.assertEqual(calls.count("repos/org/alpha/pulls/17"), 1)
        self.assertCountEqual(
            calls[1:],
            [
                "repos/org/alpha/issues/comments/617",
                "graphql",
                "repos/org/alpha/pulls/17/reviews/617",
                "repos/org/alpha/pulls/comments/617",
            ],
        )
        [(_, status)] = self.read("SELECT DISTINCT operation_id,status FROM source_github_observations")
        self.assertEqual(status, "present")
        self.assertEqual(self.read("SELECT COUNT(*) FROM source_github_observations"), [(3,)])

    def test_current_github_marker_identity_and_authoritative_absence_stay_held(self):
        from robert_agent.application.source_cycle import has_due_source

        self._delivered_previous_pr()
        self._admit_github_discussion([self._github_discussion()])
        for options, status, reason in (
            ({"parent_edit": lambda pr: pr.update(number=18)}, "unavailable", "github_parent_scope_unavailable"),
            (
                {"item_edit": lambda item: item.update(issue_url="https://api.github.com/repos/org/alpha/issues/18")},
                "unavailable",
                "github_item_identity_changed",
            ),
            ({"parent_status": 403, "item_status": 404}, "unavailable", "github_parent_scope_unavailable"),
            ({"item_status": 404, "scope_status": 403}, "unavailable", "github_item_read_unavailable"),
            ({"item_status": 404}, "absent", "github_item_absence_confirmed"),
        ):
            with self.subTest(options=options):
                calls, run = self._current_github_runner(**options)
                self.tick("held", github_discussion_runner=run)
                _, saved_status, evidence = self._github_observation()
                self.assertEqual((saved_status, evidence["reason"]), (status, reason))
                if options.get("parent_status") or "parent_edit" in options:
                    self.assertEqual(calls, ["repos/org/alpha/pulls/17"])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE tapd_coordination SET next_due_at='2000-01-01T00:00:00+00:00'")
        self.assertFalse(has_due_source(self.db, self.configured, owner="test-coordinator"))
        self.assertEqual(self.read("SELECT phase FROM ingress_requests WHERE provider='github'"), [("revoked",)])
        self.assertEqual(
            self.read(
                "SELECT state FROM request_events JOIN ingress_requests USING(request_id) WHERE provider='github'"
            ),
            [("revoked",)],
        )
        for identifier, options, reason in (
            (618, {"body": "The mention was removed."}, "github_current_mention_removed"),
            (619, {"actor": "outsider", "base_branch": "release"}, "github_current_actor_untrusted"),
        ):
            with self.subTest(reason=reason):
                self._admit_github_discussion([self._github_discussion(identifier=identifier)])
                with closing(sqlite3.connect(self.db)) as conn, conn:
                    conn.execute("UPDATE tapd_coordination SET next_due_at='2000-01-01T00:00:00+00:00'")
                self.assertTrue(has_due_source(self.db, self.configured, owner="test-coordinator"))
                _, runner = self._current_github_runner(identifier=identifier, **options)
                self.tick("held", github_discussion_runner=runner)
                [[encoded, phase, state]] = self.read(
                    "SELECT o.evidence_json,r.phase,e.state FROM source_github_observations o "
                    "JOIN ingress_requests r USING(request_id) JOIN request_events e USING(request_id) "
                    "WHERE e.event_id=?",
                    (f"event:comment:{identifier}",),
                )
                self.assertEqual((json.loads(encoded)["reason"], phase, state), (reason, "revoked", "revoked"))
        self.assertFalse(has_due_source(self.db, self.configured, owner="test-coordinator"))

    def test_github_current_read_discards_scope_drift_and_cooperative_stop(self):
        from threading import Event

        self._delivered_previous_pr()
        self._admit_github_discussion([self._github_discussion()])

        def drift_repo(endpoint):
            if endpoint.endswith("/pulls/17"):
                self.configured["repos"][0]["trusted_actors"].append("changed")

        _, run = self._current_github_runner(during_read=drift_repo)
        self.assertEqual(self.tick("held", github_discussion_runner=run).reason, "github_observation_scope_changed")
        _, status, evidence = self._github_observation()
        self.assertEqual((status, evidence["reason"]), ("unavailable", "github_observation_in_progress"))

        def drift_request(endpoint):
            if endpoint.endswith("/pulls/17"):
                with closing(sqlite3.connect(self.db, timeout=0)) as conn, conn:
                    conn.execute("UPDATE ingress_requests SET content_version='changed' WHERE provider='github'")

        _, run = self._current_github_runner(during_read=drift_request)
        self.assertEqual(self.tick("held", github_discussion_runner=run).reason, "github_observation_scope_changed")

        stop = Event()
        calls, run = self._current_github_runner(during_read=lambda _endpoint: stop.set())
        self.assertEqual(
            self.tick("held", stop=stop, github_discussion_runner=run).reason, "github_observation_unavailable"
        )
        self.assertEqual(calls, ["repos/org/alpha/pulls/17"])

        def drift_revision(endpoint):
            if endpoint.endswith("/pulls/17"):
                with closing(sqlite3.connect(self.db, timeout=0)) as conn, conn:
                    conn.execute(
                        "UPDATE sources SET metadata_json=json_set(metadata_json,'$.revision','changed') WHERE source_id=?",
                        (self.ticket.source_id,),
                    )

        _, run = self._current_github_runner(during_read=drift_revision)
        self.assertEqual(self.tick("held", github_discussion_runner=run).reason, "github_observation_scope_changed")

        def drift_claim(endpoint):
            if endpoint.endswith("/pulls/17"):
                with closing(sqlite3.connect(self.db, timeout=0)) as conn, conn:
                    conn.execute(
                        "UPDATE source_sessions SET token=token+1,owner='other' WHERE source_id=?",
                        (self.ticket.source_id,),
                    )

        _, run = self._current_github_runner(during_read=drift_claim)
        self.assertEqual(self.tick("held", github_discussion_runner=run).reason, "source_claim_changed")
        _, status, evidence = self._github_observation()
        self.assertEqual((status, evidence["reason"]), ("unavailable", "github_observation_in_progress"))

    def test_public_mixed_admission_freezes_current_github_and_tapd_without_dispatch(self):
        _claim, _revision, owners = self._mixed_pending_fixture()
        before = self.read("SELECT first_observation_json,target_json FROM source_github_ingress")
        starts = len(self.started)
        _, run = self._current_github_runner(body="@bot current GitHub request body")
        admitted = self.tick("admitted", github_discussion_runner=run)
        members = self.read(
            "SELECT i.provider,i.phase,rr.content_snapshot_json FROM source_round_requests rr "
            "JOIN ingress_requests i USING(request_id) WHERE rr.round_id=? ORDER BY i.provider",
            (admitted.round_id,),
        )
        self.assertEqual(
            [(provider, phase) for provider, phase, _ in members], [("github", "pending"), ("tapd", "pending")]
        )
        github = json.loads(members[0][2])
        self.assertEqual(github["body"], "@bot current GitHub request body")
        self.assertEqual(github["return_target"], owners["github"].return_target)
        self.assertEqual(self.read("SELECT first_observation_json,target_json FROM source_github_ingress"), before)
        self.tick("prepared", ticket=self.ticket, github_discussion_runner=run)
        self.assertEqual(len(self.started), starts)

    def test_github_paused_entry_stays_readonly_after_exact_same_rule_resume(self):
        self._delivered_previous_pr()
        self.labels = ()
        self.tick("idle", ticket=self.ticket)
        self._admit_github_discussion([self._github_discussion()])
        self.assertEqual(
            self.read(
                "SELECT json_extract(authorization_json,'$.entry_grant.capability') FROM ingress_requests "
                "WHERE provider='github'"
            ),
            [("readonly",)],
        )
        self.labels = ("robot",)
        notice = self.tick("publication_pending", ticket=self.ticket)
        [[question, version, action]] = self.read(
            "SELECT question_id,version,action_id FROM source_confirmation_questions WHERE source_result_id=?",
            (notice.identity,),
        )
        _, run = self._current_github_runner()
        publish, _identity = self._recovery_tapd_client(run, [])
        self.assertEqual(publish(notice.identity, action).status, "published")
        self._recovery_comment(207, f"@test-helper ROBERT-CHOICE {question} v{version} resume", "requester")
        outcome = self.tick("admitted", ticket=self.ticket, github_discussion_runner=run)
        self.assertEqual(self.read("SELECT COUNT(*) FROM source_resume_transitions"), [(1,)])
        self.assertEqual(
            self.read("SELECT capability FROM source_rounds WHERE round_id=?", (outcome.round_id,)), [("readonly",)]
        )
        self.assertEqual(
            self.read(
                "SELECT json_extract(authorization_json,'$.capability') FROM ingress_requests WHERE provider='github'"
            ),
            [("readonly",)],
        )

    def test_replayed_paused_entry_cannot_gain_development_when_fresh_identity_arrives(self):
        from robert_agent.work import source_policy

        self._delivered_previous_pr()
        self.labels = ()
        self.tick("idle", ticket=self.ticket)
        raw = self._github_discussion()
        [cached] = source_policy.refresh_pending_events([], [raw], {("pull_request", 17)}, self.configured["repos"][0])
        self._admit_github_discussion([cached], known=False)
        self.assertEqual(
            self.read("SELECT request_id,first_observation_json FROM source_github_ingress"), [(None, None)]
        )
        self.labels = ("robot",)
        notice = self.tick("publication_pending", ticket=self.ticket)
        [[question, version, action]] = self.read(
            "SELECT question_id,version,action_id FROM source_confirmation_questions WHERE source_result_id=?",
            (notice.identity,),
        )
        _, run = self._current_github_runner()
        publish, _ = self._recovery_tapd_client(run, [])
        self.assertEqual(publish(notice.identity, action).status, "published")
        self._recovery_comment(207, f"@test-helper ROBERT-CHOICE {question} v{version} resume", "requester")
        self.tick("idle", ticket=self.ticket, github_discussion_runner=run)
        self.assertEqual(self.read("SELECT COUNT(*) FROM source_resume_transitions"), [(1,)])
        self._admit_github_discussion([self._github_discussion(body="@bot fresh authenticated body")], known=False)
        _, run = self._current_github_runner(body="@bot fresh authenticated body")
        outcome = self.tick("admitted", ticket=self.ticket, github_discussion_runner=run)
        self.assertEqual(
            self.read("SELECT capability FROM source_rounds WHERE round_id=?", (outcome.round_id,)), [("readonly",)]
        )

    def test_mixed_admission_cannot_reuse_sealed_operation_after_round_start_refusal(self):
        from robert_agent.persistence import transactions
        from robert_agent.work.application import source_github_observation, tapd_intake

        claim, revision, owned = self._mixed_pending_fixture()
        request_ids = (owned["github"].request["request_id"],)
        _, run = self._current_github_runner()
        batch = source_github_observation.refresh_batch(
            self.db, self.configured, claim, self.ticket, revision=revision, request_ids=request_ids, runner=run
        )
        self.assertIsNotNone(batch.operation_id)
        options = {
            "round_id": "mixed-operation-once",
            "revision": revision,
            "now": utc_now(),
            "operations": ((batch.operation_id, request_ids),),
        }
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            with patch.object(tapd_intake.source_sessions, "start_round", return_value=None):
                self.assertEqual(
                    tapd_intake.admit_mixed_round(conn, self.configured, self.ticket, claim, **options),
                    (None, True),
                )
            ledger = self.read("SELECT * FROM request_events")
            self.assertEqual(
                tapd_intake.admit_mixed_round(conn, self.configured, self.ticket, claim, **options),
                (None, False),
            )
        self.assertEqual(self.read("SELECT * FROM request_events"), ledger)
        self.assertFalse(self.read("SELECT round_id FROM source_rounds WHERE round_id=?", (options["round_id"],)))
        refreshed = source_github_observation.refresh_batch(
            self.db, self.configured, claim, self.ticket, revision=revision, request_ids=request_ids, runner=run
        )
        self.assertNotEqual(refreshed.operation_id, batch.operation_id)
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            options["operations"] = ((refreshed.operation_id, request_ids),)
            resumed, proven = tapd_intake.admit_mixed_round(conn, self.configured, self.ticket, claim, **options)
            self.assertTrue(proven)
            self.assertEqual(resumed["round_id"], options["round_id"])

    def test_github_bot_account_drift_holds_without_treating_original_marker_as_removed(self):
        self._delivered_previous_pr()
        self._admit_github_discussion([self._github_discussion()])
        self.configured["repos"][0]["github_account"] = "replacement-bot"
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE repos SET github_account='replacement-bot' WHERE full_name='org/alpha'")
        calls, run = self._current_github_runner()
        result = self.tick("held", github_discussion_runner=run)
        self.assertEqual(result.reason, "github_observation_unavailable")
        self.assertEqual(calls, [])
        self.assertEqual(
            self.read(
                "SELECT phase,state FROM ingress_requests JOIN request_events USING(request_id) WHERE provider='github'"
            ),
            [("held", "pending")],
        )
        operation, status, checked = self._github_observation()
        self.assertIsNotNone(operation)
        self.assertEqual((status, checked["reason"]), ("unavailable", "github_request_binding_unavailable"))

    def test_old_github_development_entry_is_paused_and_new_readonly_can_admit(self):
        self._delivered_previous_pr()
        self._admit_github_discussion([self._github_discussion()])
        self.labels = ()
        _, run = self._current_github_runner()
        self.tick("held", ticket=self.ticket, github_discussion_runner=run)
        self.assertEqual(self.read("SELECT phase FROM ingress_requests WHERE provider='github'"), [("paused",)])
        self._admit_github_discussion([self._github_discussion(identifier=618)])
        _, second = self._current_github_runner(identifier=618)

        def both(command, **options):
            if command[7].endswith("/618") or "id=current-comment-618" in command:
                return second(command, **options)
            return run(command, **options)

        admitted = self.tick("admitted", ticket=self.ticket, github_discussion_runner=both)
        self.assertEqual(
            self.read(
                "SELECT e.event_id FROM request_events e JOIN source_round_requests r USING(request_id) WHERE r.round_id=?",
                (admitted.round_id,),
            ),
            [("event:comment:618",)],
        )
        self.assertCountEqual(
            self.read("SELECT phase FROM ingress_requests WHERE provider='github'"), [("paused",), ("pending",)]
        )

    def test_github_mixed_admission_proves_more_than_one_batch_despite_malformed_oldest(self):
        self._delivered_previous_pr()
        identifiers = tuple(range(617, 624))
        self._admit_github_discussion([self._github_discussion(identifier=identifier) for identifier in identifiers])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE source_github_ingress SET target_json='{}' WHERE event_id='event:comment:617'")
        runners = {identifier: self._current_github_runner(identifier=identifier)[1] for identifier in identifiers}
        selected = identifiers[0]

        def run(command, **options):
            nonlocal selected
            if "/issues/comments/" in command[7]:
                selected = int(command[7].rsplit("/", 1)[-1])
            return runners[selected](command, **options)

        step = self.tick("admitted", github_discussion_runner=run)
        self.assertEqual(
            self.read("SELECT COUNT(*) FROM source_round_requests WHERE round_id=?", (step.round_id,)), [(6,)]
        )
        self.assertEqual(self.read("SELECT COUNT(DISTINCT operation_id) FROM source_github_observations"), [(2,)])
        [[encoded]] = self.read(
            "SELECT o.evidence_json FROM source_github_observations o JOIN source_github_ingress b USING(request_id) "
            "WHERE b.event_id='event:comment:617'"
        )
        self.assertEqual(json.loads(encoded)["reason"], "github_request_binding_unavailable")
        self.assertEqual(self.read("SELECT COUNT(*) FROM source_github_observations WHERE status='present'"), [(6,)])

    def _fixture_promote_github_member(self, owned, *, suffix="", **item_changes):
        from robert_agent.persistence.rounds.source_evidence import context_digest

        snapshot = json.loads(owned.request["content_snapshot_json"])
        target = owned.return_target
        snapshot.update(event_id=target["event_id"], event_fingerprint=target["event_fingerprint"])
        snapshot["body"] += suffix
        snapshot["item"].update(item_changes)
        version = context_digest([target["event_id"], snapshot["body"], snapshot["actor_login"].casefold()])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE ingress_requests SET phase='pending',content_snapshot_json=?,content_version=? WHERE request_id=?",
                (json.dumps(snapshot), version, owned.request["request_id"]),
            )
        return version, snapshot

    def _mixed_start(
        self, conn, claim, revision, owners, *, round_id="mixed-round", repo="repo:org/alpha", altered=False
    ):
        from robert_agent.foundation.values import utc_now
        from robert_agent.persistence.queries.ingress_requests import find_by_id
        from robert_agent.persistence.stores.source_sessions import start_round

        requests = []
        for owner in owners:
            row = find_by_id(conn, owner.request["request_id"])
            requests.append(
                {
                    "request_id": row["request_id"],
                    "content_version": row["content_version"],
                    "input": json.loads(row["content_snapshot_json"]),
                }
            )
        if altered:
            requests[0]["input"]["body"] = "Different input"
        return start_round(
            conn,
            claim,
            round_id=round_id,
            member_versions={row["request_id"]: row["content_version"] for row in requests},
            input_version=revision,
            input_snapshot={"requests": requests},
            capability="readonly",
            repo_ids=(repo,),
            publication_scope={"provider": "tapd"},
            now=utc_now(),
        )

    def test_mixed_round_freezes_real_provider_triggers_and_can_refreeze_same_members(self):
        from robert_agent.foundation.values import utc_now
        from robert_agent.persistence import transactions
        from robert_agent.persistence.queries import source_members, source_tasks, tapd_intake as tapd_reads
        from robert_agent.persistence.stores import source_sessions

        claim, revision, owners = self._mixed_pending_fixture()
        tapd, github = owners["tapd"], owners["github"]
        self.assertEqual(tapd.request["source_id"], self.ticket.source_id)
        self.assertEqual(github.request["source_id"], "source:github:org/alpha!17")
        self.assertEqual(github.request["phase"], "held")
        self.assertEqual(github.original_item["author"], "owner")
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            self.assertEqual(
                [row["provider"] for row in tapd_reads.pending_requests(conn, self.ticket.source_id)], ["tapd"]
            )
            self.assertIsNone(self._mixed_start(conn, claim, revision, owners.values()))
        first_version, first_snapshot = self._fixture_promote_github_member(github)
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            self.assertIsNone(self._mixed_start(conn, claim, revision, owners.values(), altered=True))
            round_ = self._mixed_start(conn, claim, revision, owners.values())
            self.assertIsNotNone(round_)
            self.assertEqual(
                {tuple(row) for row in source_tasks.round_trigger_events(conn, "mixed-round")},
                {
                    (member.request["request_id"], member.events[0]["event_id"], member.events[0]["event_fingerprint"])
                    for member in owners.values()
                },
            )
            [github_frozen] = [
                item for item in source_members.frozen_members(conn, "mixed-round") if item.ownership.return_target
            ]
            self.assertEqual(github_frozen.ownership.return_target, github.return_target)
            self.assertEqual(github_frozen.member["content_version"], first_version)
        edited, latest = self._fixture_promote_github_member(
            github, suffix=" new body", updated_at="2026-11-14T20:02:00Z"
        )
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            self.assertEqual(len(source_sessions.pending_round_members(conn, "mixed-round")), 2)
            self.assertFalse(source_sessions.pending_round_members(conn, "mixed-round", require_current_versions=True))
            [original] = [
                item for item in source_members.frozen_members(conn, "mixed-round") if item.ownership.return_target
            ]
            self.assertEqual(json.loads(original.member["content_snapshot_json"]), first_snapshot)
            self.assertTrue(
                source_sessions.finish_round(conn, claim, round_id="mixed-round", outcome="canceled", now=utc_now())
            )
            self.assertIsNotNone(self._mixed_start(conn, claim, revision, owners.values(), round_id="new-round"))
            [renewed] = [
                item for item in source_members.frozen_members(conn, "new-round") if item.ownership.return_target
            ]
            self.assertEqual(renewed.member["content_version"], edited)
            self.assertEqual(json.loads(renewed.member["content_snapshot_json"]), latest)

    def test_mixed_membership_refuses_identity_corruption_and_prior_ordinary_ownership(self):
        from robert_agent.foundation import values
        from robert_agent.persistence import transactions
        from robert_agent.persistence.queries import source_members
        from robert_agent.persistence.stores import ingress_requests, task_events

        claim, revision, owners = self._mixed_pending_fixture()
        github = owners["github"]
        request_id = github.request["request_id"]
        event_id = github.return_target["event_id"]
        self._fixture_promote_github_member(github)
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            self.assertIsNone(source_members.owned_request(conn, "other-tapd-source", request_id))
            self.assertIsNone(self._mixed_start(conn, claim, revision, owners.values(), repo="foreign"))
            with self.assertRaisesRegex(ValueError, "must belong"):
                ingress_requests.enqueue(
                    conn,
                    request_id="bad-cross-source",
                    source_id=self.ticket.source_id,
                    provider="tapd",
                    auth_type="mention",
                    authorization={},
                    content_snapshot={},
                    content_version="x",
                    event_ids=(event_id,),
                    now=values.utc_now(),
                )
        corruptions = (
            ("source_github_ingress", "target_json", "event_id", "foreign", "request_id", request_id),
            ("source_github_ingress", "target_json", "pr_number", 18, "request_id", request_id),
            ("source_github_ingress", "target_json", "repo_id", "repo:org/other", "request_id", request_id),
            ("ingress_requests", "content_snapshot_json", "item.author", "stranger", "request_id", request_id),
            (
                "ingress_requests",
                "content_snapshot_json",
                "item.created_at",
                "2026-11-14T19:00:00Z",
                "request_id",
                request_id,
            ),
            ("publication_intents", "remote_evidence_json", "receipt", {}, "action_id", self.action_ids["open_pr"]),
        )
        for table, field, path, value, key, identity in corruptions:
            with self.subTest(table=table, path=path):
                [[original]] = self.read(f"SELECT {field} FROM {table} WHERE {key}=?", (identity,))
                stmt = f"UPDATE {table} SET {field}=? WHERE {key}=?"
                with closing(sqlite3.connect(self.db)) as conn, conn:
                    modified = json.loads(original)
                    current, _, leaf = path.rpartition(".")
                    parent = modified[current] if current else modified
                    parent[leaf] = value
                    conn.execute(stmt, (json.dumps(modified), identity))
                with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
                    transactions.begin_immediate(conn)
                    self.assertIsNone(source_members.owned_request(conn, self.ticket.source_id, request_id))
                    self.assertIsNone(self._mixed_start(conn, claim, revision, owners.values()))
                with closing(sqlite3.connect(self.db)) as conn, conn:
                    conn.execute(stmt, (original, identity))
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE ingress_requests SET provider='tapd' WHERE request_id=?", (request_id,))
        with closing(database.connect(self.db, foreign_keys=True)) as conn:
            self.assertIsNone(source_members.owned_request(conn, self.ticket.source_id, request_id))
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE ingress_requests SET provider='github' WHERE request_id=?", (request_id,))
        [[old_task]] = self.read("SELECT task_id FROM github_actions WHERE action_id=?", (self.action_ids["open_pr"],))
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            task_events.bind_task_event(conn, old_task, event_id, "trigger", values.utc_now())
            self.assertIsNone(source_members.owned_request(conn, self.ticket.source_id, request_id))
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("DELETE FROM task_events WHERE task_id=? AND event_id=?", (old_task, event_id))
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            ingress_requests.enqueue(
                conn,
                request_id="duplicate",
                source_id=github.request["source_id"],
                provider="github",
                auth_type="test",
                authorization={},
                content_snapshot={},
                content_version="other",
                event_ids=(event_id,),
                now=values.utc_now(),
            )
            self.assertIsNone(source_members.owned_request(conn, self.ticket.source_id, request_id))

    def test_accepted_mixed_result_requires_exact_frozen_ledger_and_return_target(self):
        from robert_agent.foundation.values import utc_now
        from robert_agent.persistence import transactions
        from robert_agent.persistence.queries import source_members
        from robert_agent.persistence.stores import source_results, source_sessions, task_events

        claim, revision, owners = self._mixed_pending_fixture()
        github = owners["github"]
        self._fixture_promote_github_member(github)
        now = utc_now()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "INSERT INTO tasks(task_id,workstream_id,lifecycle,route_id,created_at,updated_at)"
                " VALUES('mixed-task',?,'running','comment-analysis',?,?)",
                (github.return_target["origin_workstream_id"], now, now),
            )
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            round_ = self._mixed_start(conn, claim, revision, owners.values())
            self.assertIsNotNone(round_)
            self.assertTrue(
                source_sessions.bind_task(
                    conn,
                    claim,
                    round_id="mixed-round",
                    request_id=github.request["request_id"],
                    task_id="mixed-task",
                    capability="readonly",
                    now=now,
                )
            )
            handled = []
            for frozen in source_members.frozen_members(conn, "mixed-round"):
                [event] = frozen.ownership.events
                task_events.bind_task_event(conn, "mixed-task", event["event_id"], "trigger", now)
                handled.append(
                    {
                        "request_id": frozen.member["request_id"],
                        "content_version": frozen.member["content_version"],
                        "events": [
                            {
                                "row_id": event["row_id"],
                                "event_id": event["event_id"],
                                "fingerprint": event["event_fingerprint"],
                            }
                        ],
                    }
                )
        fingerprints = [item["events"][0]["fingerprint"] for item in handled]
        row_ids = [item["events"][0]["row_id"] for item in handled]
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "INSERT INTO attempts(attempt_id,task_id,attempt_no,status) VALUES('mixed-attempt','mixed-task',1,'completed')"
            )
            conn.execute(
                "INSERT INTO worker_results(result_id,task_id,attempt_id,output_type,consumed_event_fingerprints_json,created_at,metadata_json)"
                " VALUES('mixed-worker','mixed-task','mixed-attempt','comment_analysis',?,?,?)",
                (
                    json.dumps(fingerprints),
                    now,
                    json.dumps(
                        {
                            "audit": {"status": "accepted", "ok": True},
                            "result_contract": "native-v1",
                            "submission_fingerprint": "a" * 64,
                        }
                    ),
                ),
            )
        evidence = {
            "task_id": "mixed-task",
            "attempt_id": "mixed-attempt",
            "repo_id": "repo:org/alpha",
            "round_input_sha256": round_["input_sha256"],
            "handled_members": handled,
            "submission_fingerprint": "a" * 64,
        }
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            source_results.insert_result(
                conn,
                source_result_id="mixed-root",
                source_id=self.ticket.source_id,
                round_id="mixed-round",
                kind="answer",
                input_version=revision,
                idempotency_key="mixed-fixture",
                evidence=evidence,
                payload={},
                now=now,
                worker_result_id="mixed-worker",
            )
            with self.assertRaisesRegex(ValueError, "return targets"):
                source_results.consume_accepted_members(conn, source_result_id="mixed-root", row_ids=row_ids, now=now)
        evidence["github_return_targets"] = [
            {"request_id": github.request["request_id"], "target": github.return_target}
        ]
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE source_results SET evidence_json=? WHERE source_result_id='mixed-root'", (json.dumps(evidence),)
            )
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            for invalid_ids in (row_ids[:1], row_ids + row_ids[:1], row_ids + [10_000]):
                with self.assertRaises(ValueError):
                    source_results.consume_accepted_members(
                        conn, source_result_id="mixed-root", row_ids=invalid_ids, now=now
                    )
            with self.assertRaisesRegex(ValueError, "lacks required GitHub feedback"):
                source_results.consume_accepted_members(conn, source_result_id="mixed-root", row_ids=row_ids, now=now)
            self.assertFalse(source_members.consumed_root_requests(conn, self.ticket.source_id, "mixed-round"))

    def test_preexisting_pr_never_blocks_classification_or_qa(self):
        from robert_agent.adapters.github import pull_requests

        self._delivered_previous_pr()
        with patch.object(pull_requests, "read_pr", side_effect=OSError("PR unavailable")) as read:
            self._followup_classification("comment-analysis")
            self.tick("prepared")
            self.tick("dispatched")
            self.submit(
                "comment_analysis",
                planned_actions=[{"provider": "tapd", "type": "comment", "target_id": "ticket", "body": "Answered."}],
            )
            self.tick("waiting")
            self.tick("audited")
            self.tick("publication_pending")
        read.assert_not_called()
        self.assertEqual(self.read("SELECT COUNT(*) FROM source_pr_selections"), [(0,)])

    def test_merged_history_allows_fresh_current_base_but_closed_unmerged_holds(self):
        from robert_agent.adapters.github import pull_requests
        from robert_agent.persistence.schema.database import init_database

        bodies, tapd = self._delivered_previous_pr()
        round_id = self._followup_classification("new-pr")
        old_round = self.read("SELECT * FROM source_rounds WHERE round_id=?", (round_id,))
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("DROP TABLE source_pr_selections")
        init_database(self.db)
        self.assertEqual(self.read("SELECT * FROM source_rounds WHERE round_id=?", (round_id,)), old_round)
        observation = {
            "number": 17,
            "html_url": "https://github.com/org/alpha/pull/17",
            "state": "closed",
            "merged": False,
            "merged_at": None,
            "user": {"login": "bot"},
            "head": {"repo": {"full_name": "bot/alpha"}, "ref": self.branch, "sha": self.head},
            "base": {"repo": {"full_name": "org/alpha"}, "ref": "main", "sha": self.base},
        }

        def new_checkout(repo, purpose, *, pr=None):
            self.assertIsNone(pr)
            branch = f"codex/dd-{purpose['workspace_key']}-source-work"
            target = Path(repo["worktree_root"]) / "second"
            self.git(Path(repo["repo_root"]), "worktree", "add", "-b", branch, str(target), self.base)
            return {"ok": True, "worktree_path": str(target), "branch_name": branch}

        with (
            patch.object(pull_requests, "read_pr", return_value=observation) as read,
            patch.object(workspace, "prepare_source_worktree", side_effect=new_checkout) as prepare,
        ):
            self.assertEqual(self.tick("held").reason, "source_pr_closed_unmerged")
            prepare.assert_not_called()
            decision_before = self.read("SELECT decision_json FROM source_pr_selections WHERE round_id=?", (round_id,))
            observation.update(merged=True, merged_at="2026-11-14T00:00:00Z")
            observation["base"]["ref"] = "historical-target"

            def lost_fence(*_args, **_kwargs):
                with closing(sqlite3.connect(self.db)) as conn, conn:
                    conn.execute("UPDATE source_sessions SET owner='other' WHERE source_id=?", (self.ticket.source_id,))
                return observation

            read.side_effect = lost_fence
            self.assertEqual(self.tick("held").reason, "source_pr_scope_changed")
            self.assertEqual(
                self.read("SELECT decision_json FROM source_pr_selections WHERE round_id=?", (round_id,)),
                decision_before,
            )
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute(
                    "UPDATE source_sessions SET owner='test-coordinator' WHERE source_id=?", (self.ticket.source_id,)
                )
            read.side_effect = None
            task = self.tick("prepared")
            self.tick("dispatched")
        [[route, raw]] = self.read(
            "SELECT route_id,attempts.metadata_json FROM tasks JOIN attempts USING(task_id) WHERE task_id=?",
            (task.identity,),
        )
        self.assertEqual(route, "new-pr")
        info = json.loads(raw)
        self.assertIs(info["native_workspace"]["pr_selection_required"], True)
        self.assertEqual(info["native_route_resolution"]["reason"], "source_pr_fresh_branch")

        [[tree, branch]] = self.read("SELECT worktree_path,branch_name FROM attempts WHERE task_id=?", (task.identity,))
        self.tree, self.branch = Path(tree), branch
        (self.tree / "README.md").write_text("New work after the previous PR merged")
        self.git(self.tree, "add", "README.md")
        self.git(self.tree, "commit", "-m", tapd_policy.NATIVE_COMMIT_MESSAGE)
        self.head = self.git(self.tree, "rev-parse", "HEAD")
        actions = native_contract.selectors_for("new_pr")
        actions[-1]["body"] = "Updated README.md for the new request"
        self.submit(
            "new_pr",
            planned_actions=actions,
            verification=[
                {
                    "command": ["pytest"],
                    "source": "worker_reported",
                    "exit_code": 0,
                    "status": "passed",
                    "required": True,
                    "purpose": "local test",
                }
            ],
        )
        self.tick("waiting")
        self.tick("audited")
        self.root_id = self.tick("publication_pending").identity
        self.action_ids = dict(
            self.read(
                "SELECT action_type,action_id FROM github_actions WHERE provider='github' AND task_id=?",
                (task.identity,),
            )
        )
        calls, _, _, github = self.runner(pr_number=18, pr_edit=lambda pr: pr.update(state="open"))

        def read_history(command, **kwargs):
            if command[:5] == ["gh", "api", "--hostname", "github.com", "repos/org/alpha/pulls/17"]:
                return SimpleNamespace(returncode=0, stdout=json.dumps(observation), stderr="")
            return github(command, **kwargs)

        observation.update(state="open", merged=False, merged_at=None)
        held = self.publish("push_existing_pr", read_history)
        self.assertEqual((held.status, held.reason), ("deferred", "native_preflight_refused"))
        self.assertFalse(any(call[:2] == ("git", "push") for call in calls))
        observation.update(state="closed", merged=True, merged_at="2026-11-14T00:00:00Z")
        self.assertEqual(self.publish("push_existing_pr", read_history).status, "published")
        self.assertEqual(self.publish("open_pr", read_history).status, "published")
        self.assertEqual(tapd()["native_completed_count"], 1)
        self.assertEqual(len(bodies), 2)
        self.assertIn("https://github.com/org/alpha/pull/18", bodies[1])
        self.assertEqual(self.read("SELECT COUNT(*) FROM sources WHERE source_type='pull_request'"), [(2,)])
        self.assertEqual(self.read("SELECT COUNT(*) FROM work_items"), [(1,)])
        self.assertEqual(sum(call[:3] == ("gh", "pr", "create") for call in calls), 1)

    def test_public_tapd_delivery_links_confirmed_pr_then_next_round_updates_it(self):
        self._confirmed_pr_next_round(recover=False)

    def test_recovery_updates_existing_canonical_pr_without_reexecuting_worker(self):
        self._confirmed_pr_next_round(recover=True)

    def _confirmed_pr_next_round(self, *, recover):
        from robert_agent.persistence.queries import source_pr_lineage
        from robert_agent.adapters.github import pull_requests

        self.accepted_development()
        calls, _, _, github = self.runner(pr_edit=lambda pr: pr.update(state="open"))
        bodies, tapd = self._tapd_publisher()
        first_root = self.root_id
        original = self.read("SELECT action_id,metadata_json FROM github_actions ORDER BY action_id")
        [[item, control]] = self.read("SELECT work_item_id,workstream_id FROM work_items")
        self.assertEqual(tapd()["pending_count"], 0)
        self.assertEqual(self.publish("push_existing_pr", github).status, "published")
        self.assertEqual(tapd()["pending_count"], 0)
        self.assertEqual(self.publish("open_pr", github).status, "published")
        self.assertEqual(self.read("SELECT COUNT(*) FROM sources WHERE source_type='pull_request'"), [(0,)])

        # Simulate restart after the two GitHub effects committed. The public
        # TAPD lane recovers the local PR link before writing its only comment.
        done = tapd()
        self.assertEqual((done["native_published_count"], done["native_completed_count"]), (1, 1), done)
        self.assertEqual(len(bodies), 1)
        self.assertIn("Updated README.md", bodies[0])
        self.assertIn("https://github.com/org/alpha/pull/17", bodies[0])
        self.assertIn("Modified", bodies[0])
        self.assertIn("Worker-reported", bodies[0])
        self.assertNotIn("robert-workstream", bodies[0])
        self.assertNotIn("TOP-SECRET-771", bodies[0])
        self.assertEqual(self.read("SELECT action_id,metadata_json FROM github_actions ORDER BY action_id"), original)
        self.assertEqual(self.read("SELECT work_item_id,workstream_id FROM work_items"), [(item, control)])
        self.assertEqual(self.read("SELECT status FROM source_rounds"), [("execution_finished",)])
        self.assertEqual(tapd()["pending_count"], 0)
        with closing(database.connect(self.db)) as conn:
            [lineage] = source_pr_lineage.linked_pr_lineages(
                conn, source_id=self.ticket.source_id, repo_id="repo:org/alpha"
            )
        self.assertEqual(
            (lineage.source_result_id, lineage.action_id, lineage.origin_workstream_id),
            (first_root, self.action_ids["open_pr"], control),
        )
        self.assertEqual((lineage.head_sha, lineage.pinned_base_sha), (self.head, self.base))

        self.initial_pr_head = self.head
        round_id = self._followup_classification("new-pr")
        observed_head = self.head

        def current_pr(_repo, _number, **_):
            return {
                "number": lineage.number,
                "html_url": lineage.url,
                "state": "open",
                "merged": False,
                "merged_at": None,
                "user": {"login": lineage.author},
                "head": {
                    "repo": {"full_name": lineage.head_repository},
                    "ref": lineage.head_branch,
                    "sha": observed_head,
                },
                "base": {"repo": {"full_name": lineage.repository}, "ref": lineage.base_branch, "sha": self.base},
            }

        follow = {}

        def reuse_checkout(_repo, _purpose, *, pr=None):
            self.assertEqual(pr["number"], lineage.number)
            return {"ok": True, "worktree_path": str(self.tree), "branch_name": self.branch}

        with (
            patch.object(workspace, "prepare_source_worktree", side_effect=reuse_checkout),
            patch.object(pull_requests, "read_pr", side_effect=current_pr),
        ):
            prepared = self.tick("prepared", **follow)
            [[frozen]] = self.read("SELECT metadata_json FROM attempts WHERE task_id=?", (prepared.identity,))
            resolution = json.loads(frozen)["native_route_resolution"]
            self.assertEqual(
                (resolution["recommended_route"], resolution["selected_route"]), ("new-pr", "update-existing-pr")
            )
            observed_head = "a" * 40
            self.assertEqual(self.tick("held", **follow).reason, "source_pr_prepared_target_changed")
            self.assertEqual(
                self.read("SELECT metadata_json FROM attempts WHERE task_id=?", (prepared.identity,)), [(frozen,)]
            )
            observed_head = self.head
            self.tick("dispatched", **follow)
        (self.tree / "README.md").write_text("Follow-up repository behavior")
        self.git(self.tree, "add", "README.md")
        self.git(self.tree, "commit", "-m", tapd_policy.NATIVE_COMMIT_MESSAGE)
        self.head = self.git(self.tree, "rev-parse", "HEAD")
        actions = native_contract.selectors_for("update_existing_pr")
        actions[-1]["body"] = "Updated README.md again"
        self.submit(
            "update_existing_pr",
            planned_actions=actions,
            verification=[
                {
                    "command": ["pytest"],
                    "source": "worker_reported",
                    "exit_code": 0,
                    "status": "passed",
                    "required": True,
                    "purpose": "local test",
                }
            ],
        )
        self.tick("waiting", **follow)
        self.tick("audited", **follow)
        second = self.tick("publication_pending", **follow)
        self.root_id = second.identity
        self.action_ids = dict(
            self.read(
                "SELECT action_type,action_id FROM github_actions WHERE provider='github' AND task_id=?",
                (prepared.identity,),
            )
        )
        self.assertEqual(set(self.action_ids), {"push_existing_pr"})
        update_calls, state, _, github_update = self.runner(pr_number=17)
        if recover:
            worker_count = len(self.started)
            self._offer_missing_github_recovery(github_update)
            self.tick("publication_pending", ticket=self.ticket, github_discussion_runner=github_update)
            self.assertEqual(self.publish("push_existing_pr", github_update).status, "published")
            [[action]] = self.read(
                "SELECT a.action_id FROM source_required_actions a JOIN github_actions g ON g.action_id=a.action_id "
                "WHERE a.source_result_id=? AND g.provider='tapd'",
                (self.root_id,),
            )
            send, _ = self._recovery_tapd_client(github_update, [])
            self.assertEqual(send(self.root_id, action).completion.status, "completed")
            self.assertEqual(len(self.started), worker_count)
            self.assertEqual(sum(call[:2] == ("git", "push") for call in update_calls), 1)
            self.assertFalse([call for call in update_calls if call[:3] == ("gh", "pr", "create")])
            return
        from robert_agent.delivery.application.publish import publish_ready_actions

        [[saved_round, saved_repo, timestamp, decision]] = self.read(
            "SELECT * FROM source_pr_selections WHERE round_id=?", (round_id,)
        )
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("DELETE FROM source_pr_selections WHERE round_id=?", (round_id,))
        refused = self.publish("push_existing_pr", github_update)
        self.assertEqual((refused.status, refused.reason), ("deferred", "native_pr_selection_unavailable"))
        self.assertEqual(update_calls, [])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "INSERT INTO source_pr_selections VALUES(?,?,?,?)", (saved_round, saved_repo, timestamp, decision)
            )
            conn.execute("UPDATE tapd_coordination SET next_due_at='2000-01-01T00:00:00+00:00'")
        with patch.object(pull_requests, "read_pr", return_value={**current_pr(None, None), "state": "closed"}):
            refused = self.publish("push_existing_pr", github_update)
        self.assertEqual((refused.status, refused.reason), ("deferred", "native_preflight_refused"))
        self.assertEqual(
            self.read(
                "SELECT COUNT(*) FROM publication_intents WHERE action_id=?", (self.action_ids["push_existing_pr"],)
            ),
            [(0,)],
        )
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE tapd_coordination SET next_due_at='2000-01-01T00:00:00+00:00'")
        advanced = publish_ready_actions(
            self.db,
            config=self.configured,
            provider="github",
            source_owner="test-coordinator",
            tapd_client_factory=self.factory,
            tapd_observation_provider=self.provider,
            run_command=github_update,
            limit=1,
        )
        self.assertEqual((advanced["pending_count"], advanced["native_published_count"]), (1, 1), advanced)
        self.assertEqual(state["sha"], self.head)
        sent = tapd()
        self.assertEqual((sent["native_published_count"], sent["native_completed_count"]), (1, 1), sent)
        self.assertIn("https://github.com/org/alpha/pull/17", bodies[1])
        self.assertEqual(self.read("SELECT COUNT(*) FROM sources WHERE source_type='pull_request'"), [(1,)])
        self.assertEqual(self.read("SELECT work_item_id,workstream_id FROM work_items"), [(item, control)])
        self.assertEqual(len([cmd for cmd in calls + update_calls if cmd[:3] == ("gh", "pr", "create")]), 1)
        with closing(database.connect(self.db)) as conn:
            self.assertEqual(
                source_pr_lineage.linked_pr_lineages(conn, source_id=self.ticket.source_id, repo_id="repo:org/alpha"),
                (lineage,),
            )

    def _discover_pr_before_local_link(self, *, with_task=False):
        from robert_agent.foundation.values import utc_now

        now = utc_now()
        source_id = "scanner-pr-source"
        stream = "scanner-pr-stream"
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                """INSERT INTO sources(source_id,repo_id,source_key,source_type,provider,host,project_key,external_id,
                                       number,html_url,title,state,source_updated_at)
                   VALUES(?,'repo:org/alpha','scanner-pr-key','pull_request','github','github.com','ORG/ALPHA','17',
                          17,'https://github.com/ORG/ALPHA/pull/17','Observed newer title','closed',?)""",
                (source_id, now),
            )
            conn.execute(
                """INSERT INTO workstreams(workstream_id,repo_id,primary_source_id,lifecycle,created_at,updated_at,metadata_json)
                   VALUES(?,'repo:org/alpha',?,'completed',?,?,'{"existing_history":"preserve"}')""",
                (stream, source_id, now, now),
            )
            if with_task:
                conn.execute(
                    """INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at)
                       VALUES('scanner-task',?,'completed',?,?)""",
                    (stream, now, now),
                )
                conn.execute(
                    """INSERT INTO attempts(attempt_id,task_id,attempt_no,status,metadata_json)
                       VALUES('scanner-attempt','scanner-task',1,'canceled','{"dispatch":{"pid":12345}}')"""
                )
        return source_id, stream

    def test_readonly_link_recovery_adopts_exact_scanner_pr_after_revocation_and_process_exit(self):
        from robert_agent.delivery.application.source_publication_queue import source_publication_ready
        from robert_agent.foundation.values import utc_now
        from robert_agent.persistence import transactions
        from robert_agent.persistence.queries import source_pr_lineage
        from robert_agent.work.application import source_observation, tapd_intake

        self.accepted_development()
        calls, _, _, github = self.runner(pr_edit=lambda pr: pr.update(state="open"))
        self.assertEqual(self.publish("push_existing_pr", github).status, "published")
        self.assertEqual(self.publish("open_pr", github).status, "published")
        source, stream = self._discover_pr_before_local_link(with_task=True)
        self.terminal = True
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            observation = self.provider(None, self.ticket)
            tapd_intake.observe_ticket(
                conn,
                self.configured["tapd"],
                observation,
                source_observation.publication_facts(conn, self.ticket),
                now=utc_now(),
            )
        self.assertEqual(self.read("SELECT COUNT(*) FROM source_round_cancellations"), [(1,)])
        bodies, tapd = self._tapd_publisher()
        local = {"limit": 0, "tapd_identity_provider": None, "tapd_observation_provider": None}
        from robert_agent.persistence.queries import source_results

        with closing(database.connect(self.db)) as conn:
            scope = {
                "scopes": [(self.ticket.host, self.ticket.workspace_id)],
                "owner": "test-coordinator",
                "now": utc_now(),
                "limit": 1,
            }
            inspect = source_results.completion_candidates(conn, **scope, linkage_only=True)
        self.assertTrue(
            inspect, self.read("SELECT source_id,round_id,status,reason,next_due_at,updated_at FROM tapd_coordination")
        )
        wrong_scope = tapd(action_ids=["foreign"], **local)
        self.assertEqual(wrong_scope["native_completion_held_count"], 0)
        held = tapd(**local)
        self.assertEqual((held["native_completed_count"], held["native_completion_held_count"]), (0, 1), held)
        self.assertEqual(
            self.read("SELECT origin_workstream_id FROM workstreams WHERE workstream_id=?", (stream,)), [(None,)]
        )
        with closing(database.connect(self.db)) as conn:
            self.assertFalse(source_publication_ready(conn, self.configured, source_owner="test-coordinator"))
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE attempts SET metadata_json=json_set(metadata_json,'$.process_stopped_at',?) WHERE attempt_id='scanner-attempt'",
                (utc_now(),),
            )
            conn.execute("UPDATE tapd_coordination SET next_due_at='2000-01-01T00:00:00+00:00'")
        done = tapd(**local)
        self.assertEqual((done["native_completed_count"], done["native_completion_held_count"]), (0, 0), done)
        self.assertEqual(bodies, [])
        self.assertEqual(self.read("SELECT COUNT(*) FROM source_round_cancellations"), [(1,)])
        self.assertEqual(self.read("SELECT status FROM source_rounds"), [("active",)])
        self.assertEqual(
            self.read("SELECT title,state FROM sources WHERE source_id=?", (source,)),
            [("Observed newer title", "closed")],
        )
        self.assertEqual(
            self.read(
                "SELECT json_extract(metadata_json,'$.existing_history') FROM workstreams WHERE workstream_id=?",
                (stream,),
            ),
            [("preserve",)],
        )
        with closing(database.connect(self.db)) as conn:
            [link] = source_pr_lineage.linked_pr_lineages(
                conn, source_id=self.ticket.source_id, repo_id="repo:org/alpha"
            )
            self.assertEqual((link.workstream_id, link.pr_source_id, link.state), (stream, source, "closed"))
            self.assertFalse(source_publication_ready(conn, self.configured, source_owner="test-coordinator"))
        self.assertEqual(len([cmd for cmd in calls if cmd[:3] == ("gh", "pr", "create")]), 1)

    def test_native_repair_budget_uses_mock_published_adopted_pr_original_base(self):
        from dataclasses import replace

        from robert_agent.foundation.values import utc_now
        from robert_agent.persistence.queries import repair_cycles as read_cycles, source_pr_lineage
        from robert_agent.persistence.stores import source_sessions
        from robert_agent.work.application.repair_cycles import PullObservation, apply_observation, claim_repair

        self.accepted_development()
        _, _, _, github = self.runner(pr_edit=lambda pr: pr.update(state="open"))
        self.assertEqual(self.publish("push_existing_pr", github).status, "published")
        self.assertEqual(self.publish("open_pr", github).status, "published")
        pr_source_id, pr_stream = self._discover_pr_before_local_link()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE sources SET state='open' WHERE source_id=?", (pr_source_id,))
        _, tapd = self._tapd_publisher()
        self.assertEqual(tapd()["native_completed_count"], 1)
        config = self.configured["tapd"]
        config["projects"][0]["rules"][0]["repair"]["conflict"]["enabled"] = True
        round_id = self._followup_classification("new-pr")
        now = utc_now()

        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            owner, token = conn.execute(
                "SELECT owner,token FROM source_sessions WHERE source_id=?", (self.ticket.source_id,)
            ).fetchone()
            claim = source_sessions.SourceClaim(self.ticket.source_id, owner, token)
            request_id = conn.execute(
                "SELECT request_id FROM source_round_requests WHERE round_id=?", (round_id,)
            ).fetchone()[0]
            revision = conn.execute(
                "SELECT json_extract(metadata_json,'$.revision') FROM sources WHERE source_id=?",
                (self.ticket.source_id,),
            ).fetchone()[0]
            [original] = source_pr_lineage.linked_pr_lineages(
                conn, source_id=self.ticket.source_id, repo_id="repo:org/alpha"
            )
            self.assertEqual((original.pr_source_id, original.workstream_id), (pr_source_id, pr_stream))
            self.assertEqual((original.base_branch, original.pinned_base_sha), ("main", self.base))

            # Model the later scheduler's concrete attached subject without starting
            # an automatic repair path here.
            conn.execute(
                "INSERT INTO pr_remediation_episodes(episode_id,workstream_id,episode_kind,subject_key,"
                "observed_head_sha,observed_base_sha,status,first_seen_at,updated_at)"
                " VALUES('repair-episode',?,'merge_conflict',?,?,?,'open',?,?)",
                (pr_stream, f"{self.head}:{self.base}", self.head, self.base, now, now),
            )
            conn.execute(
                "INSERT INTO tasks(task_id,workstream_id,task_kind,lifecycle,created_at,updated_at,metadata_json)"
                " VALUES('repair-task',?,'merge_conflict_remediation','queued',?,?,"
                " json_object('remediation_episode_id','repair-episode'))",
                (pr_stream, now, now),
            )
            self.assertTrue(
                source_sessions.bind_task(
                    conn,
                    claim,
                    round_id=round_id,
                    request_id=request_id,
                    task_id="repair-task",
                    capability="development",
                    now=now,
                )
            )
            observation = PullObservation(
                pr_source_id,
                "github.com",
                "ORG/ALPHA",
                17,
                self.head,
                self.base,
                "release",
                "open",
                False,
                False,
                now,
            )

            def charge(current):
                return claim_repair(
                    conn,
                    config,
                    self.ticket,
                    claim,
                    round_id=round_id,
                    request_id=request_id,
                    revision=revision,
                    episode_id="repair-episode",
                    task_id="repair-task",
                    observation=current,
                    now=current.observed_at,
                )

            self.assertEqual(
                apply_observation(
                    conn, config, self.ticket, claim, revision=revision, observation=observation, now=now
                ).status,
                "base_ref_changed",
            )
            self.assertEqual(charge(observation).status, "base_ref_changed")
            self.assertEqual(conn.execute("SELECT count(*) FROM repair_cycles WHERE scope='tapd'").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM repair_cycle_claims").fetchone()[0], 0)
            aligned = replace(observation, base_ref=original.base_branch)
            initial = charge(aligned)
            self.assertEqual(initial.status, "claimed")
            self.assertEqual(charge(aligned).status, "already_claimed")
            cycle = read_cycles.load_cycle(conn, initial.cycle_id)
            self.assertEqual((cycle["authorized_base_ref"], cycle["started_at"]), ("main", now))
            newer = replace(aligned, head_sha="a" * 40, base_sha="b" * 40, observed_at=utc_now())
            conn.execute(
                "UPDATE pr_remediation_episodes SET observed_head_sha=?,observed_base_sha=?"
                " WHERE episode_id='repair-episode'",
                (newer.head_sha, newer.base_sha),
            )
            self.assertEqual(charge(newer).status, "already_claimed")
            self.assertEqual(read_cycles.load_cycle(conn, initial.cycle_id)["started_at"], now)
            self.assertEqual(len(read_cycles.cycle_claims(conn, initial.cycle_id)), 1)
            self.assertEqual(
                source_pr_lineage.linked_pr_lineages(conn, source_id=self.ticket.source_id, repo_id="repo:org/alpha"),
                (original,),
            )

    def test_conflicting_origin_and_mutated_receipt_never_repoint_scanner_pr(self):
        self.accepted_development()
        _, _, _, github = self.runner(pr_edit=lambda pr: pr.update(state="open"))
        self.assertEqual(self.publish("push_existing_pr", github).status, "published")
        self.assertEqual(self.publish("open_pr", github).status, "published")
        _, stream = self._discover_pr_before_local_link()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "INSERT INTO workstreams(workstream_id,repo_id,created_at,updated_at) VALUES('foreign-origin','repo:org/alpha','then','then')"
            )
            conn.execute(
                "UPDATE workstreams SET origin_workstream_id='foreign-origin' WHERE workstream_id=?", (stream,)
            )
        bodies, tapd = self._tapd_publisher()
        result = tapd()
        self.assertEqual((result["native_published_count"], bodies), (0, []))
        self.assertEqual(
            self.read("SELECT origin_workstream_id FROM workstreams WHERE workstream_id=?", (stream,)),
            [("foreign-origin",)],
        )
        action = self.action_ids["open_pr"]
        self.assertEqual(
            self.read("SELECT status FROM publication_intents WHERE action_id=?", (action,)), [("published",)]
        )
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                """UPDATE publication_intents SET remote_evidence_json=json_set(remote_evidence_json,'$.receipt.proof.pr.url',?)
                   WHERE action_id=?""",
                ("https://github.com/foreign/alpha/pull/17", action),
            )
            conn.execute("UPDATE tapd_coordination SET next_due_at='2000-01-01T00:00:00+00:00'")
        result = tapd(limit=0, tapd_observation_provider=None)
        self.assertEqual(result["native_completed_count"], 0)
        self.assertEqual(
            self.read(
                "SELECT COUNT(*) FROM workstreams WHERE json_extract(metadata_json,'$.tapd_creation.action_id') IS NOT NULL"
            ),
            [(0,)],
        )

    def test_scanner_stream_with_pending_unstarted_publication_cannot_be_adopted(self):
        from robert_agent.foundation.values import utc_now

        self.accepted_development()
        _, _, _, github = self.runner(pr_edit=lambda pr: pr.update(state="open"))
        self.assertEqual(self.publish("push_existing_pr", github).status, "published")
        self.assertEqual(self.publish("open_pr", github).status, "published")
        _, stream = self._discover_pr_before_local_link(with_task=True)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE attempts SET metadata_json=json_set(metadata_json,'$.process_stopped_at',?) WHERE attempt_id='scanner-attempt'",
                (utc_now(),),
            )
            conn.execute(
                """INSERT INTO github_actions(action_id,task_id,action_type,audit_status,publish_status,created_at)
                   VALUES('scanner-pending','scanner-task','comment','accepted','not_published',?)""",
                (utc_now(),),
            )
        _, tapd = self._tapd_publisher()
        result = tapd(limit=0, tapd_observation_provider=None)
        self.assertEqual(result["native_completion_held_count"], 1, result)
        self.assertEqual(
            self.read("SELECT origin_workstream_id FROM workstreams WHERE workstream_id=?", (stream,)), [(None,)]
        )
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE github_actions SET publish_status='skipped' WHERE action_id='scanner-pending'")
            conn.execute("UPDATE tapd_coordination SET next_due_at='2000-01-01T00:00:00+00:00'")
        recovered = tapd(limit=0, tapd_observation_provider=None)
        self.assertEqual(recovered["native_completion_held_count"], 0, recovered)
        [[origin]] = self.read("SELECT origin_workstream_id FROM workstreams WHERE workstream_id=?", (stream,))
        self.assertIsNotNone(origin)

    def test_cold_completion_reconstructs_saved_enrichment_and_refuses_changed_candidate(self):
        from robert_agent.delivery.application.source_completion import CompletionOutcome

        self.accepted_development()
        _, _, _, github = self.runner(pr_edit=lambda pr: pr.update(state="open"))
        self.assertEqual(self.publish("push_existing_pr", github).status, "published")
        self.assertEqual(self.publish("open_pr", github).status, "published")
        bodies, tapd = self._tapd_publisher()
        target = "robert_agent.delivery.application.source_completion.complete_source_result"
        with patch(target, return_value=CompletionOutcome("held", "simulated_completion_gap")):
            first = tapd()
        self.assertEqual((first["native_published_count"], first["native_completed_count"]), (1, 0), first)
        [[original]] = self.read("SELECT metadata_json FROM github_actions WHERE provider='tapd'")
        [[action_id]] = self.read("SELECT action_id FROM github_actions WHERE provider='tapd'")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE github_actions SET metadata_json=json_set(metadata_json,'$.body','Changed body') WHERE action_id=?",
                (action_id,),
            )
            conn.execute("UPDATE tapd_coordination SET next_due_at='2000-01-01T00:00:00+00:00'")
        missing = tapd(limit=0, tapd_observation_provider=None)
        self.assertEqual((missing["native_completed_count"], missing["native_completion_held_count"]), (0, 1), missing)
        self.assertEqual(self.read("SELECT status FROM source_rounds"), [("active",)])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE github_actions SET metadata_json=? WHERE action_id=?", (original, action_id))
            conn.execute("UPDATE sources SET title='Different public title' WHERE provider='github'")
            conn.execute("UPDATE tapd_coordination SET next_due_at='2000-01-01T00:00:00+00:00'")
        recovered = tapd(limit=0, tapd_observation_provider=None)
        self.assertEqual((recovered["native_completed_count"], len(bodies)), (1, 1), recovered)
        self.assertEqual(self.read("SELECT status FROM source_rounds"), [("execution_finished",)])

    def test_public_github_orders_native_actions_and_requires_shared_effect_limit(self):
        from robert_agent.delivery.application import publish

        self.accepted_development()
        calls, _, _, run = self.runner(pr_edit=lambda pr: pr.update(state="open"))
        options = {
            "provider": "github",
            "source_owner": "test-coordinator",
            "tapd_client_factory": self.factory,
            "tapd_observation_provider": self.provider,
        }

        def execute(**kwargs):
            return publish.publish_ready_actions(
                self.db, config=self.configured, run_command=run, **{**options, **kwargs}
            )

        self.assertEqual(execute(limit=0)["pending_count"], 0)
        self.assertEqual(execute(limit=1, repo_id="repo:wrong")["pending_count"], 0)
        self.assertEqual(execute(limit=1, action_ids=[self.action_ids["open_pr"]])["pending_count"], 0)
        self.assertEqual(calls, [])
        pushed = execute(limit=1)
        self.assertEqual((pushed["pending_count"], pushed.get("native_published_count")), (1, 1), pushed)
        self.assertEqual(self.read("SELECT COUNT(*) FROM github_publication_preparations"), [(1,)])
        opened = execute(limit=1)
        self.assertEqual((opened["pending_count"], opened["native_published_count"]), (1, 1), opened)
        bodies, tapd = self._tapd_publisher()
        done = tapd(limit=1)
        self.assertEqual((done["native_published_count"], done["native_completed_count"]), (1, 1), done)
        self.assertIn("https://github.com/org/alpha/pull/17", bodies[0])
        self.assertEqual(execute(limit=1)["pending_count"], 0)

    def test_scan_native_only_reconciles_started_github_after_restart_and_revocation(self):
        from contextlib import redirect_stdout
        import io
        import os

        from robert_agent.application import publication_cycle
        from robert_agent.delivery.application import lane, publish
        from robert_agent.runtime import scan

        self.accepted_development()
        config = {
            **self.configured,
            "ok": True,
            "db_path": self.db,
            "daemon": {"min_core_remaining": 100, "min_search_remaining": 0},
        }
        calls, remote, _, github = self.runner()
        allow_budget = True
        hide_ref = True

        def transport(command, **kwargs):
            if command[:3] == ["gh", "api", "rate_limit"]:
                self.assertIn("--hostname", command)
                self.assertEqual(command[command.index("--hostname") + 1], "github.com")
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"resources": {"core": {"remaining": 500 if allow_budget else 1}}}),
                    stderr="",
                )
            if hide_ref and remote["sha"] and "/git/matching-refs/" in " ".join(command):
                return SimpleNamespace(returncode=0, stdout="[]", stderr="")
            return github(command, **kwargs)

        original = publish.publish_ready_actions

        def with_transport(db_path, **kwargs):
            return original(db_path, run_command=transport, **kwargs)

        budget = publish.github_publication_budget

        def with_budget_transport(db_path, current, **kwargs):
            return budget(db_path, current, run_command=transport, **kwargs)

        def invoke(*flags, observe=True):
            out = io.StringIO()
            with redirect_stdout(out):
                scan.main(
                    ["--config", "test.yml", "--publish", "--native-only", *flags],
                    tapd_client_factory=self.factory,
                    tapd_observation_provider=self.provider if observe else None,
                )
            return json.loads(out.getvalue())

        def due_at(timestamp):
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute("UPDATE source_sessions SET lease_until='2000-01-01T00:00:00+00:00'")
                conn.execute(
                    "UPDATE github_actions SET metadata_json=json_set(metadata_json,'$.publish.next_retry_at',?) WHERE action_id=?",
                    (timestamp, self.action_ids["push_existing_pr"]),
                )

        with (
            patch.object(lane.validate_config, "validate_config", return_value=config),
            patch.object(publish, "publish_ready_actions", side_effect=with_transport),
            patch.object(publish, "github_publication_budget", side_effect=with_budget_transport),
            patch.object(publication_cycle.dispatch_requests, "advance") as issue,
            patch.dict(os.environ, {"GH_HOST": "unrelated.example", "GH_REPO": "unrelated/project"}),
        ):
            due_at("2000-01-01T00:00:00+00:00")
            self.assertEqual(invoke("--source-reconcile-only")["pending_count"], 0)
            self.assertEqual(calls, [])
            allow_budget = False
            blocked = invoke()
            self.assertEqual((blocked["status"], blocked["pending_count"]), ("core_rate_limit_floor", 0), blocked)
            self.assertEqual(calls, [])
            allow_budget = True
            unknown = invoke()
            self.assertEqual((unknown["pending_count"], unknown["native_unknown_count"]), (1, 1), unknown)
            self.assertEqual(len([cmd for cmd in calls if cmd[:2] == ("git", "push")]), 1)
            calls_before = list(calls)
            self.assertEqual(invoke("--source-reconcile-only", observe=False)["pending_count"], 0)
            self.assertEqual(calls, calls_before)
            self.comments.clear()
            hide_ref = False
            due_at("2000-01-01T00:00:00+00:00")
            reconciled = invoke("--source-reconcile-only", observe=False)
            self.assertEqual((reconciled["pending_count"], reconciled["native_published_count"]), (1, 1), reconciled)
            self.assertEqual(len([cmd for cmd in calls if cmd[:2] == ("git", "push")]), 1)
            self.assertEqual(self.read("SELECT COUNT(*) FROM github_publication_preparations"), [(1,)])
            issue.assert_not_called()

    def test_combined_lane_preserves_healthy_repo_progress_and_blocks_dispatch_on_other_repo_budget(self):
        from robert_agent.application import publication_cycle
        from robert_agent.delivery.application import lane, publish

        self.accepted_development()
        denied = {**self.configured["repos"][0], "full_name": "org/blocked", "github_account": "other"}
        config = {**self.configured, "ok": True, "db_path": self.db, "repos": [denied, *self.configured["repos"]]}
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) "
                "VALUES('repo:blocked','org/blocked','other','main',?,?)",
                (str(self.root), str(self.root)),
            )
            conn.execute("UPDATE source_sessions SET lease_until='2000-01-01T00:00:00+00:00'")
        calls, _, _, run = self.runner()
        actual = publish.publish_ready_actions
        seen = []

        def budget(account):
            seen.append(account)
            if account == "other":
                return {"ok": False, "status": "core_rate_limit_floor", "failure_scope": "repository"}

        with (
            patch.object(lane.validate_config, "validate_config", return_value=config),
            patch.object(publish, "github_publication_budget", return_value=budget),
            patch.object(
                publish, "publish_ready_actions", side_effect=lambda db, **kw: actual(db, run_command=run, **kw)
            ),
            patch.object(publication_cycle.dispatch_requests, "advance") as dispatch,
        ):
            result = publication_cycle.publish(
                "unused", limit=1, tapd_client_factory=self.factory, tapd_observation_provider=self.provider
            )
        self.assertEqual(seen, ["other", "bot", "bot"])
        self.assertEqual(
            (result["ok"], result["status"], result["failure_scope"], result["pending_count"]),
            (False, "core_rate_limit_floor", "repository", 1),
            result,
        )
        self.assertEqual(result["native_published_count"], 1)
        self.assertEqual(len([cmd for cmd in calls if cmd[:2] == ("git", "push")]), 1)
        dispatch.assert_not_called()

    def test_direct_publisher_keeps_effect_limit_across_restart_and_proves_standalone_owner(self):
        from contextlib import redirect_stdout
        import io

        from robert_agent.delivery.application import publish
        from robert_agent.execution import source_controller

        self.accepted_development()
        config = {**self.configured, "ok": True, "db_path": self.db}
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE source_sessions SET lease_until='2000-01-01T00:00:00+00:00'")
        _, _, _, run = self.runner()
        actual = publish.publish_ready_actions
        scopes = []

        def with_transport(db_path, **options):
            if options["provider"] == "github":
                owner, guard = options["source_owner"], options["stop"]
                self.assertEqual(owner, source_controller.standalone_owner(db_path))
                self.assertFalse(guard.is_set())
                scopes.append(owner)
            return actual(db_path, run_command=run, **options)

        def invoke(limit):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(
                    publish.main(
                        ["--db", str(self.db), "--config", "test.yml", "--limit", str(limit)],
                        tapd_client_factory=self.factory,
                        tapd_observation_provider=self.provider,
                    ),
                    0,
                )
            return json.loads(out.getvalue())

        with (
            patch.object(publish.supervision_publication, "fresh_config", return_value=config),
            patch.object(publish, "publish_ready_actions", side_effect=with_transport),
            patch.object(publish, "github_publication_budget", return_value=lambda _account: None),
        ):
            self.assertEqual(invoke(0)["pending_count"], 0)
            self.assertEqual(invoke(1)["native_published_count"], 1)
            self.assertEqual(self.read("SELECT COUNT(*) FROM github_publication_preparations"), [(1,)])
            self.assertEqual(invoke(1)["native_published_count"], 1)
        self.assertEqual(len(set(scopes)), 1)
        self.assertEqual(len(scopes), 2)
        self.assertEqual(self.read("SELECT COUNT(*) FROM github_publication_preparations"), [(2,)])
        self.assertEqual(
            self.read("SELECT COUNT(*) FROM leases WHERE resource_type='daemon' AND status='active'"), [(0,)]
        )

    def test_repository_only_cycle_refuses_native_effect_and_normal_loop_counts_it(self):
        from robert_agent.delivery.application import publish
        from robert_agent.runtime import cycle, loop

        self.accepted_development()
        self.configured["db_path"] = self.db
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE source_sessions SET lease_until='2000-01-01T00:00:00+00:00'")
        calls, _, _, run = self.runner()
        actual = publish.publish_ready_actions
        common = {
            "dry_run": False,
            "consume_observations": True,
            "max_publish_actions": 1,
            "tapd_client_factory": self.factory,
            "tapd_observation_provider": self.provider,
        }
        with (
            patch.object(cycle.validate_config, "validate_config", return_value=self.configured),
            patch.object(cycle.native_sources, "runnable", return_value=False),
            patch.object(cycle.native_sources, "scan_due", return_value=False),
            patch.object(cycle.native_sources, "model_due", return_value=False),
            patch.object(cycle.discover, "collect_account_notifications", return_value={}),
            patch.object(cycle.readiness, "has_local_work", return_value=False),
            patch.object(loop, "has_runnable_local_work", return_value=False),
            patch.object(publish, "github_publication_budget", return_value=lambda _account: None),
            patch.object(
                publish, "publish_ready_actions", side_effect=lambda db, **kw: actual(db, run_command=run, **kw)
            ),
            patch("robert_agent.work.application.source_lifecycle.reconcile_remote_source_states", return_value=0),
            patch.object(cycle.discover, "collect_live_events", return_value=[]),
            patch(
                "robert_agent.work.application.remediation_requests.collect_pr_remediation_decisions", return_value=[]
            ),
        ):
            isolated = cycle.run_once("unused", manage_sources=False, **common)
            self.assertTrue(isolated["ok"], isolated)
            self.assertEqual(calls, [])
            result = loop.run_loop("unused", max_cycles=1, **common)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["budgets"]["max_publish_actions_used"], 1)
        self.assertEqual(result["cycle_results"][0]["publish_result"]["native_published_count"], 1)
        self.assertEqual(len([cmd for cmd in calls if cmd[:2] == ("git", "push")]), 1)
