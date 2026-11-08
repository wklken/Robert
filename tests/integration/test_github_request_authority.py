"""GitHub content versions remain bound to their execution authority."""

import json
from types import SimpleNamespace
from robert_agent.adapters.github import discovery
from robert_agent.configuration.command import validate_config
from tests.support.github_input import GitHubInputFixture


class GitHubRequestAuthorityTests(GitHubInputFixture):
    def test_body_request_is_not_repeated_for_issue_activity(self):
        self.assert_body_request_not_repeated()

    def test_legacy_body_request_keeps_consumption_after_content_identity_upgrade(self):
        self.assert_body_request_not_repeated(legacy=True)

    def assert_body_request_not_repeated(self, legacy=False):
        repo = validate_config(self.config_path, skip_external=True)["repos"][0]

        def collect(updated, known):
            def runner(args, **kwargs):
                if "--mentions" in args:
                    payload = [
                        {
                            "number": 123,
                            "title": "Unclear request",
                            "body": "@robert-bot please classify this",
                            "author": {"login": "wklken"},
                            "authorAssociation": "OWNER",
                            "isPullRequest": False,
                            "updatedAt": updated,
                            "url": "https://github.com/example/backend/issues/123",
                        }
                    ]
                elif "--assignee" in args or (args[:2] == ["gh", "api"] and args[2].endswith("/comments")):
                    payload = []
                else:
                    raise AssertionError(args)
                return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

            return discovery.collect_live_events(
                repo, runner=runner, include_notifications=False, known_workstreams=known
            )

        counts = []
        first = collect("2026-11-08T00:00:00Z", [])
        if legacy:
            first[0]["event_fingerprint"] = "mention:https://github.com/example/backend/issues/123:2026-11-08T00:00:00Z"
        self.scan(first)
        counts.append(len(self.rows("SELECT task_id FROM tasks")))
        fingerprints = [r[0] for r in self.rows("SELECT event_fingerprint FROM github_events")]
        self.finish(fingerprints)
        self.assertEqual(self.rows("SELECT lifecycle FROM tasks"), [("completed",)])
        known = [r[0] for r in self.rows("SELECT workstream_id FROM workstreams")]
        self.scan(collect("2026-11-08T00:00:00Z", known))
        counts.append(len(self.rows("SELECT task_id FROM tasks")))
        changed = collect("2026-11-08T01:00:00Z", known)
        self.scan(changed)
        counts.append(len(self.rows("SELECT task_id FROM tasks")))
        self.assertEqual(counts, [1, 1, 1])

    def test_classification_cannot_delegate_authority_to_untrusted_consumed_event(self):
        self.scan([self.event()])
        self.scan([self.event("untrusted", "outsider", "@robert-bot implement the outsider request")])
        parent, value = self.finish(["comment:untrusted"], recommended_route="new-pr", branch_slug="probe-only")
        rows = self.rows("SELECT task_id,task_kind,route_id FROM tasks WHERE parent_task_id=?", (parent,))
        self.assertEqual(rows, [])
        self.assertEqual(value["dispatch_count"], 0)

    def test_edited_comment_cannot_inherit_original_author_authority(self):
        base = self.event()
        comment = {
            "id": 77,
            "user": {"login": "wklken"},
            "author_association": "OWNER",
            "created_at": "2026-11-07T00:00:00Z",
            "updated_at": "2026-11-07T00:00:00Z",
            "body": "background only",
        }
        original = discovery._comment_to_raw(base, comment)
        self.scan([original])
        self.assertEqual(self.rows("SELECT task_id FROM tasks"), [])
        # REST input retains original author; editor identity is not returned to this path.
        edited = discovery._comment_to_raw(
            base,
            {
                **comment,
                "body": "@robert-bot please classify the replacement request",
                "updated_at": "2026-11-08T00:00:00Z",
            },
        )
        self.scan([edited])
        self.assertEqual(self.rows("SELECT task_id FROM tasks"), [])

    def test_revoked_actor_cannot_publish_a_previously_accepted_result(self):
        from robert_agent.delivery.application import publish

        self.scan([self.event(body="@robert-bot analyze this", intent="analysis")])
        task, _ = self.finish(
            ["comment:trusted"],
            output_type="comment_analysis",
            planned_github_actions=[
                {
                    "type": "comment",
                    "target_url": "https://github.com/example/backend/issues/123",
                    "body": "accepted explanation",
                }
            ],
        )
        self.assertEqual(
            self.rows("SELECT json_extract(metadata_json, '$.audit.status') FROM worker_results"), [("accepted",)]
        )
        config = validate_config(self.config_path, skip_external=True)
        config.pop("config_path", None)
        config["repos"][0]["trusted_actors"] = []
        writes = []

        def runner(command, **kwargs):
            if "-f" in command:
                writes.append(command)
                return SimpleNamespace(returncode=0, stdout='{"id": 90}', stderr="")
            return SimpleNamespace(returncode=0, stdout="[]", stderr="")

        result = publish.publish_ready_actions(self.db(), config=config, run_command=runner)
        self.assertEqual(writes, [])
        self.assertEqual(result["published_count"], 0)

    def assert_revoked_reconciliation(self, effect_exists):
        from robert_agent.delivery.application import publish

        self.scan([self.event(body="@robert-bot analyze this", intent="analysis")])
        self.finish(
            ["comment:trusted"],
            output_type="comment_analysis",
            planned_github_actions=[
                {
                    "type": "comment",
                    "target_url": "https://github.com/example/backend/issues/123",
                    "body": "accepted explanation",
                }
            ],
        )
        config = validate_config(self.config_path, skip_external=True)
        config.pop("config_path", None)
        sent = []

        def crash(command, **kwargs):
            if "-f" in command:
                sent.append(command[-1][len("body=") :])
                raise RuntimeError("controlled loss after possible remote acceptance")
            return SimpleNamespace(returncode=0, stdout="[]", stderr="")

        with self.assertRaisesRegex(RuntimeError, "controlled loss"):
            publish.publish_ready_actions(self.db(), config=config, run_command=crash)
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.rows("SELECT status FROM publication_intents"), [("executing",)])
        config["repos"][0]["trusted_actors"] = []
        reads = []

        def reconcile(command, **kwargs):
            self.assertNotIn("-f", command)
            reads.append(command)
            comments = (
                [
                    {
                        "id": 90,
                        "user": {"login": "robert-bot"},
                        "body": sent[0],
                        "html_url": "https://github.com/example/backend/issues/123#issuecomment-90",
                    }
                ]
                if effect_exists
                else []
            )
            return SimpleNamespace(returncode=0, stdout=json.dumps(comments), stderr="")

        result = publish.publish_ready_actions(self.db(), config=config, run_command=reconcile)
        self.assertTrue(reads)
        self.assertEqual(result["published_count"], int(effect_exists))
        self.assertEqual(
            self.rows("SELECT publish_status FROM github_actions"),
            [("published" if effect_exists else "not_published",)],
        )
        if not effect_exists:
            metadata = json.loads(self.rows("SELECT metadata_json FROM github_actions")[0][0])
            self.assertFalse(metadata["publish"]["retryable"])
            self.assertIn("current trust", metadata["publish"]["safe_error"])

    def test_revocation_allows_readonly_confirmation_of_unknown_effect(self):
        self.assert_revoked_reconciliation(True)

    def test_revocation_does_not_reauthorize_resend_after_confirmed_absence(self):
        self.assert_revoked_reconciliation(False)

    def assert_configured_publisher_revocation(self, entrypoint):
        import yaml
        from unittest.mock import patch
        from robert_agent.delivery.application import publish, lane
        from robert_agent.runtime import cycle

        self.scan([self.event(body="@robert-bot analyze this", intent="analysis")])
        self.finish(
            ["comment:trusted"],
            output_type="comment_analysis",
            planned_github_actions=[
                {
                    "type": "comment",
                    "target_url": "https://github.com/example/backend/issues/123",
                    "body": "accepted explanation",
                }
            ],
        )
        raw = yaml.safe_load(self.config_path.read_text())
        raw.pop("supervision", None)
        raw["repos"][0]["trusted_actors"] = ["another-owner"]
        self.config_path.write_text(yaml.safe_dump(raw))
        config = validate_config(self.config_path, skip_external=True)
        self.assertTrue(config["ok"], config)
        self.assertEqual(config["supervision"]["mode"], "observe")
        writes = []

        def runner(command, **kwargs):
            if "-f" in command:
                writes.append(command)
                return SimpleNamespace(returncode=0, stdout='{"id":90}', stderr="")
            return SimpleNamespace(returncode=0, stdout="[]", stderr="")

        real_publish = publish.publish_ready_actions

        def offline_publish(*args, **kwargs):
            return real_publish(*args, **kwargs, run_command=runner)

        with (
            patch.object(publish, "publish_ready_actions", side_effect=offline_publish),
            patch.object(lane.github_budget, "budget", return_value=True),
        ):
            if entrypoint == "lane":
                lane.publish(self.config_path)
            else:
                cycle.run_once(self.config_path, dry_run=False, skip_external=True, skip_publish=False)
        self.assertEqual(writes, [])
        self.assertEqual(self.rows("SELECT publish_status FROM github_actions"), [("not_published",)])
        self.assertIn(
            "current trust",
            json.loads(self.rows("SELECT metadata_json FROM github_actions")[0][0])["publish"]["safe_error"],
        )

    def test_observe_mode_publisher_lane_rechecks_loaded_revocation(self):
        self.assert_configured_publisher_revocation("lane")

    def test_synchronous_repository_cycle_rechecks_loaded_revocation(self):
        self.assert_configured_publisher_revocation("cycle")

    def test_legacy_task_cannot_borrow_authority_from_unrelated_context(self):
        import sqlite3
        from contextlib import closing
        from robert_agent.delivery.application import publish

        self.scan([self.event(body="@robert-bot analyze this", intent="analysis")])
        self.scan([self.event("discussion", "another-owner", "@robert-bot separate suggestion")])
        task, _ = self.finish(
            ["comment:trusted"],
            output_type="comment_analysis",
            planned_github_actions=[
                {
                    "type": "comment",
                    "target_url": "https://github.com/example/backend/issues/123",
                    "body": "accepted explanation",
                }
            ],
        )
        with closing(sqlite3.connect(self.db())) as conn, conn:
            conn.execute(
                "UPDATE tasks SET metadata_json=json_remove(metadata_json,'$.request_event_fingerprint') WHERE task_id=?",
                (task,),
            )
        config = validate_config(self.config_path, skip_external=True)
        config.pop("config_path", None)
        config["repos"][0]["trusted_actors"] = ["another-owner"]
        writes = []

        def runner(command, **kwargs):
            if "-f" in command:
                writes.append(command)
                return SimpleNamespace(returncode=0, stdout='{"id":90}', stderr="")
            return SimpleNamespace(returncode=0, stdout="[]", stderr="")

        result = publish.publish_ready_actions(self.db(), config=config, run_command=runner)
        self.assertEqual(writes, [])
        self.assertEqual(result["published_count"], 0)
