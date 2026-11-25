import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess

from robert_agent.adapters.git.review_revision import git
from robert_agent.configuration.validation import validate_config
from robert_agent.delivery import queries
from robert_agent.delivery.application import audit
from robert_agent.execution.stages import StageStopped
from robert_agent.persistence import transactions
from robert_agent.persistence.rounds import pre_pr_review as store, review as native_store
from robert_agent.work.application import github_requests
from robert_agent.workflows.pre_pr_review.coordinator import Coordinator
from tests.support import database
from tests.support.pre_pr_review import PrePRReviewFixture


class ReviewRemote:
    def __init__(self, driver, base):
        self.scope = driver.scope
        self.base = base
        self.head = None
        self.pr = None
        self.comments = []
        self.events = []
        self.lose = None
        self.repo = self.scope["repo"]
        self.url = f"https://github.com/{self.repo}/pull/72"
        self.push_url = f"https://github.com/{self.repo}.git"

    def completed(self, args, value, *, effect=None):
        if effect:
            self.events.append(effect)
            if self.lose == effect:
                self.lose = None
                raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(value), stderr="")

    def __call__(self, args, **kwargs):
        if args[0] == "git":
            if args[1:3] == ["remote", "get-url"]:
                return subprocess.CompletedProcess(args, 0, stdout=self.push_url + "\n", stderr="")
            if args[1] == "ls-remote":
                if args[2] == "--get-url":
                    text = self.push_url + "\n"
                else:
                    ref = args[-1].removeprefix("refs/heads/")
                    head = self.base if ref == self.scope["base_branch"] else self.head
                    text = f"{head}\trefs/heads/{ref}\n" if head else ""
                return subprocess.CompletedProcess(args, 0, stdout=text, stderr="")
            if args[1] == "push":
                self.head = args[-1].split(":", 1)[0]
                if self.pr:
                    self.pr["head"]["sha"] = self.head
                return self.completed(args, {}, effect="push")
            return subprocess.run(args, **kwargs)
        if args[1:3] == ["pr", "list"]:
            return self.completed(args, [{"number": 72}] if self.pr else [])
        if args[1:3] == ["pr", "create"]:
            branch = self.scope["branch_name"]
            self.pr = {
                "head": {"ref": branch, "sha": self.head, "repo": {"full_name": self.repo}},
                "base": {"ref": self.scope["base_branch"], "sha": self.base, "repo": {"full_name": self.repo}},
                "user": {"login": self.scope["github_account"]},
                "body": args[args.index("--body") + 1],
                "title": args[args.index("--title") + 1],
                "draft": "--draft" in args,
                "number": 72,
                "html_url": self.url,
                "state": "open",
            }
            return self.completed(args, self.url, effect="open")
        if args[1:3] == ["api", "user"]:
            return self.completed(args, {"login": self.scope["github_account"]})
        if args[1:3] == ["api", f"repos/{self.repo}/pulls/72"]:
            if "PATCH" in args:
                self.pr["body"] = args[-1].removeprefix("body=")
                return self.completed(args, self.pr, effect="description")
            return self.completed(args, self.pr)
        if args[1:3] == ["api", f"repos/{self.repo}/issues/72/comments?per_page=100"]:
            return self.completed(args, [self.comments])
        if args[1:3] == ["api", f"repos/{self.repo}/issues/72/comments"]:
            body = args[-1].removeprefix("body=")
            comment = {"body": body, "id": len(self.comments) + 1, "user": self.pr["user"]}
            self.comments.append(comment)
            kind = "report" if " 第一轮评审" in body else "completion"
            return self.completed(args, comment, effect=kind)
        raise AssertionError(args[:3])


class EarlyReviewPublicationTests(PrePRReviewFixture):
    def setup_publication(self, workers=("claude",), *, local=False):
        driver = self.start(list(workers), legacy=False, legacy_report=False, pr_first=True, local=local)
        remote = ReviewRemote(driver, self.git_fixture.base)
        driver.publication_runner = remote
        driver.publication_config = self.config_path
        launch = driver.launcher

        def observe(**kwargs):
            name = Path(kwargs["prompt_path"]).name
            if name == "reviewer-prompt.md":
                self.assertTrue(remote.pr["draft"])
                self.assertIn("等待第一轮评审", remote.pr["body"])
                with database.connect(self.db) as conn:
                    round = store.session(conn, round_id=driver.round_id)
                    self.assertFalse(round["proof"])
                    self.assertEqual(round["request"]["review_publication"]["pr"]["number"], 72)
                remote.events.append("review")
            elif name.startswith("address-"):
                self.assertTrue(remote.comments)
                remote.events.append("repair")
            return launch(**kwargs)

        driver.launcher = observe
        return driver, remote

    def test_review_starts_only_after_draft_pr_is_confirmed(self):
        driver, remote = self.setup_publication()
        result = driver.run()

        self.assertTrue(result["ok"], result)
        self.assertEqual(remote.events, ["push", "open", "review", "report", "description", "completion"])
        self.assertIn("Fixes #42", remote.pr["body"])
        self.cycle([])
        with database.connect(self.db) as conn:
            entry = store.session(conn, round_id=driver.round_id)
            self.assertEqual(entry["proof"]["publication"]["initial"]["number"], 72)
            self.assertEqual(entry["proof"]["head_sha"], remote.head)
            self.assertEqual(queries.ready_actions(conn, 10), [])

    def test_web_origin_comments_are_scoped_to_the_registered_pull_request(self):
        driver, remote = self.setup_publication(local=True)
        result = driver.run()

        self.assertTrue(result["ok"], result)
        self.assertEqual([comment["id"] for comment in remote.comments], [1, 2])
        with database.connect(self.db) as conn:
            publication = store.session(conn, round_id=driver.round_id)["request"]["review_publication"]
        self.assertEqual(publication["report"]["target_url"], remote.url)
        self.assertEqual(publication["completion"]["target_url"], remote.url)
        wrong_target = {**publication["pr"], "url": f"https://github.com/{remote.repo}/pull/99"}
        self.assertIsNotNone(
            audit.managed_review_comment_violation(
                "report", wrong_target, driver.scope, ["open_pr", "push_existing_pr"]
            )
        )

    def test_description_edit_after_draft_registration_is_preserved(self):
        driver, remote = self.setup_publication()
        launch = driver.launcher

        def edit_metadata(**kwargs):
            process = launch(**kwargs)
            if Path(kwargs["prompt_path"]).name == "reviewer-prompt.md":
                remote.pr["body"] = "Maintainer update"
            return process

        driver.launcher = edit_metadata
        result = driver.run()

        self.assertTrue(result["ok"], result)
        self.assertEqual(remote.pr["body"], "Maintainer update")
        self.assertEqual(remote.events, ["push", "open", "review", "report", "completion"])

    def test_changed_remote_base_stops_completion_even_when_no_push_is_needed(self):
        driver, remote = self.setup_publication()
        completed = remote.completed

        def advance_base(args, value, *, effect=None):
            result = completed(args, value, effect=effect)
            if effect == "report":
                remote.base = "f" * 40
            return result

        remote.completed = advance_base

        with self.assertRaisesRegex(StageStopped, "review publication base changed"):
            driver.run()
        self.assertEqual(remote.events, ["push", "open", "review", "report"])

    def test_repaired_commit_updates_the_same_draft(self):
        driver, remote = self.setup_publication(("claude", "codex"))
        self.with_finding = True
        result = driver.run()

        self.assertTrue(result["ok"], result)
        self.assertEqual(
            remote.events,
            ["push", "open", "review", "review", "report", "repair", "push", "description", "completion"],
        )
        with database.connect(self.db) as conn:
            state = store.session(conn, round_id=driver.round_id)["request"]["review_publication"]
        self.assertNotEqual(state["pr"]["initial_head"], remote.head)
        self.assertEqual(state["complete"]["head_sha"], remote.head)
        self.assertIn("Updated the value", remote.comments[-1]["body"])

    def test_required_gate_retry_updates_the_existing_draft_without_another_review(self):
        driver, remote = self.setup_publication()
        self.with_finding = True
        launch = driver.launcher
        failed_head = []

        def fail_first_gate(**kwargs):
            prompt = Path(kwargs["prompt_path"])
            addressing = prompt.name.startswith("address-")
            if addressing and failed_head:
                self.assertIn("verification-retry", prompt.read_text())
                self.assertEqual(git(kwargs["worktree_path"], "rev-parse", "HEAD"), failed_head[0])
            process = launch(**kwargs)
            if addressing and not failed_head:
                output = Path(kwargs["log_dir"]) / "dispositions.json"
                result = json.loads(output.read_text())
                failed_head.append(result["final_sha"])
                result["verification"][0].update(status="failed", exit_code=1)
                output.write_text(json.dumps(result))
            return process

        driver.launcher = fail_first_gate
        self.assertTrue(driver.run()["ok"])
        self.assertEqual(self.launches["reviewer-prompt.md"], 1)
        self.assertEqual(self.launches["address"], 2)
        self.assertEqual(self.launches["verify_final"], 0)
        self.assertEqual(remote.events.count("open"), 1)
        self.assertEqual(remote.events.count("report"), 1)
        self.assertEqual(remote.events.count("completion"), 1)
        self.assertEqual(remote.head, failed_head[0])

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

    def test_remote_head_drift_stops_before_repair(self):
        driver, remote = self.setup_publication()
        self.with_finding = True
        launch = driver.launcher

        def advance_remote(**kwargs):
            result = launch(**kwargs)
            if Path(kwargs["prompt_path"]).name == "reviewer-prompt.md":
                remote.head = remote.pr["head"]["sha"] = "a" * 40
            return result

        driver.launcher = advance_remote
        with self.assertRaisesRegex(StageStopped, "identity or head changed"):
            driver.run()
        self.assertEqual(remote.events, ["push", "open", "review"])

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

    def test_high_finding_requests_one_bound_final_review(self):
        driver, remote = self.setup_publication()
        self.with_finding = True
        launch = driver.launcher

        def high_finding(**kwargs):
            value = launch(**kwargs)
            path = Path(kwargs["log_dir"]) / "result.json"
            if Path(kwargs["prompt_path"]).name == "reviewer-prompt.md":
                data = json.loads(path.read_text())
                data["findings"][0]["severity"] = "high"
                path.write_text(json.dumps(data))
            return value

        driver.launcher = high_finding

        result = driver.run()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.launches["verify_final"], 1)
        self.assertIn("critical_or_high_finding", remote.comments[-1]["body"])

    def test_unreviewed_changed_file_gets_targeted_check_without_repair(self):
        driver, remote = self.setup_publication(("claude", "codex"))
        launch = driver.launcher

        def report_gap(**kwargs):
            result = launch(**kwargs)
            if Path(kwargs["prompt_path"]).name == "reviewer-prompt.md":
                path = Path(kwargs["log_dir"]) / "result.json"
                value = json.loads(path.read_text())
                value["coverage"].update(reviewed_paths=[], unreviewed_paths=["source.py"])
                path.write_text(json.dumps(value))
            return result

        driver.launcher = report_gap
        result = driver.run()

        self.assertTrue(result["ok"], result)
        self.assertEqual(self.launches["address"], 0)
        self.assertEqual(self.launches["verify_final"], 1)
        self.assertIn("unreviewed_changed_path: source.py", remote.comments[-1]["body"])

    def test_real_decision_keeps_draft_and_unpushed_repair(self):
        driver, remote = self.setup_publication()
        self.with_finding = self.needs_decision = True

        with self.assertRaisesRegex(StageStopped, "require an operator decision"):
            driver.run()

        self.assertEqual(remote.events.count("push"), 1)
        self.assertEqual(len(remote.comments), 2)
        self.assertIn("待决定", remote.comments[-1]["body"])
        self.assertTrue(remote.pr["draft"])

    def test_revoked_source_trust_prevents_publication_after_review(self):
        import yaml

        driver, remote = self.setup_publication()
        launch = driver.launcher

        def revoke(**kwargs):
            result = launch(**kwargs)
            if Path(kwargs["prompt_path"]).name == "reviewer-prompt.md":
                settings = yaml.safe_load(self.config_path.read_text())
                settings["repos"][0]["trusted_actors"] = ["other-maintainer"]
                self.config_path.write_text(yaml.safe_dump(settings))
            return result

        driver.launcher = revoke
        with self.assertRaisesRegex(StageStopped, "does not authorize"):
            driver.run()
        self.assertEqual(remote.events, ["push", "open", "review"])

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

    def test_refreshed_unverified_head_is_not_published(self):
        driver, remote = self.setup_publication()
        driver.prepare()
        original_head = driver.manifest["head_sha"]
        upstream = self.git_fixture.upstream
        git(upstream, "checkout", "main")
        (upstream / "consumer.py").write_text("from source import value\nchanges_contract = True\n")
        git(upstream, "add", "consumer.py")
        git(upstream, "commit", "-m", "change related source")
        remote.base = git(upstream, "rev-parse", "HEAD")

        with self.assertRaisesRegex(StageStopped, "requires implementation verification for the reviewed SHA"):
            driver.run()
        self.assertNotEqual(driver.manifest["head_sha"], original_head)
        self.assertEqual(self.launches, {"implement": 1})
        self.assertEqual(remote.events, [])
