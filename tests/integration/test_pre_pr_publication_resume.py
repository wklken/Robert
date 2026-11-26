import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import call, patch

from robert_agent.adapters.git.review_revision import git
from robert_agent.configuration.validation import validate_config
from robert_agent.delivery.application import review_publication
from robert_agent.execution.stages import StageStopped
from robert_agent.persistence import transactions
from robert_agent.persistence.rounds import pre_pr_review as store
from robert_agent.persistence.rounds import review as native_store
from robert_agent.work.application import commands, github_requests
from robert_agent.workflows.pre_pr_review.coordinator import Coordinator
from tests.support import database
from tests.support.pre_pr_publication import PrePRPublicationFixture


class EarlyReviewPublicationResumeTests(PrePRPublicationFixture):
    def test_unknown_create_or_report_is_adopted_without_repeating_models_or_comments(self):
        driver, remote = self.setup_publication(("claude", "codex"))
        remote.lose = "open"
        with self.assertRaises(subprocess.CalledProcessError):
            driver.run()
        self.assertEqual(self.launches, {"implement": 1})
        remote.lose = "report"
        restarted = Coordinator(
            self.db,
            driver.task_id,
            driver.attempt_id,
            launcher=driver.launcher,
            poll_seconds=0.01,
            config_path=self.config_path,
        )
        restarted.publication_runner = remote
        with self.assertRaises(subprocess.CalledProcessError):
            restarted.run()
        self.assertEqual(self.launches, {"implement": 1, "reviewer-prompt.md": 2})

        result = restarted.run()

        self.assertTrue(result["ok"], result)
        self.assertEqual(self.launches, {"implement": 1, "reviewer-prompt.md": 2})
        self.assertEqual(remote.events.count("report"), 1)
        self.assertEqual(remote.events.count("open"), 1)

    def test_unknown_repair_push_is_reconciled_without_a_new_repair(self):
        driver, remote = self.setup_publication()
        self.with_finding = True
        launch = driver.launcher

        def interrupt_push(**kwargs):
            if Path(kwargs["prompt_path"]).name.startswith("address-"):
                remote.lose = "push"
            return launch(**kwargs)

        driver.launcher = interrupt_push
        with self.assertRaises(subprocess.CalledProcessError):
            driver.run()
        self.assertEqual(remote.events.count("push"), 2)
        restarted = Coordinator(
            self.db,
            driver.task_id,
            driver.attempt_id,
            launcher=driver.launcher,
            poll_seconds=0.01,
            config_path=self.config_path,
        )
        restarted.publication_runner = remote

        result = restarted.run()

        self.assertTrue(result["ok"], result)
        self.assertEqual(remote.events.count("push"), 2)
        self.assertEqual(remote.events.count("repair"), 1)
        self.assertEqual(remote.events.count("completion"), 1)
        self.assertEqual(remote.events.count("open"), 1)

    def test_transient_pr_creation_response_reconciles_automatically(self):
        driver, remote = self.setup_publication()
        completed = remote.completed

        def lose_response(args, value, *, effect=None):
            response = completed(args, value, effect=effect)
            if effect == "open":
                raise subprocess.CalledProcessError(1, args, stderr="HTTP 503")
            return response

        remote.completed = lose_response
        wait = driver.stopping.wait

        def skip_retry_backoff(seconds):
            # This scenario exercises reconciliation after the delay is scheduled.
            return False if seconds == 5 else wait(seconds)

        with patch.object(driver.stopping, "wait", side_effect=skip_retry_backoff) as waiting:
            self.assertTrue(driver.run()["ok"])
        self.assertEqual(waiting.call_args_list.count(call(5)), 1)
        self.assertEqual(remote.events.count("open"), 1)
        self.assertEqual(remote.events.count("push"), 1)
        self.assertEqual(self.launches["reviewer-prompt.md"], 1)
        with store.connect(self.db) as conn:
            entry = store.session(conn, round_id=driver.round_id)
            self.assertEqual(entry["request"]["publication_retry"]["stages"], {"preliminary_publication": 1})

    def test_authorized_retrigger_can_reconcile_and_retry_exhausted_write(self):
        driver, remote = self.setup_publication()
        attempts = []

        def unavailable(args, **kwargs):
            if args[:3] == ["gh", "pr", "create"]:
                attempts.append(args)
                raise subprocess.CalledProcessError(1, args, stderr="HTTP 503")
            return remote(args, **kwargs)

        driver.publication_runner = unavailable
        with patch.object(driver.stopping, "wait", return_value=False):
            with self.assertRaises(review_publication.TransientPublicationError):
                driver.run()
        self.assertEqual(len(attempts), 3)
        self.assertIsNone(remote.pr)

        driver.publication_runner = remote
        with self.assertRaisesRegex(StageStopped, "retry budget exhausted"):
            driver.run()
        with store.connect(self.db) as conn:
            transactions.begin(conn, immediate=True)
            entry = store.session(conn, round_id=driver.round_id)
            request = {**entry["request"], "retry_attempt_id": driver.attempt_id}
            store.update(conn, driver.round_id, request_json=json.dumps(request))

        self.assertTrue(driver.run()["ok"])
        self.assertEqual(remote.events.count("open"), 1)
        self.assertEqual(remote.events.count("push"), 1)
        self.assertEqual(self.launches["implement"], 1)

    def test_concurrent_equivalent_review_triggers_share_initial_report(self):
        driver, remote = self.setup_publication(("claude", "codex"))
        repo = validate_config(self.config_path, skip_external=True)["repos"][0]
        launch = driver.launcher

        def requests_after_draft(**kwargs):
            result = launch(**kwargs)
            if (
                Path(kwargs["prompt_path"]).name != "reviewer-prompt.md"
                or kwargs["worker_agent_config"]["name"] != "claude"
            ):
                return result
            events = [
                {
                    **self.event("comment", author=repo["github_account"]),
                    "event_fingerprint": "comment:generic-review",
                    "source_key": "example/backend:pull_request:72",
                    "body_authorization_complete": True,
                    "number": 72,
                },
                {
                    **self.event("request", author=repo["github_account"]),
                    "number": 72,
                    "event_type": "review_request",
                    "event_fingerprint": "review-request:generic-review",
                    "source_key": "example/backend:pull_request:72",
                    "requester_login": "maintainer",
                    "requested_reviewer": repo["github_account"],
                    "body": remote.pr["body"],
                },
            ]

            def admit(event, observed_pr=None):
                with store.connect(self.db) as conn:
                    transactions.begin(conn, immediate=True)
                    github_requests.enqueue_native_request(
                        conn,
                        native_store,
                        repo,
                        event,
                        {"native_review_workers": driver.plan["workers"]},
                        driver.plan["workers"],
                        None,
                        store.now(),
                        observed_pr=observed_pr or remote.pr,
                    )

            with ThreadPoolExecutor(2) as pool:
                list(pool.map(admit, events))
            admit(
                {
                    **events[0],
                    "event_fingerprint": "comment:request-with-acceptance",
                    "acceptance_criteria": "Check compatibility with older clients.",
                    "workstream_id": driver.scope["workstream_id"],
                }
            )
            admit(
                {
                    **events[0],
                    "event_fingerprint": "comment:request-after-requirements-edit",
                    "workstream_id": driver.scope["workstream_id"],
                },
                {**remote.pr, "body": remote.pr["body"] + "\n\nAcceptance: also verify older clients."},
            )
            return result

        driver.launcher = requests_after_draft
        self.assertTrue(driver.run()["ok"])
        with database.connect(self.db) as conn:
            request = store.session(conn, round_id=driver.round_id)["request"]
            triggers = request["review_publication_triggers"]
            self.assertEqual(set(triggers), {"comment:generic-review", "review-request:generic-review"})
            self.assertEqual(conn.execute("SELECT count(*) FROM review_sessions").fetchone()[0], 2)
            self.assertEqual({row["completion"]["id"] for row in triggers.values()}, {"1"})
        self.assertEqual(self.launches["reviewer-prompt.md"], 2)

    def test_stopped_draft_without_report_admits_new_trusted_review(self):
        driver, remote = self.setup_publication(("claude", "codex"))
        repo = validate_config(self.config_path, skip_external=True)["repos"][0]
        driver.prepare()
        driver.preliminary_publication()
        with store.connect(self.db) as conn:
            transactions.begin(conn, immediate=True)
            store.update(conn, driver.round_id, status="attention")
            event = {
                **self.event("comment", author=repo["github_account"]),
                "event_fingerprint": "comment:review-after-stop",
                "source_key": "example/backend:pull_request:72",
                "body_authorization_complete": True,
                "number": 72,
                "workstream_id": driver.scope["workstream_id"],
            }
            github_requests.enqueue_native_request(
                conn,
                native_store,
                repo,
                event,
                {"native_review_workers": driver.plan["workers"]},
                driver.plan["workers"],
                None,
                store.now(),
                observed_pr=remote.pr,
            )
            saved = store.session(conn, round_id=driver.round_id)
            self.assertNotIn("review_publication_triggers", saved["request"])
        with database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM review_sessions").fetchone()[0], 1)

    def test_compatible_source_base_advance_reuses_verified_head_before_draft(self):
        driver, remote = self.setup_publication()
        driver.prepare()
        original_head = driver.manifest["head_sha"]
        upstream = self.git_fixture.upstream
        git(upstream, "checkout", "main")
        (upstream / "independent.py").write_text("independent_value = 42\n")
        git(upstream, "add", "independent.py")
        git(upstream, "commit", "-m", "advance independent source")
        remote.base = git(upstream, "rev-parse", "HEAD")

        result = driver.run()

        self.assertTrue(result["ok"], result)
        self.assertEqual(self.launches, {"implement": 1, "reviewer-prompt.md": 1})
        self.assertEqual(remote.head, original_head)
        self.assertEqual(remote.pr["base"]["sha"], remote.base)

    def test_related_base_refresh_validates_new_head_before_publishing(self):
        driver, remote = self.setup_publication()
        driver.prepare()
        original_head = driver.manifest["head_sha"]
        upstream = self.git_fixture.upstream
        git(upstream, "checkout", "main")
        (upstream / "consumer.py").write_text("from source import value\nchanges_contract = True\n")
        git(upstream, "add", "consumer.py")
        git(upstream, "commit", "-m", "change related source")
        remote.base = git(upstream, "rev-parse", "HEAD")

        launch = driver.launcher

        def validate_refreshed_candidate(**kwargs):
            if Path(kwargs["prompt_path"]).name.startswith("verify-candidate-"):
                self.assertEqual(remote.events, [])
                head = git(kwargs["worktree_path"], "rev-parse", "HEAD")
                self.assertNotEqual(head, original_head)
                self.launches["validate_candidate"] += 1
                (Path(kwargs["log_dir"]) / "candidate-verification.json").write_text(
                    json.dumps({"head_sha": head, "base_sha": remote.base, "verification": self.verification})
                )
                return {"ok": True, "process_exited": True, "returncode": 0, "pid": 1073741824}
            return launch(**kwargs)

        driver.launcher = validate_refreshed_candidate
        self.assertTrue(driver.run()["ok"])
        self.assertEqual(self.launches, {"implement": 1, "validate_candidate": 1, "reviewer-prompt.md": 1})
        self.assertEqual(remote.events, ["push", "open", "review", "report", "description", "completion"])
        self.assertNotEqual(remote.head, original_head)

    def test_decision_holds_draft_and_authenticated_reply_continues_from_partial_repair(self):
        driver, remote = self.setup_publication(local=True)
        self.with_finding = self.needs_decision = True
        self.assertTrue(driver.run()["ok"])
        self.assertEqual(remote.events.count("push"), 1)
        self.assertEqual(len(remote.comments), 2)
        self.assertIn("待决定", remote.comments[-1]["body"])
        self.assertTrue(remote.pr["draft"])
        original_head = remote.head

        self.cycle([])
        with database.connect(self.db) as conn:
            item = conn.execute("SELECT work_item_id,version FROM work_items").fetchone()
            question = conn.execute(
                "SELECT event_id FROM work_item_events WHERE event_type='operator_question'"
            ).fetchone()
            entry = store.session(conn, round_id=driver.round_id)
            self.assertEqual(entry["status"], "waiting_for_user")
            partial = entry["proof"]["decision"]["source_checkpoint"]["worktree_head"]
            self.assertNotEqual(partial, original_head)

        commands.execute_command(
            self.db,
            item[0],
            context=commands.CommandContext(
                "operator", "owner", frozenset({"repo:example/backend"}), frozenset({"codex", "claude"})
            ),
            command="reply",
            expected_version=item[1],
            expected_event_id=question[0],
            idempotency_key="pr-first-reply",
            body="Use the explicit supported compatibility mode.",
        )
        self.cycle([])
        task, attempt = self.current()
        self.needs_decision = False

        def launch(**kwargs):
            if Path(kwargs["prompt_path"]).name.startswith("address-"):
                self.assertEqual(git(kwargs["worktree_path"], "rev-parse", "HEAD"), partial)
            return driver.launcher(**kwargs)

        resumed = Coordinator(self.db, task, attempt, launcher=launch, poll_seconds=0.01, config_path=self.config_path)
        resumed.publication_runner = remote
        self.assertEqual(resumed.round_id, driver.round_id)
        self.assertTrue(resumed.run()["ok"])
        self.assertEqual(remote.events.count("open"), 1)
        self.assertEqual(remote.events.count("report"), 1)
        self.assertEqual(remote.head, partial)
        self.assertEqual(self.launches["reviewer-prompt.md"], 1)
