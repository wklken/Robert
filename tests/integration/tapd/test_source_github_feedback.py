"""Exercise actual accepted GitHub feedback selection and independent receipt."""

import json
import sqlite3
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from robert_agent.delivery.application.publish import publish_ready_actions
from robert_agent.foundation.values import utc_now
from robert_agent.persistence import database, transactions
from robert_agent.persistence.queries import source_results
from robert_agent.persistence.rounds.source_evidence import context_digest
from robert_agent.persistence.rounds.source_feedback import planned_actions
from robert_agent.persistence.stores import source_sessions as fence
from robert_agent.work.application import source_github_observation as discussion
from tests.support.source_github_lifecycle import SourceGitHubLifecycleFixture


class SourceGitHubFeedbackTests(SourceGitHubLifecycleFixture):
    def accepted_feedback(self, identifiers=(617,), *, handle_github=True):
        round_id, raw = self.prepare_mixed_model(identifiers, handle_github=handle_github)
        step = self.tick("publication_pending", github_discussion_runner=raw)
        actions = dict(
            self.read(
                "SELECT a.provider,a.action_id FROM source_required_actions r JOIN github_actions a USING(action_id) "
                "WHERE r.source_result_id=?",
                (step.identity,),
            )
        )
        actual_reader = discussion.DiscussionReader
        with patch.object(
            discussion, "DiscussionReader", side_effect=lambda **kw: actual_reader(runner=raw, stop=kw["stop"])
        ):
            _, publish_tapd = self._tapd_publisher()
            result = publish_tapd(action_ids=(actions["tapd"],))
        self.assertEqual(result["native_published_count"], 1, result)
        return round_id, step.identity, actions.get("github"), raw

    def remote(self, *, publisher="bot", creator="bot", mode="confirmed", parent_edit=None):
        def observe_parent(item):
            item["base"]["repo"]["id"] = 1729
            if parent_edit:
                parent_edit(item)

        _, parent = self._current_github_runner(parent_edit=observe_parent)
        calls, posts, remote = [], [], {}
        endpoint = "repos/org/alpha/issues/17/comments"
        repository = "Org/Alpha" if creator == "Bot" else "org/alpha"
        url = f"https://github.com/{repository}/pull/17#issuecomment-900"

        def run(command, **kwargs):
            path, method = command[7], command[6]
            self.assertEqual(command[:5], ["gh", "api", "--hostname", "github.com", "--include"])
            self.assertEqual(kwargs["env"]["GH_HOST"], "github.com")
            self.assertNotIn("GH_REPO", kwargs["env"])
            calls.append((method, path))
            if "/pulls/" in path:
                return parent(command, **kwargs)
            status, headers = 200, ""
            if path == "user":
                payload = {"login": publisher}
            elif path == endpoint and method == "POST":
                posts.append(json.loads(kwargs["input"])["body"])
                moment = utc_now().split(".")[0] + "Z"
                remote.update(
                    id=900,
                    node_id="feedback-node",
                    html_url=url,
                    issue_url=f"https://api.github.com/repos/{repository}/issues/17",
                    user={"login": creator, "type": "User"},
                    body=posts[-1],
                    created_at=moment,
                    updated_at=moment,
                )
                if state["mode"] == "timeout":
                    raise TimeoutError("ack lost")
                status, payload = 201, dict(remote)
            elif path == endpoint + "?per_page=100&page=2":
                self.assertEqual(state["mode"], "paginated")
                payload = [dict(remote)]
            elif path == endpoint + "?per_page=100&page=1":
                payload = [dict(remote)] if remote else []
                if state["mode"] == "timeout":
                    status, payload = 503, {}
                if state["mode"] == "paginated":
                    payload = [{"id": 899, "body": "Unrelated remote text"}]
                    next_url = "https://api.github.com/repositories/1729/issues/17/comments?page=2&per_page=100"
                    headers = f'Link: <{next_url}>; rel="next", <{next_url}>; rel="last"\r\n'
                if state["mode"] == "wrong_creator":
                    payload[0]["user"] = {"login": "unrelated", "type": "User"}
                if state["mode"] == "duplicate":
                    payload.append({**remote, "id": 901})
                if state["mode"] == "incomplete":
                    headers = f'Link: <https://api.github.com/{endpoint}?per_page=100&page=2>; rel="last"\r\n'
            elif path == "repos/org/alpha/issues/comments/900":
                payload = dict(remote)
                if state["mode"] == "wrong_url":
                    payload["html_url"] = url + "1"
            elif path == "graphql":
                payload = {
                    "data": {
                        "node": {
                            "id": "feedback-node",
                            "body": remote["body"],
                            "author": {"login": creator},
                            "editor": None,
                            "lastEditedAt": remote["created_at"] if state["mode"] == "edited" else None,
                            "updatedAt": remote["updated_at"],
                        }
                    }
                }
            else:
                self.fail((method, path))
            out = f"HTTP/2.0 {status} Result\r\n{headers}\r\n" + json.dumps(payload)
            return SimpleNamespace(stdout=out.encode(), stderr=b"", returncode=int(status >= 400))

        state = {"mode": mode}
        return calls, posts, state, run

    def publish_feedback(self, action_id, raw, runner, **options):
        defaults = dict(
            provider="github",
            source_owner="test-coordinator",
            action_ids=(action_id,),
            tapd_client_factory=self.factory,
            tapd_observation_provider=self.provider,
            github_feedback_runner=runner,
            github_discussion_runner=raw,
            run_command=lambda *a, **kw: self.fail("generic transport"),
        )
        return publish_ready_actions(self.db, config=self.configured, **{**defaults, **options})

    def test_public_scheduler_publishes_required_feedback_and_proves_independent_receipt(self):
        round_id, root_id, action_id, raw = self.accepted_feedback((617, 618))
        calls, posts, _, runner = self.remote()
        result = self.publish_feedback(action_id, raw, runner, repo_id="repo:org/alpha")
        self.assertEqual(result["native_published_count"], 1, result)
        with closing(database.connect(self.db)) as conn:
            target = source_results.source_target(source_results.tapd_source(conn, self.ticket.source_id))
            confirmed = source_results.confirmed_required_effects(conn, root_id, target)
            self.assertEqual([row["provider"] for row in confirmed], ["tapd", "github"])
        self.assertEqual(self.read("SELECT status FROM source_rounds WHERE round_id=?", (round_id,)), [("active",)])
        self.assertEqual(len(posts), 1)
        self.assertEqual(len([path for method, path in calls if method == "POST" and path != "graphql"]), 1)
        self.assertEqual(
            calls[-3:],
            [
                ("GET", "repos/org/alpha/issues/17/comments?per_page=100&page=1"),
                ("GET", "repos/org/alpha/issues/comments/900"),
                ("POST", "graphql"),
            ],
        )
        self.assertIn("No code changes were made.", posts[0])
        self.assertNotIn("ZX991", posts[0])
        self.assertNotIn("tapd", posts[0].lower())
        self.assertEqual(
            self.read("SELECT task_id,result_id,source_result_id FROM github_actions WHERE action_id=?", (action_id,)),
            [(None, None, root_id)],
        )

    def test_code_feedback_is_rendered_from_audited_code_and_checks(self):
        self.accepted_mixed_development()
        _, before_push = self._current_github_runner(
            parent_edit=lambda parent: parent["head"].update(sha=self.initial_pr_head)
        )
        _, _, _, github = self.runner()
        outcome = self.publish("push_existing_pr", github, github_discussion_runner=before_push)
        self.assertEqual(outcome.status, "published", outcome)
        _, raw = self._current_github_runner()
        _, tapd = self._tapd_publisher()
        actual_reader = discussion.DiscussionReader
        with patch.object(
            discussion, "DiscussionReader", side_effect=lambda **kw: actual_reader(runner=raw, stop=kw["stop"])
        ):
            published = tapd(action_ids=(self.action_ids["comment"],))
        self.assertEqual(published["native_published_count"], 1, published)
        from robert_agent.delivery.application.source_feedback_publication import publish_feedback_action

        feedback_id = self.action_ids["source_feedback"]
        [[worker, original_evidence]] = self.read(
            "SELECT worker_result_id,evidence_json FROM source_results WHERE source_result_id=?",
            (self.root_id,),
        )
        [[original_audit]] = self.read("SELECT metadata_json FROM worker_results WHERE result_id=?", (worker,))
        [[original_recipe]] = self.read("SELECT metadata_json FROM github_actions WHERE action_id=?", (feedback_id,))
        audit, evidence = json.loads(original_audit), json.loads(original_evidence)
        projection = audit["audit"]["action_scope"]["native_github"]
        projection["body"] += "\nFORGED PRIVATE BODY"
        evidence["github_feedback_code_sha256"] = context_digest(projection)
        (recipe,) = planned_actions(
            self.root_id,
            evidence["github_return_targets"],
            evidence["github_feedback_code_sha256"],
        )
        self.assertEqual(recipe.action_id, feedback_id)

        def update(audit_bytes, evidence_bytes, recipe_bytes):
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute("UPDATE worker_results SET metadata_json=? WHERE result_id=?", (audit_bytes, worker))
                conn.execute(
                    "UPDATE source_results SET evidence_json=? WHERE source_result_id=?", (evidence_bytes, self.root_id)
                )
                conn.execute("UPDATE github_actions SET metadata_json=? WHERE action_id=?", (recipe_bytes, feedback_id))

        update(json.dumps(audit), json.dumps(evidence), json.dumps(recipe.metadata))
        _, forged_posts, _, forged_transport = self.remote()
        held = publish_feedback_action(
            self.db,
            self.configured,
            source_result_id=self.root_id,
            action_id=feedback_id,
            owner="test-coordinator",
            observation_provider=self.provider,
            tapd_client_factory=self.factory,
            github_discussion_runner=raw,
            runner=forged_transport,
        )
        self.assertEqual(held.status, "deferred", held)
        self.assertEqual(forged_posts, [])
        update(original_audit, original_evidence, original_recipe)
        logs, posts, _, runner = self.remote(publisher="Bot", creator="Bot")
        with patch.dict("os.environ", {"GH_HOST": "example.invalid", "GH_REPO": "wrong/repo"}):
            result = self.publish_feedback(self.action_ids["source_feedback"], raw, runner)
        self.assertEqual(result["native_published_count"], 1, result)
        self.assertEqual(len(posts), 1)
        self.assertIn("README.md", posts[0])
        self.assertIn("Worker-reported", posts[0])
        self.assertNotIn("ZX991", posts[0])
        self.assertNotIn("TOP-SECRET-771", posts[0])
        self.assertNotIn("https://", posts[0])
        self.assertEqual(logs[0], ("GET", "user"))

        attempted = self.read("SELECT attempt_id FROM attempts ORDER BY attempt_id")
        ledger = self.read("SELECT * FROM request_events ORDER BY row_id")
        _, edited = self._current_github_runner(body="@bot please inspect clarified details")
        closed = self.cold_completion(edited, repo_id="repo:org/alpha")
        self.assertEqual(closed["native_completed_count"], 1, closed)
        [[round_id]] = self.read("SELECT round_id FROM source_results WHERE source_result_id=?", (self.root_id,))
        self.assertEqual(
            self.read("SELECT status FROM source_rounds WHERE round_id=?", (round_id,)), [("execution_finished",)]
        )
        self.assertEqual(
            self.read(
                "SELECT w.lifecycle, i.completed_at IS NOT NULL FROM work_items i JOIN workstreams w USING(workstream_id) "
                "WHERE i.origin_source_id=? AND i.repo_id='repo:org/alpha'",
                (self.ticket.source_id,),
            ),
            [("completed", 1)],
        )
        self.assertEqual(self.read("SELECT attempt_id FROM attempts ORDER BY attempt_id"), attempted)
        self.assertEqual(self.read("SELECT * FROM request_events ORDER BY row_id"), ledger)

    def test_timeout_reconciles_original_packet_after_mention_withdrawal_and_resolves_attention(self):
        from robert_agent.delivery.application.source_feedback_publication import publish_feedback_action
        from robert_agent.persistence.queries import source_attention, source_feedback

        _round_id, root_id, action_id, raw = self.accepted_feedback()
        calls, posts, state, runner = self.remote(mode="timeout")
        first = self.publish_feedback(action_id, raw, runner)
        self.assertEqual(first["native_unknown_count"], 1, first)
        with closing(database.connect(self.db)) as conn:
            action = source_feedback.required_action(conn, root_id, action_id)
            self.assertEqual(action["intent_status"], "attention")
            self.assertTrue(source_results.unknown_github_effect(conn, self.ticket.source_id))
            episode = source_attention.publication_episode(conn, action["publication_id"])
            self.assertIsNotNone(episode)
            self.assertIsNone(episode["resolved_at"])
            self.assertIsNone(source_feedback.confirmed_receipt(conn, root_id, action_id))
        state["mode"] = "paginated"
        _, withdrawn = self._current_github_runner(body="Please ignore the earlier request")
        new_calls = len(calls)
        removed_project = {**self.configured, "tapd": {**self.configured["tapd"], "projects": []}}
        restored = publish_feedback_action(
            self.db,
            removed_project,
            source_result_id=root_id,
            action_id=action_id,
            owner="test-coordinator",
            runner=runner,
            observation_provider=lambda *a, **kw: self.fail("TAPD write authorization queried"),
            github_discussion_runner=withdrawn,
            reconcile_only=True,
        )
        self.assertEqual(restored.status, "published", restored)
        self.assertEqual(len(posts), 1)
        self.assertNotIn(("GET", "user"), calls[new_calls:])
        self.assertIn(("GET", "repos/org/alpha/issues/17/comments?per_page=100&page=2"), calls[new_calls:])
        with closing(database.connect(self.db)) as conn:
            self.assertFalse(
                source_results.unknown_github_effect(conn, self.ticket.source_id, require_positive_receipt=True)
            )
            self.assertIsNotNone(source_attention.publication_episode(conn, action["publication_id"])["resolved_at"])
        self.assertEqual(
            self.read("SELECT DISTINCT state FROM request_events WHERE source_result_id=?", (root_id,)), [("consumed",)]
        )

    def test_current_authority_account_parent_and_repository_scope_refuse_before_post(self):
        from robert_agent.delivery.application.source_feedback_publication import publish_feedback_action
        from robert_agent.delivery.application.source_github_publication import publish_github_action

        round_id, root_id, action_id, raw = self.accepted_feedback()
        calls, posts, _, wrong_account = self.remote(publisher="somebody-else")
        result = self.publish_feedback(action_id, raw, wrong_account, repo_id="repo:elsewhere", limit=1)
        self.assertEqual(result["pending_count"], 0, result)
        self.assertEqual(calls, [])
        self.assertEqual(
            publish_github_action(
                self.db,
                self.configured,
                source_result_id=root_id,
                action_id=action_id,
                owner="test-coordinator",
                run_command=lambda *a, **k: self.fail("generic code transport"),
            ).status,
            "invalid",
        )

        def invoke(transport, observation=raw, **options):
            return publish_feedback_action(
                self.db,
                self.configured,
                source_result_id=root_id,
                action_id=action_id,
                owner="test-coordinator",
                observation_provider=self.provider,
                tapd_client_factory=self.factory,
                runner=transport,
                github_discussion_runner=observation,
                **options,
            )

        self.assertEqual(invoke(wrong_account, reconcile_only=True).reason, "reconciliation_only")
        self.assertEqual(invoke(wrong_account).reason, "github_feedback_target_unavailable")
        self.assertEqual(posts, [])
        _, invalid_posts, _, wrong_parent = self.remote(parent_edit=lambda parent: parent.update(number=18))
        self.assertEqual(invoke(wrong_parent).reason, "github_feedback_target_unavailable")
        self.assertEqual(invalid_posts, [])
        budgets = []
        _, denied_posts, _, budget_transport = self.remote()

        def budget(account):
            budgets.append(account)
            return {"ok": False, "status": "core_rate_limit_floor", "failure_scope": "repository"}

        refused = self.publish_feedback(action_id, raw, budget_transport, github_budget_check=budget)
        self.assertEqual(refused["status"], "core_rate_limit_floor", refused)
        self.assertEqual((budgets, denied_posts), (["bot"], []))
        _, cancelled_posts, _, good_transport = self.remote()
        _, withdrawn = self._current_github_runner(body="Please ignore the earlier request")
        self.assertEqual(invoke(good_transport, withdrawn).reason, "current_source_grant_unavailable")
        self.assertEqual(cancelled_posts, [])
        self.assertEqual(
            self.read("SELECT COUNT(*) FROM source_round_cancellations WHERE round_id=?", (round_id,)), [(1,)]
        )
        self.assertEqual(self.read("SELECT COUNT(*) FROM publication_intents WHERE action_id=?", (action_id,)), [(0,)])

    def test_incomplete_listing_and_invalid_independent_items_remain_unknown(self):
        from robert_agent.delivery.application.source_feedback_publication import publish_feedback_action
        from robert_agent.persistence.queries.source_feedback import confirmed_receipt

        _, root_id, action_id, raw = self.accepted_feedback()
        calls, posts, remote, runner = self.remote(mode="incomplete")
        for mode in ("incomplete", "duplicate", "wrong_url", "edited", "wrong_creator"):
            with self.subTest(mode=mode):
                remote["mode"] = mode
                result = publish_feedback_action(
                    self.db,
                    self.configured,
                    source_result_id=root_id,
                    action_id=action_id,
                    owner="test-coordinator",
                    observation_provider=self.provider,
                    tapd_client_factory=self.factory,
                    runner=runner,
                    github_discussion_runner=raw,
                )
                self.assertEqual(result.status, "unknown", result)
                self.assertEqual(len(posts), 1)
                with closing(database.connect(self.db)) as conn:
                    self.assertIsNone(confirmed_receipt(conn, root_id, action_id))
        self.assertEqual(len([path for method, path in calls if method == "POST" and path != "graphql"]), 1)

    def test_fence_lost_after_remote_authentication_can_never_prepare_feedback(self):
        from robert_agent.delivery.application.source_feedback_publication import publish_feedback_action

        _, root_id, action_id, raw = self.accepted_feedback()
        _, posts, _, runner = self.remote()

        def move_fence(command, **options):
            if command[7] == "user":
                with closing(sqlite3.connect(self.db)) as conn, conn:
                    conn.execute(
                        "UPDATE source_sessions SET lease_until='2026-01-01T00:00:00+00:00' WHERE source_id=?",
                        (self.ticket.source_id,),
                    )
                with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
                    transactions.begin_immediate(conn)
                    self.assertIsNotNone(
                        fence.acquire_for_owner(
                            conn,
                            source_id=self.ticket.source_id,
                            owner="new-owner",
                            now=utc_now(),
                            lease_seconds=300,
                        )
                    )
            return runner(command, **options)

        result = publish_feedback_action(
            self.db,
            self.configured,
            source_result_id=root_id,
            action_id=action_id,
            owner="test-coordinator",
            observation_provider=self.provider,
            tapd_client_factory=self.factory,
            runner=move_fence,
            github_discussion_runner=raw,
        )
        self.assertEqual(result.reason, "current_source_grant_unavailable")
        self.assertEqual(posts, [])
        self.assertEqual(self.read("SELECT COUNT(*) FROM publication_intents WHERE action_id=?", (action_id,)), [(0,)])

    def cold_completion(self, raw, **options):
        defaults = {
            "provider": "tapd",
            "action_ids": None,
            "limit": 0,
            "repo_id": "repo:org/alpha",
        }
        return self.publish_feedback(
            None,
            raw,
            lambda *a, **kw: self.fail("cold local completion called the GitHub feedback transport"),
            **{**defaults, **options},
        )

    def allow_completion_retry(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE tapd_coordination SET next_due_at='2026-01-01T00:00:00+00:00' WHERE source_id=?",
                (self.ticket.source_id,),
            )

    def test_cold_publication_pass_completes_both_receipts_and_exposes_the_question(self):
        from robert_agent.work.application import messaging

        round_id, root_id, action_id, raw = self.accepted_feedback()
        identity = {
            "protocol": "robert.message.v1",
            "provider": "openclaw",
            "channel": "telegram",
            "conversation": "telegram:42",
            "thread": "",
            "account": "test-account",
            "sender_id": "42",
        }
        self.configured.update(db_path=str(self.db))
        self.configured["messaging"] = {
            **identity,
            "enabled": True,
            "allowed_senders": ["42"],
            "send_after": "2026-07-01T00:00:00Z",
            "target": "42",
            "repos": ["org/alpha"],
            "tapd_projects": [{"host": self.ticket.host, "workspace_id": self.ticket.workspace_id}],
        }
        self.configured["tapd"]["projects"][0]["operators"] = ["openclaw:telegram:test-account:42"]
        self.assertEqual(messaging.inbox(self.configured, identity)["messages"], [])
        before_feedback = self.cold_completion(lambda *a, **kw: self.fail("missing receipt should not need authority"))
        self.assertEqual(before_feedback["native_completed_count"], 0, before_feedback)
        initial_ledger = self.read("SELECT * FROM request_events ORDER BY row_id")
        _, posts, _, runner = self.remote()
        written = self.publish_feedback(action_id, raw, runner, recover_sources=False)
        self.assertEqual(written["native_published_count"], 1, written)
        self.assertEqual(messaging.inbox(self.configured, identity)["messages"], [])

        def never_read(*_args, **_options):
            self.fail("local-only completion opened a provider read")

        for limit_options in (
            {"tapd_observation_provider": None},
            {"tapd_observation_provider": never_read, "source_reconcile_only": True},
        ):
            refused = self.cold_completion(never_read, **limit_options)
            self.assertEqual(refused["native_completed_count"], 0, refused)
        readback = self.cold_completion(raw)
        self.assertEqual(readback["native_completed_count"], 1, readback)
        self.assertEqual(
            self.read("SELECT status FROM source_rounds WHERE round_id=?", (round_id,)), [("waiting_for_user",)]
        )
        [question] = messaging.inbox(self.configured, identity)["messages"]
        self.assertEqual(question["target"]["source_result_id"], root_id)
        self.assertEqual(question["summary"], "Which component needs more detail? Private canary ZX991.")
        self.assertEqual(len(posts), 1)
        self.assertEqual(self.read("SELECT * FROM request_events ORDER BY row_id"), initial_ledger)

        wrong_project = deepcopy(self.configured)
        wrong_project["messaging"]["tapd_projects"][0]["workspace_id"] = "999"
        self.assertEqual(messaging.inbox(wrong_project, identity)["messages"], [])
        repeated = self.cold_completion(never_read)
        self.assertEqual(repeated["native_completed_count"], 0, repeated)
        self.assertEqual(self.read("SELECT COUNT(*) FROM source_question_attentions"), [(1,)])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE publication_intents SET remote_evidence_json=json_remove(remote_evidence_json,'$.proof.creator') "
                "WHERE action_id=?",
                (action_id,),
            )
        self.assertEqual(messaging.inbox(self.configured, identity)["messages"], [])
        from robert_agent.execution.notifications import has_pending_local_delivery

        with closing(database.connect(self.db)) as conn:
            self.assertFalse(has_pending_local_delivery(conn, ["repo:org/alpha"], utc_now(), self.configured))

    def test_handled_tapd_subset_still_checks_original_github_and_leaves_it_pending(self):
        round_id, root_id, feedback, raw = self.accepted_feedback(handle_github=False)
        self.assertIsNone(feedback)
        initial = self.read("SELECT * FROM request_events ORDER BY row_id")
        _, unavailable = self._current_github_runner(item_status=503)
        blocked = self.cold_completion(unavailable)
        self.assertEqual(blocked["native_completion_held_count"], 1, blocked)
        self.assertEqual(self.read("SELECT status FROM source_rounds WHERE round_id=?", (round_id,)), [("active",)])
        self.assertEqual(self.read("SELECT * FROM request_events ORDER BY row_id"), initial)
        self.allow_completion_retry()
        finished = self.cold_completion(raw)
        self.assertEqual(finished["native_completed_count"], 1, finished)
        self.assertEqual(self.read("SELECT * FROM request_events ORDER BY row_id"), initial)
        self.assertEqual(
            self.read(
                "SELECT DISTINCT l.state FROM request_events l JOIN ingress_requests r USING(request_id) "
                "WHERE r.provider='github'",
            ),
            [("pending",)],
        )
        subsequent = self.tick("admitted", github_discussion_runner=raw, ticket=self.ticket)
        self.assertNotEqual(subsequent.round_id, round_id)
        self.assertEqual(
            self.read(
                "SELECT COUNT(*) FROM source_round_requests rr JOIN ingress_requests r USING(request_id) "
                "WHERE rr.round_id=? AND r.provider='github'",
                (subsequent.round_id,),
            ),
            [(1,)],
        )
        self.assertIsNotNone(root_id)

    def test_known_withdrawal_beats_unavailable_sibling_without_rewriting_the_ledger(self):
        round_id, _, action_id, raw = self.accepted_feedback((617, 618))
        _, posts, _, runner = self.remote()
        self.assertEqual(self.publish_feedback(action_id, raw, runner)["native_published_count"], 1)
        initial = self.read("SELECT * FROM request_events ORDER BY row_id")
        readers = {
            617: self._current_github_runner(identifier=617, body="Removed marker")[1],
            618: self._current_github_runner(identifier=618, item_status=503)[1],
        }
        active = 617

        def observed(command, **state):
            nonlocal active
            if "/issues/comments/" in command[7]:
                active = int(command[7].rsplit("/", 1)[1])
            return readers[active](command, **state)

        result = self.cold_completion(observed)
        self.assertEqual(result["native_completion_held_count"], 1, result)
        self.assertEqual(
            self.read(
                "SELECT reason FROM source_round_cancellations WHERE round_id=?",
                (round_id,),
            ),
            [("github_current_mention_removed",)],
        )
        self.assertEqual(len(posts), 1)
        self.assertEqual(self.read("SELECT COUNT(*) FROM source_question_attentions"), [(0,)])
        self.assertEqual(self.read("SELECT * FROM request_events ORDER BY row_id"), initial)

    def test_completion_proof_refuses_stale_operation_claim_and_receipt_vector(self):
        from robert_agent.delivery.application import source_delivery_observation

        round_id, _, action_id, raw = self.accepted_feedback()
        _, posts, _, runner = self.remote()
        self.assertEqual(self.publish_feedback(action_id, raw, runner)["native_published_count"], 1)
        initial = self.read("SELECT * FROM request_events ORDER BY row_id")
        [[receipt]] = self.read("SELECT remote_evidence_json FROM publication_intents WHERE action_id=?", (action_id,))
        actual_observe = source_delivery_observation.observe_completion

        def persist_receipt(value):
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute(
                    "UPDATE publication_intents SET remote_evidence_json=? WHERE action_id=?", (value, action_id)
                )

        def change_vector():
            changed = json.loads(receipt)
            # Preserve canonical receipt validity but alter the exact observation.
            [[when]] = self.read("SELECT finished_at FROM publication_intents WHERE action_id=?", (action_id,))
            changed["observed_at"] = when
            self.assertNotEqual(json.loads(receipt), changed)
            persist_receipt(json.dumps(changed))

        def change_during_tapd_read(*args, **options):
            change_vector()
            return self.provider(*args, **options)

        held = self.cold_completion(
            lambda *a, **kw: self.fail("the receipt vector changed during the first provider read"),
            tapd_observation_provider=change_during_tapd_read,
        )
        self.assertEqual(held["native_completion_held_count"], 1, held)
        persist_receipt(receipt)
        self.allow_completion_retry()

        for attack in ("operation", "vector", "claim"):
            with self.subTest(attack=attack):

                def displaced(db, config, ticket, claim, **kwargs):
                    first = actual_observe(db, config, ticket, claim, **kwargs)
                    self.assertIsNotNone(first)
                    if attack == "operation":
                        second = actual_observe(db, config, ticket, claim, **kwargs)
                        self.assertIsNotNone(second)
                        self.assertNotEqual(first.github.operations, second.github.operations)
                    elif attack == "vector":
                        change_vector()
                    else:
                        with closing(sqlite3.connect(self.db)) as conn, conn:
                            conn.execute(
                                "UPDATE source_sessions SET lease_until='2026-01-01T00:00:00+00:00' WHERE source_id=?",
                                (self.ticket.source_id,),
                            )
                        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
                            transactions.begin_immediate(conn)
                            self.assertIsNotNone(
                                fence.acquire_for_owner(
                                    conn,
                                    source_id=self.ticket.source_id,
                                    owner="next-owner",
                                    now=utc_now(),
                                    lease_seconds=300,
                                )
                            )
                    return first

                with patch.object(source_delivery_observation, "observe_completion", side_effect=displaced):
                    denied = self.cold_completion(raw)
                self.assertEqual(denied["native_completion_held_count"], 1, denied)
                self.assertEqual(
                    self.read(
                        "SELECT status FROM source_rounds WHERE round_id=?",
                        (round_id,),
                    ),
                    [("active",)],
                )
                self.assertEqual(self.read("SELECT COUNT(*) FROM source_question_attentions"), [(0,)])
                self.assertEqual(self.read("SELECT * FROM request_events ORDER BY row_id"), initial)
                if attack == "vector":
                    persist_receipt(receipt)
                self.allow_completion_retry()
        finished = self.cold_completion(raw, source_owner="next-owner")
        self.assertEqual(finished["native_completed_count"], 1, finished)
        self.assertEqual(len(posts), 1)

    def test_coordinator_retires_unstarted_feedback_after_original_mention_withdrawal(self):
        round_id, root_id, action_id, _ = self.accepted_feedback()
        _, revoked = self._current_github_runner(body="I withdraw the request")
        ledger = self.read("SELECT * FROM request_events ORDER BY row_id")
        self.allow_completion_retry()
        self.tick("draining", github_discussion_runner=revoked)
        retired = self.tick("canceled", ticket=self.ticket, github_discussion_runner=revoked)
        self.assertEqual(retired.reason, "accepted_delivery_retired")
        self.assertEqual(retired.identity, root_id)
        self.assertEqual(self.read("SELECT status FROM source_rounds WHERE round_id=?", (round_id,)), [("canceled",)])
        self.assertEqual(
            self.read("SELECT publish_status FROM github_actions WHERE action_id=?", (action_id,)), [("skipped",)]
        )
        self.assertEqual(self.read("SELECT * FROM request_events ORDER BY row_id"), ledger)

    def test_partial_mixed_development_pauses_and_later_terminal_retires_only_the_original_projection(self):
        from robert_agent.persistence.queries import source_delivery_pauses, source_retirement

        admitted, _, _ = self.accepted_mixed_development()
        root_id = self.root_id
        _, prior = self._current_github_runner(
            parent_edit=lambda parent: parent["head"].update(sha=self.initial_pr_head)
        )
        _, _, _, github = self.runner()
        pushed = self.publish("push_existing_pr", github, github_discussion_runner=prior)
        self.assertEqual(pushed.status, "published", pushed)
        code = self.read("SELECT * FROM publication_intents WHERE action_id=?", (self.action_ids["push_existing_pr"],))
        ledger = self.read("SELECT * FROM request_events ORDER BY row_id")
        feedback = self.action_ids["source_feedback"]
        tapd = self.action_ids["comment"]
        self.labels = ()
        _, raw = self._current_github_runner()
        _, unavailable = self._current_github_runner(item_status=503)
        self.tick("held", ticket=self.ticket, github_discussion_runner=unavailable)
        with closing(database.connect(self.db)) as conn:
            self.assertIsNone(source_delivery_pauses.latest(conn, root_id))
        self.tick("canceled", ticket=self.ticket, github_discussion_runner=raw)
        with closing(database.connect(self.db)) as conn:
            pause = source_delivery_pauses.latest(conn, root_id)
            self.assertEqual(source_delivery_pauses.dormant_actions(conn, pause), (tapd, feedback))
        self.assertEqual(self.read("SELECT * FROM request_events ORDER BY row_id"), ledger)

        follow = self.comment("94")
        at = replace(follow.version, created=utc_now())
        self.comments.insert(
            0,
            replace(
                follow,
                version=at,
                creator=replace(follow.creator, observation=at),
                own_body=replace(follow.own_body, observation=at),
            ),
        )
        self.model_kind = "question"
        newer = self.tick("admitted", ticket=self.ticket, github_discussion_runner=raw)
        self.assertNotEqual(newer.round_id, admitted.round_id)
        self.tick("prepared", github_discussion_runner=raw)
        self.tick("dispatched", github_discussion_runner=raw)
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
        self.tick("waiting", github_discussion_runner=raw)
        self.tick("audited", github_discussion_runner=raw)
        self.tick("stage_recorded", github_discussion_runner=raw)
        question = self.tick("publication_pending", github_discussion_runner=raw)
        [[question_action]] = self.read(
            "SELECT action_id FROM source_required_actions WHERE source_result_id=?", (question.identity,)
        )
        _, tapd_publish = self._tapd_publisher()
        self.assertEqual(tapd_publish(action_ids=(question_action,))["native_completed_count"], 1)
        new_projection = self.read("SELECT * FROM work_items WHERE repo_id='repo:org/alpha'")
        _, withdrawn = self._current_github_runner(body="I withdraw the original discussion")
        actual_observe = discussion.observe_disposition

        def advance_pause_generation(*args, **options):
            proof = actual_observe(*args, **options)
            self.assertIsNotNone(proof)
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute(
                    "UPDATE source_delivery_pauses SET generation=generation+1 WHERE source_result_id=?",
                    (root_id,),
                )
            return proof

        with patch.object(discussion, "observe_disposition", side_effect=advance_pause_generation):
            stale = self.tick("held", ticket=self.ticket, github_discussion_runner=withdrawn)
        self.assertEqual(stale.reason, "accepted_disposition_scope_changed")
        self.assertEqual(self.read("SELECT COUNT(*) FROM source_result_retirements"), [(0,)])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE source_delivery_pauses SET generation=generation-1 WHERE source_result_id=?",
                (root_id,),
            )
        self.terminal = True
        retired = self.tick("canceled", ticket=self.ticket, github_discussion_runner=raw)
        self.assertEqual((retired.reason, retired.identity), ("accepted_delivery_retired", root_id))
        self.assertEqual(self.read("SELECT * FROM work_items WHERE repo_id='repo:org/alpha'"), new_projection)
        self.assertEqual(
            self.read("SELECT * FROM publication_intents WHERE action_id=?", (self.action_ids["push_existing_pr"],)),
            code,
        )
        self.assertEqual(
            self.read("SELECT publish_status FROM github_actions WHERE action_id IN (?,?)", (tapd, feedback)),
            [("skipped",), ("skipped",)],
        )
        with closing(database.connect(self.db)) as conn:
            self.assertTrue(source_retirement.is_retired(conn, root_id))

    def test_unknown_feedback_blocks_retirement_until_original_independent_readback(self):
        from robert_agent.delivery.application.source_feedback_publication import publish_feedback_action
        from robert_agent.persistence.queries import source_retirement

        round_id, root_id, action_id, raw = self.accepted_feedback()
        _, posts, remote, runner = self.remote(mode="timeout")
        result = self.publish_feedback(action_id, raw, runner)
        self.assertEqual(result["native_unknown_count"], 1, result)
        _, revoked = self._current_github_runner(body="I withdraw this request")
        self.tick("draining", ticket=self.ticket, github_discussion_runner=revoked)
        self.tick("held", ticket=self.ticket, github_discussion_runner=revoked)
        self.assertEqual(self.read("SELECT status FROM source_rounds WHERE round_id=?", (round_id,)), [("active",)])
        self.assertEqual(self.read("SELECT COUNT(*) FROM source_result_retirements"), [(0,)])
        remote["mode"] = "confirmed"
        settled = publish_feedback_action(
            self.db,
            self.configured,
            source_result_id=root_id,
            action_id=action_id,
            owner="test-coordinator",
            runner=runner,
            reconcile_only=True,
        )
        self.assertEqual(settled.status, "published", settled)
        retired = self.tick("canceled", ticket=self.ticket, github_discussion_runner=revoked)
        self.assertEqual(retired.identity, root_id)
        self.assertEqual(len(posts), 1)
        self.assertEqual(
            self.read("SELECT publish_status FROM github_actions WHERE action_id=?", (action_id,)), [("published",)]
        )
        with closing(database.connect(self.db)) as conn:
            self.assertTrue(source_retirement.is_retired(conn, root_id))

    def test_unhandled_original_withdrawal_preserves_consumed_ledger_with_unavailable_sibling(self):
        round_id, root_id, _, _ = self.accepted_feedback((617, 618), handle_github=False)
        handled = self.read("SELECT * FROM request_events WHERE source_result_id=?", (root_id,))
        readers = {
            617: self._current_github_runner(identifier=617, body="Removed marker")[1],
            618: self._current_github_runner(identifier=618, item_status=503)[1],
        }
        key = 617

        def observed(command, **state):
            nonlocal key
            if "/issues/comments/" in command[7]:
                key = int(command[7].rsplit("/", 1)[1])
            return readers[key](command, **state)

        self.tick("draining", ticket=self.ticket, github_discussion_runner=observed)
        retired = self.tick("canceled", ticket=self.ticket, github_discussion_runner=observed)
        self.assertEqual((retired.round_id, retired.identity), (round_id, root_id))
        self.assertEqual(self.read("SELECT * FROM request_events WHERE source_result_id=?", (root_id,)), handled)
        self.assertEqual(
            self.read(
                "SELECT e.event_id,r.phase,e.state FROM request_events e JOIN ingress_requests r USING(request_id) "
                "WHERE r.provider='github' ORDER BY e.event_id",
            ),
            [("event:comment:617", "revoked", "revoked"), ("event:comment:618", "pending", "pending")],
        )

    def test_disposition_after_its_source_fence_moves_cannot_cancel_or_retire(self):
        round_id, root_id, _, _ = self.accepted_feedback()
        _, revoked = self._current_github_runner(body="I withdraw this request")
        probe = discussion.observe_disposition

        def move_fence(*args, **options):
            observed = probe(*args, **options)
            self.assertIsNotNone(observed)
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute(
                    "UPDATE source_sessions SET lease_until='2026-01-01T00:00:00+00:00' WHERE source_id=?",
                    (self.ticket.source_id,),
                )
            with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
                transactions.begin_immediate(conn)
                self.assertIsNotNone(
                    fence.acquire_for_owner(
                        conn,
                        source_id=self.ticket.source_id,
                        owner="new-owner",
                        now=utc_now(),
                        lease_seconds=300,
                    )
                )
            return observed

        with patch.object(discussion, "observe_disposition", side_effect=move_fence):
            self.tick("held", ticket=self.ticket, github_discussion_runner=revoked)
        self.assertEqual(
            self.read("SELECT COUNT(*) FROM source_round_cancellations WHERE round_id=?", (round_id,)), [(0,)]
        )
        self.assertEqual(
            self.read("SELECT COUNT(*) FROM source_result_retirements WHERE source_result_id=?", (root_id,)), [(0,)]
        )

    def mixed_recovery_offer(self, *, settle_tapd=False, handle_github=True, settle_push=True):
        from robert_agent.work import tapd_authority

        admitted, _, _ = self.accepted_mixed_development(handle_github=handle_github)
        _, prior = self._current_github_runner(
            parent_edit=lambda parent: parent["head"].update(sha=self.initial_pr_head)
        )
        calls, state, _, repository = self.runner()
        if settle_push:
            result = self.publish("push_existing_pr", repository, github_discussion_runner=prior)
            self.assertEqual(result.status, "published", result)
            _, raw = self._current_github_runner()
        else:
            self.assertFalse(settle_tapd)
            raw = prior
        _written, publish_tapd = self._tapd_publisher()
        if settle_tapd:
            actual_reader = discussion.DiscussionReader
            with patch.object(
                discussion, "DiscussionReader", side_effect=lambda **kw: actual_reader(runner=raw, stop=kw["stop"])
            ):
                result = publish_tapd(action_ids=(self.action_ids["comment"],))
            self.assertEqual(result["native_published_count"], 1, result)
        self.labels = ()
        paused = self.tick("canceled", ticket=self.ticket, github_discussion_runner=raw)
        self.assertEqual(paused.reason, "accepted_delivery_paused")
        self.labels = ("robot",)
        notice = self.tick("publication_pending", ticket=self.ticket, github_discussion_runner=raw)
        [[question, version, action]] = self.read(
            "SELECT question_id,version,action_id FROM source_confirmation_questions WHERE source_result_id=?",
            (notice.identity,),
        )
        delivered = publish_tapd(action_ids=(action,))
        self.assertEqual(delivered["native_published_count"], 1, delivered)
        body = f"@test-helper ROBERT-CHOICE {question} v{version} resume"
        fact = tapd_authority.CommentVersion(
            self.ticket, "bug", "207", utc_now(), None, sha256(body.encode()).hexdigest()
        )
        choice = tapd_authority.CommentObservation(
            fact,
            body,
            "requester",
            tapd_authority.CreatorEvidence(fact, "requester", "creator"),
            tapd_authority.OwnBodyEvidence(fact, "requester", "own-body"),
        )
        self.comments.append(choice)

        def runner(command, **kwargs):
            return (raw if "max_output_bytes" in kwargs else repository)(command, **kwargs)

        return admitted.round_id, question, choice, calls, state, runner

    def test_mixed_published_continue_choice_claims_only_the_dormant_result(self):
        from robert_agent.persistence.queries import source_delivery_recovery

        round_id, question, _choice, _calls, _state, runner = self.mixed_recovery_offer()
        ledger = self.read("SELECT * FROM request_events ORDER BY row_id")
        cancellations = self.read("SELECT * FROM source_round_cancellations")
        attempts = self.read("SELECT * FROM attempts ORDER BY attempt_id")
        before = self.read("SELECT * FROM publication_intents ORDER BY action_id")
        purposes = []
        observe_recovery = discussion.observe_recovery

        def capture(*args, **kwargs):
            proof = observe_recovery(*args, **kwargs)
            if proof is not None:
                purposes.append(proof.scope.purpose)
            return proof

        with patch.object(discussion, "observe_recovery", side_effect=capture):
            resumed = self.tick("publication_pending", ticket=self.ticket, github_discussion_runner=runner)
            again = self.tick("publication_pending", ticket=self.ticket, github_discussion_runner=runner)
        self.assertEqual(purposes, ["claim", "owned"])
        self.assertEqual(again.identity, self.root_id)
        self.assertEqual(resumed.identity, self.root_id)
        with closing(database.connect(self.db)) as conn:
            owner = source_delivery_recovery.active(conn, self.ticket.source_id)
            self.assertEqual((owner["source_result_id"], owner["question_id"]), (self.root_id, question))
            durable = source_delivery_recovery.applied_bound(conn, owner)
            self.assertEqual(durable.missing, (self.action_ids["comment"], self.action_ids["source_feedback"]))
        self.assertEqual(self.read("SELECT status FROM source_rounds WHERE round_id=?", (round_id,)), [("canceled",)])
        self.assertEqual(self.read("SELECT * FROM request_events ORDER BY row_id"), ledger)
        self.assertEqual(self.read("SELECT * FROM source_round_cancellations"), cancellations)
        self.assertEqual(self.read("SELECT * FROM attempts ORDER BY attempt_id"), attempts)
        self.assertEqual(self.read("SELECT * FROM publication_intents ORDER BY action_id"), before)
        self.assertEqual(self.read("SELECT COUNT(*) FROM source_delivery_recoveries"), [(1,)])
        self.assertEqual(self.read("SELECT COUNT(*) FROM source_resume_transitions"), [(1,)])
        from robert_agent.delivery.application import source_delivery_resume
        from robert_agent.persistence.queries import source_sessions

        with closing(database.connect(self.db)) as conn:
            lease = source_sessions.session(conn, self.ticket.source_id)
            claim = fence.SourceClaim(self.ticket.source_id, lease["owner"], lease["token"])
            revision = json.loads(source_results.tapd_source(conn, self.ticket.source_id)["metadata_json"])["revision"]
            for action in durable.missing:
                self.assertIsNone(
                    source_delivery_resume.effect_check(conn, claim, self.root_id, action, revision=revision)
                )
        _, posts, _, remote = self.remote()
        feedback = self.publish_feedback(self.action_ids["source_feedback"], runner, remote)
        self.assertEqual((feedback["native_published_count"], posts), (0, []))
        self.assertEqual(self.read("SELECT * FROM publication_intents ORDER BY action_id"), before)

    def test_feedback_only_recovery_claims_with_audited_original_code_and_exact_prefix(self):
        from robert_agent.persistence.queries import source_delivery_recovery

        round_id, question, _, calls, _, runner = self.mixed_recovery_offer(settle_tapd=True)
        receipts = self.read("SELECT * FROM publication_intents ORDER BY action_id")
        self.assertEqual(
            self.tick("publication_pending", ticket=self.ticket, github_discussion_runner=runner).identity, self.root_id
        )
        with closing(database.connect(self.db)) as conn:
            owner = source_delivery_recovery.active(conn, self.ticket.source_id)
            self.assertEqual(owner["question_id"], question)
            self.assertEqual(
                source_delivery_recovery.applied_bound(conn, owner).missing, (self.action_ids["source_feedback"],)
            )
        self.assertEqual(self.read("SELECT status FROM source_rounds WHERE round_id=?", (round_id,)), [("canceled",)])
        self.assertEqual(self.read("SELECT * FROM publication_intents ORDER BY action_id"), receipts)
        self.assertEqual(sum(command[:2] == ("git", "push") for command in calls), 1)

    def test_unhandled_original_trigger_must_be_fresh_and_remains_unconsumed_after_claim(self):
        from robert_agent.persistence.queries import source_delivery_recovery

        _, _, _, _, _, runner = self.mixed_recovery_offer(handle_github=False)
        self.assertNotIn("source_feedback", self.action_ids)
        ledger = self.read("SELECT * FROM request_events ORDER BY row_id")
        _, unavailable = self._current_github_runner(item_status=503)
        actual = discussion.observe_recovery

        def unavailable_only_when_claiming(*args, **kwargs):
            return actual(*args, **{**kwargs, "runner": unavailable})

        with patch.object(discussion, "observe_recovery", side_effect=unavailable_only_when_claiming):
            self.tick("held", ticket=self.ticket, github_discussion_runner=runner)
        self.assertFalse(self.read("SELECT * FROM source_delivery_recoveries"))
        self.assertEqual(self.read("SELECT consumer_id FROM source_confirmation_answers"), [(None,)])
        self.assertEqual(
            self.tick("publication_pending", ticket=self.ticket, github_discussion_runner=runner).identity, self.root_id
        )
        with closing(database.connect(self.db)) as conn:
            owner = source_delivery_recovery.active(conn, self.ticket.source_id)
            evidence = json.loads(owner["evidence_json"])["original_github"]
            self.assertEqual([fact["event_id"] for fact in evidence["members"]], ["event:comment:617"])
        self.assertEqual(self.read("SELECT * FROM request_events ORDER BY row_id"), ledger)

    def test_mixed_recovery_late_parent_drift_old_operation_and_changed_fence_hold_the_choice(self):
        from robert_agent.work.application import messaging

        _, _, _, _, _, runner = self.mixed_recovery_offer()
        identity = {
            "protocol": "robert.message.v1",
            "provider": "openclaw",
            "channel": "telegram",
            "conversation": "telegram:42",
            "thread": "",
            "account": "test-account",
            "sender_id": "42",
        }
        self.configured.update(db_path=str(self.db))
        self.configured["messaging"] = {
            **identity,
            "enabled": True,
            "allowed_senders": ["42"],
            "send_after": "2026-07-01T00:00:00Z",
            "target": "42",
            "repos": ["org/alpha"],
            "tapd_projects": [{"host": self.ticket.host, "workspace_id": self.ticket.workspace_id}],
        }
        self.configured["tapd"]["projects"][0]["operators"] = ["openclaw:telegram:test-account:42"]
        actual = discussion.observe_recovery
        _, drift = self._current_github_runner(parent_edit=lambda parent: parent["base"].update(sha="d" * 40))

        for scenario in ("late_parent", "operation", "generation", "receipt", "claim", "prompt"):
            with self.subTest(scenario=scenario):
                push = self.action_ids["push_existing_pr"]
                [[receipt, finished]] = self.read(
                    "SELECT remote_evidence_json,finished_at FROM publication_intents WHERE action_id=?",
                    (push,),
                )

                def changed(*args, **kw):
                    if scenario == "late_parent":
                        return actual(*args, **{**kw, "runner": drift})
                    proof = actual(*args, **kw)
                    self.assertIsNotNone(proof)
                    if scenario == "prompt":
                        from robert_agent.persistence.queries import source_delivery_pauses, source_delivery_recovery

                        with closing(database.connect(self.db)) as conn:
                            pause = source_delivery_pauses.latest(conn, self.root_id)
                            prompt = Path(source_delivery_recovery.bound_delivery(conn, pause).prompt_path)
                        original = prompt.read_bytes()
                        changed_prompt.append((prompt, original))
                        prompt.write_bytes(original + b"\nChanged original prompt.")
                    elif scenario == "operation":
                        second = actual(*args, **kw)
                        self.assertIsNotNone(second)
                        self.assertNotEqual(proof.operations, second.operations)
                    else:
                        with closing(sqlite3.connect(self.db)) as conn, conn:
                            if scenario == "generation":
                                conn.execute(
                                    "UPDATE source_delivery_pauses SET generation=generation+1 WHERE source_result_id=?",
                                    (self.root_id,),
                                )
                            elif scenario == "receipt":
                                value = json.loads(receipt)
                                value["receipt"]["observed_at"] = finished
                                conn.execute(
                                    "UPDATE publication_intents SET remote_evidence_json=? WHERE action_id=?",
                                    (json.dumps(value), push),
                                )
                            else:
                                conn.execute(
                                    "UPDATE source_sessions SET token=token+1 WHERE source_id=?",
                                    (self.ticket.source_id,),
                                )
                    return proof

                changed_prompt = []
                with patch.object(discussion, "observe_recovery", side_effect=changed):
                    self.tick(
                        "attention" if scenario == "prompt" else "held",
                        ticket=self.ticket,
                        github_discussion_runner=runner,
                    )
                for prompt, original in changed_prompt:
                    prompt.write_bytes(original)
                self.assertFalse(self.read("SELECT * FROM source_delivery_recoveries"))
                self.assertFalse(self.read("SELECT * FROM source_resume_transitions"))
                self.assertEqual(self.read("SELECT consumer_id FROM source_confirmation_answers"), [(None,)])
                if scenario == "late_parent":
                    notices = [
                        entry
                        for entry in messaging.inbox(self.configured, identity)["messages"]
                        if entry["event_type"] == "source_delivery_recovery_attention"
                    ]
                    self.assertEqual(len(notices), 1)
                    self.assertEqual(notices[0]["target"]["source_result_id"], self.root_id)
                    wrong_scope = deepcopy(self.configured)
                    wrong_scope["messaging"]["tapd_projects"][0]["workspace_id"] = "999"
                    self.assertFalse(messaging.inbox(wrong_scope, identity)["messages"])
                elif scenario in {"generation", "receipt"}:
                    with closing(sqlite3.connect(self.db)) as conn, conn:
                        if scenario == "generation":
                            conn.execute(
                                "UPDATE source_delivery_pauses SET generation=generation-1 WHERE source_result_id=?",
                                (self.root_id,),
                            )
                        else:
                            conn.execute(
                                "UPDATE publication_intents SET remote_evidence_json=? WHERE action_id=?",
                                (receipt, push),
                            )
        self.assertEqual(
            self.tick("publication_pending", ticket=self.ticket, github_discussion_runner=runner).identity, self.root_id
        )

    def test_mixed_choice_stays_unclaimed_after_unknown_history_or_late_choice_change(self):
        from robert_agent.delivery.application import source_delivery_resume
        from robert_agent.work.application import source_observation, tapd_intake

        _, _, choice, _, _, runner = self.mixed_recovery_offer()
        [[attempt, prior]] = self.read(
            "SELECT a.attempt_id,a.metadata_json FROM attempts a JOIN worker_results w USING(attempt_id) "
            "JOIN source_results s ON s.worker_result_id=w.result_id WHERE s.source_result_id=?",
            (self.root_id,),
        )
        unresolved = json.loads(prior)
        unresolved.pop("process_stopped_at", None)
        unresolved.pop("termination", None)
        unresolved["dispatch"]["cleanup_pending"] = True

        def untouched():
            self.assertFalse(self.read("SELECT * FROM source_delivery_recoveries"))
            self.assertFalse(self.read("SELECT * FROM source_resume_transitions"))
            self.assertEqual(self.read("SELECT consumer_id FROM source_confirmation_answers"), [(None,)])

        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE attempts SET metadata_json=? WHERE attempt_id=?", (json.dumps(unresolved), attempt))
        self.tick("held", ticket=self.ticket, github_discussion_runner=runner)
        untouched()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE attempts SET metadata_json=? WHERE attempt_id=?", (prior, attempt))
            conn.execute(
                "UPDATE publication_intents SET status='attention' WHERE action_id=?",
                (self.action_ids["push_existing_pr"],),
            )
        self.tick("held", ticket=self.ticket, github_discussion_runner=runner)
        untouched()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE publication_intents SET status='published' WHERE action_id=?",
                (self.action_ids["push_existing_pr"],),
            )
        raw = discussion.observe_recovery
        _, withdrawn = self._current_github_runner(body="I withdraw the original trigger")

        def late_withdrawal(*args, **options):
            return raw(*args, **{**options, "runner": withdrawn})

        with patch.object(discussion, "observe_recovery", side_effect=late_withdrawal):
            self.tick("held", ticket=self.ticket, github_discussion_runner=runner)
        untouched()

        observe = source_delivery_resume.observe

        def move_choice(*args, **options):
            result, reason = observe(*args, **options)
            self.assertIsNotNone(result, reason)
            body = "Withdraw my proceed choice"
            version = replace(choice.version, modified=utc_now(), body_sha256=sha256(body.encode()).hexdigest())
            self.comments[self.comments.index(choice)] = replace(
                choice,
                version=version,
                body_html=body,
                creator=replace(choice.creator, observation=version),
                own_body=replace(choice.own_body, observation=version),
            )
            with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
                transactions.begin_immediate(conn)
                tapd_intake.observe_ticket(
                    conn,
                    self.configured["tapd"],
                    self.provider(None, self.ticket),
                    source_observation.publication_facts(conn, self.ticket),
                    now=utc_now(),
                )
            return result, reason

        with patch.object(source_delivery_resume, "observe", side_effect=move_choice):
            self.tick("held", ticket=self.ticket, github_discussion_runner=runner)
        untouched()

    def test_public_mixed_recovery_publishes_only_the_missing_tapd_reply(self):
        round_id, _question, _choice, calls, _state, runner = self.mixed_recovery_offer()
        self.assertEqual(
            self.tick("publication_pending", ticket=self.ticket, github_discussion_runner=runner).identity, self.root_id
        )
        ledger = self.read("SELECT * FROM request_events ORDER BY row_id")
        cancellations = self.read("SELECT * FROM source_round_cancellations")
        attempts = self.read("SELECT * FROM attempts ORDER BY attempt_id")
        prefix = self.read(
            "SELECT * FROM publication_intents WHERE action_id=?", (self.action_ids["push_existing_pr"],)
        )
        written, publish_tapd = self._tapd_publisher()
        outcome = publish_tapd(
            action_ids=(self.action_ids["comment"],),
            run_command=runner,
            github_discussion_runner=runner,
        )
        self.assertEqual(outcome["native_published_count"], 1, outcome)
        self.assertEqual(outcome["native_completed_count"], 0, outcome)
        self.assertEqual(len(written), 1)
        self.assertIn("Private Worker canary ZX991", written[0])
        self.assertEqual(
            self.read(
                "SELECT publish_status FROM github_actions WHERE action_id=?",
                (self.action_ids["source_feedback"],),
            ),
            [("not_published",)],
        )
        self.assertEqual(self.read("SELECT status FROM source_rounds WHERE round_id=?", (round_id,)), [("canceled",)])
        self.assertEqual(self.read("SELECT * FROM request_events ORDER BY row_id"), ledger)
        self.assertEqual(self.read("SELECT * FROM source_round_cancellations"), cancellations)
        self.assertEqual(self.read("SELECT * FROM attempts ORDER BY attempt_id"), attempts)
        self.assertEqual(
            self.read(
                "SELECT * FROM publication_intents WHERE action_id=?",
                (self.action_ids["push_existing_pr"],),
            ),
            prefix,
        )
        self.assertEqual(sum(command[:2] == ("git", "push") for command in calls), 1)

    def test_unknown_recovered_tapd_comment_is_readback_only_after_authority_loss(self):
        from robert_agent.persistence.queries import source_attention

        round_id, _, _, _, _, runner = self.mixed_recovery_offer()
        self.assertEqual(
            self.tick("publication_pending", ticket=self.ticket, github_discussion_runner=runner).identity, self.root_id
        )
        action = self.action_ids["comment"]
        written, publish_tapd = self._tapd_publisher(unknown=True)
        first = publish_tapd(action_ids=(action,), run_command=runner, github_discussion_runner=runner)
        self.assertEqual(first["native_unknown_count"], 1, first)
        [[publication, _]] = self.read(
            "SELECT publication_id,status FROM publication_intents WHERE action_id=?", (action,)
        )
        with closing(database.connect(self.db)) as conn:
            self.assertIsNone(source_attention.publication_episode(conn, publication)["resolved_at"])
        self.allow_tapd_effect_retry(action)
        self.labels = ()
        ledger = self.read("SELECT * FROM request_events ORDER BY row_id")

        def no_git(*_args, **_options):
            self.fail("saved TAPD readback requested fresh mutating Git authority")

        second = publish_tapd(
            action_ids=(action,),
            source_reconcile_only=True,
            run_command=no_git,
            github_discussion_runner=no_git,
            tapd_identity_provider=None,
        )
        self.assertEqual(second["native_published_count"], 1, second)
        self.assertEqual(second["native_completed_count"], 0, second)
        self.assertEqual(len(written), 1)
        self.assertEqual(self.read("SELECT status FROM source_rounds WHERE round_id=?", (round_id,)), [("canceled",)])
        self.assertEqual(self.read("SELECT * FROM request_events ORDER BY row_id"), ledger)
        self.assertEqual(
            self.read("SELECT publication_id,status FROM publication_intents WHERE action_id=?", (action,)),
            [(publication, "published")],
        )
        with closing(database.connect(self.db)) as conn:
            self.assertIsNotNone(source_attention.publication_episode(conn, publication)["resolved_at"])

    def allow_tapd_effect_retry(self, action_id):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE github_actions SET metadata_json=json_remove(metadata_json,'$.publish') WHERE action_id=?",
                (action_id,),
            )

    def test_recovered_tapd_cannot_skip_a_missing_native_code_effect(self):
        _, _, _, _, _, runner = self.mixed_recovery_offer(settle_push=False)
        self.assertEqual(
            self.tick("publication_pending", ticket=self.ticket, github_discussion_runner=runner).identity, self.root_id
        )
        writes, publish_tapd = self._tapd_publisher()
        tapd = self.action_ids["comment"]
        outcome = publish_tapd(action_ids=(tapd,), run_command=runner, github_discussion_runner=runner)
        self.assertEqual((outcome["native_published_count"], writes), (0, []))
        for required in (tapd, self.action_ids["push_existing_pr"], self.action_ids["source_feedback"]):
            self.assertFalse(self.read("SELECT * FROM publication_intents WHERE action_id=?", (required,)))

    def test_recovered_tapd_detects_prompt_and_choice_changes_during_final_provider_read(self):
        from robert_agent.persistence.queries import source_delivery_pauses, source_delivery_recovery

        _, _, choice, _, _, runner = self.mixed_recovery_offer()
        self.assertEqual(
            self.tick("publication_pending", ticket=self.ticket, github_discussion_runner=runner).identity, self.root_id
        )
        action = self.action_ids["comment"]
        written, publish_tapd = self._tapd_publisher()
        ledger = self.read("SELECT * FROM request_events ORDER BY row_id")
        with closing(database.connect(self.db)) as conn:
            pause = source_delivery_pauses.latest(conn, self.root_id)
            prompt = Path(source_delivery_recovery.bound_delivery(conn, pause).prompt_path)
        original = prompt.read_bytes()
        for case in ("prompt", "choice"):
            with self.subTest(case=case):
                reads = 0

                def provider(*args, **kwargs):
                    nonlocal reads
                    reads += 1
                    if reads == 2:
                        if case == "prompt":
                            prompt.write_bytes(original + b"\nLate replacement.")
                        else:
                            body = "This no longer authorizes the old delivery"
                            changed = replace(
                                choice.version, modified=utc_now(), body_sha256=sha256(body.encode()).hexdigest()
                            )
                            self.comments[self.comments.index(choice)] = replace(
                                choice,
                                version=changed,
                                body_html=body,
                                creator=replace(choice.creator, observation=changed),
                                own_body=replace(choice.own_body, observation=changed),
                            )
                    return self.provider(*args, **kwargs)

                held = publish_tapd(
                    action_ids=(action,),
                    run_command=runner,
                    github_discussion_runner=runner,
                    tapd_observation_provider=provider,
                )
                self.assertEqual(reads, 2)
                self.assertEqual((held["native_published_count"], written), (0, []))
                self.assertFalse(self.read("SELECT * FROM publication_intents WHERE action_id=?", (action,)))
                self.assertEqual(self.read("SELECT * FROM request_events ORDER BY row_id"), ledger)
                if case == "prompt":
                    self.assertEqual(
                        self.read(
                            "SELECT COUNT(*) FROM source_recovery_attentions WHERE source_result_id=?",
                            (self.root_id,),
                        ),
                        [(1,)],
                    )
                    prompt.write_bytes(original)
                    self.allow_tapd_effect_retry(action)

    def test_recovered_tapd_requires_fresh_exact_scope_for_all_original_github_members(self):
        _, _, _, _, _, runner = self.mixed_recovery_offer(handle_github=False)
        self.assertEqual(
            self.tick("publication_pending", ticket=self.ticket, github_discussion_runner=runner).identity, self.root_id
        )
        action = self.action_ids["comment"]
        written, publish_tapd = self._tapd_publisher()
        ledger = self.read("SELECT * FROM request_events ORDER BY row_id")
        actual = discussion.observe_recovered_tapd
        for scenario in ("stale_parent", "unavailable", "wrong_operation", "lost_fence"):
            with self.subTest(scenario=scenario):

                def observe(db_path, config, claim, ticket, scope, **options):
                    if scenario in {"stale_parent", "unavailable"}:
                        _, raw_runner = self._current_github_runner(
                            item_status=503 if scenario == "unavailable" else 200,
                            parent_edit=lambda parent: (
                                parent["base"].update(sha="f" * 40) if scenario == "stale_parent" else None
                            ),
                        )
                        return actual(db_path, config, claim, ticket, scope, **{**options, "runner": raw_runner})
                    if scenario == "wrong_operation":
                        with closing(database.connect(self.db)) as conn:
                            owned = discussion.accepted_recovery_scope(
                                conn,
                                scope.root_id,
                                generation=scope.generation,
                                question_id=scope.question_id,
                                transition_id=scope.transition_id,
                                action_id=action,
                            )
                        self.assertIsNotNone(owned)
                        proof = discussion.observe_recovery(db_path, config, claim, ticket, owned, **options)
                        self.assertIsNotNone(proof)
                        return proof
                    proof = actual(db_path, config, claim, ticket, scope, **options)
                    self.assertIsNotNone(proof)
                    with closing(sqlite3.connect(self.db)) as conn, conn:
                        conn.execute(
                            "UPDATE source_sessions SET token=token+1 WHERE source_id=?", (self.ticket.source_id,)
                        )
                    return proof

                with patch.object(discussion, "observe_recovered_tapd", side_effect=observe):
                    outcome = publish_tapd(action_ids=(action,), run_command=runner, github_discussion_runner=runner)
                self.assertEqual((outcome["native_published_count"], written), (0, []))
                self.assertFalse(self.read("SELECT * FROM publication_intents WHERE action_id=?", (action,)))
                self.assertEqual(self.read("SELECT * FROM request_events ORDER BY row_id"), ledger)
                self.allow_tapd_effect_retry(action)
