"""Equivalent authorized PR triggers share execution and delivery evidence."""

from collections import Counter
import hashlib
import json
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from tests.support import database as fixture_database
from tests.support.review_coordinator import ReviewCoordinatorFixture
from robert_agent.delivery.application import publish
from robert_agent.persistence.rounds import review as store


class CanonicalExecutionTests(ReviewCoordinatorFixture):
    def pr(self, repo, number):
        return super().pr(repo, number) | {
            "title": "Review example",
            "body": "Acceptance: review all changes.",
        }

    def equivalent(self, identifier="first", *, request=False, **changes):
        event = self.event(identifier) | {
            "head_sha": self.git_fixture.head,
            "base_sha": self.git_fixture.base,
            "base_ref": "main",
            "source_body": "Acceptance: review all changes.",
        }
        if request:
            event.update(
                event_type="review_request", body="", requester_login="maintainer", requested_reviewer="robert-bot"
            )
        return event | changes

    def publish_report(self, driver):
        self.cycle([])  # Persist the existing audit before invoking publication.

        def api(command, **kwargs):
            if "--paginate" in command:
                value = [[]]
            elif "--method" not in command:
                value = self.pr("", 42)
            else:
                value = {
                    "id": 100,
                    "commit_id": driver.manifest["head_sha"],
                    "state": "COMMENTED",
                    "body": next(arg[5:] for arg in command if arg.startswith("body=")),
                    "html_url": "https://github.com/example/backend/pull/42#pullrequestreview-100",
                }
            return SimpleNamespace(returncode=0, stdout=json.dumps(value), stderr="")

        result = publish.publish_ready_actions(self.db, run_command=api)
        self.assertEqual(result["published_count"], 1, result)
        self.cycle([], skip_publish=False)

    def associations(self, driver):
        with fixture_database.connect(self.db) as conn:
            return store.trigger_associations(conn, driver.round_id)

    def test_comment_and_review_request_one_owner_one_report_each_trigger_completed(self):
        self.cycle([self.equivalent(), self.equivalent("requested", request=True)])
        driver = self.coordinator()
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_sessions").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)
        driver.run()
        self.publish_report(driver)
        self.assertEqual(
            Counter(call[:2] for call in self.calls),
            {("review", "codex"): 1, ("review", "claude"): 1, ("synthesis", "codex"): 1},
        )
        triggers = self.associations(driver)
        self.assertEqual(
            {item["trigger_fingerprint"] for item in triggers}, {"comment:first", "review_request:requested"}
        )
        self.assertEqual({item["status"] for item in triggers}, {"completed"})
        self.assertEqual(len({item["completion"]["action_id"] for item in triggers}), 1)
        self.assertEqual({item["task_id"] for item in triggers}, {driver.task_id})

    def test_late_trigger_after_result_and_after_publication_reuses_same_report(self):
        self.cycle([self.equivalent()])
        driver = self.coordinator()
        report = driver.run()
        calls = len(self.calls)
        self.cycle([self.equivalent("late", request=True)])
        self.publish_report(driver)
        self.cycle([self.equivalent("completed", request=True)])
        self.cycle([self.equivalent("completed", request=True)])
        self.assertEqual(len(self.calls), calls)
        self.assertEqual({item["status"] for item in self.associations(driver)}, {"completed"})
        self.assertEqual(len(self.associations(driver)), 3)
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM github_actions").fetchone()[0], 1)
            self.assertEqual(store.session(conn, round_id=driver.round_id)["report_path"], str(report))

    def test_delivered_receipts_follow_final_publication_without_new_calls_for_adoption(self):
        self.cycle([self.equivalent(), self.equivalent("requested", request=True)])
        driver = self.coordinator()
        driver.run()

        def receipts():
            with fixture_database.connect(self.db) as conn:
                return {
                    call_id: json.loads(encoded)
                    for call_id, encoded in conn.execute(
                        "SELECT call_id,usage_json FROM model_usage WHERE kind='stage' AND scope_id=?",
                        (driver.task_id,),
                    )
                }

        before = receipts()
        self.assertEqual(len(before), 3)
        self.assertFalse(
            any(
                "delivered" in segment.get("events", {})
                for value in before.values()
                for segment in value["segments"].values()
            )
        )
        with fixture_database.connect(self.db) as conn:
            adopted = next(run for run in store.runs(conn, driver.round_id) if run["stage"] == "review")
            receipt = {
                "run_id": adopted["run_id"],
                "prompt_sha256": adopted["prompt_sha256"],
                "original_status": "failed",
                "original_error": "exit after sealed output",
                "artifact_sha256": hashlib.sha256(json.dumps(adopted["artifact"], sort_keys=True).encode()).hexdigest(),
            }
            store.update_run(
                conn,
                adopted["run_id"],
                status="failed",
                error=receipt["original_error"],
                dispatch_json=json.dumps({**adopted["dispatch"], "artifact_adoption": receipt}),
            )
        self.cycle([])
        self.assertEqual(receipts(), before)  # Audit and report registration do not deliver.
        self.publish_report(driver)
        after = receipts()
        self.assertEqual(set(after), set(before))
        for call_id, value in after.items():
            self.assertEqual(set(value["segments"]), set(before[call_id]["segments"]))
            for segment_id, segment in value["segments"].items():
                self.assertIn("delivered", segment["events"])
                self.assertEqual(segment.get("usage"), before[call_id]["segments"][segment_id].get("usage"))
                self.assertEqual(segment["launched"], before[call_id]["segments"][segment_id]["launched"])
        self.cycle([self.equivalent("late", request=True)])
        self.cycle([])
        self.assertEqual(receipts(), after)
        self.assertEqual(len(self.calls), 3)

    def test_additional_intent_materials_and_independent_rereview_are_separate(self):
        self.cycle(
            [
                self.equivalent(),
                self.equivalent("focus", body="@robert-bot review this PR, focus on security"),
                self.equivalent("materials", materials=["requirements-v2"]),
                self.equivalent("independent", independent_review=True),
                self.equivalent("repeat", body="@robert-bot review this PR again"),
                self.equivalent("revision", head_sha="b" * 40),
            ]
        )
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_sessions").fetchone()[0], 6)

    def test_unknown_revision_and_untrusted_requests_never_attach(self):
        self.cycle([self.equivalent(), self.event("unknown"), self.equivalent("untrusted", actor_login="stranger")])
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_sessions").fetchone()[0], 2)
        self.assertEqual(len(self.associations(self.coordinator())), 1)

    def test_owner_recovery_keeps_attached_trigger_and_cached_sibling(self):
        self.cycle([self.equivalent()])
        driver = self.coordinator()
        original = driver.launcher

        def unavailable(**kwargs):
            if kwargs["worker_agent_config"]["name"] == "claude":
                return {"ok": False, "status": "failed_dispatch", "safe_error": "unavailable"}
            return original(**kwargs)

        driver.launcher = unavailable
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            driver.run()
        self.cycle([self.equivalent("late", request=True)])
        self.coordinator().run()
        self.assertEqual(Counter(call[:2] for call in self.calls)[("review", "codex")], 1)
        self.assertEqual(len(self.associations(driver)), 2)
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)

    def test_original_report_tamper_prevents_completed_reuse(self):
        self.cycle([self.equivalent()])
        driver = self.coordinator()
        report = driver.run()
        self.publish_report(driver)
        report.write_text("tampered")
        self.cycle([self.equivalent("late", request=True)])
        trigger = next(
            item for item in self.associations(driver) if item["trigger_fingerprint"] == "review_request:late"
        )
        self.assertNotEqual(trigger["status"], "completed")

    def test_arrival_during_review_does_not_change_frozen_input(self):
        self.cycle([self.equivalent()])
        driver = self.coordinator()
        driver.prepare()
        frozen = dict(driver.manifest)
        original = driver.launcher
        arrived = Event()

        def arrival(**kwargs):
            if kwargs["worker_agent_config"]["name"] == "codex" and not arrived.is_set():
                arrived.set()
                self.cycle([self.equivalent("running", request=True)])
            return original(**kwargs)

        driver.launcher = arrival
        driver.run()
        self.assertEqual(driver.manifest, frozen)
        self.assertEqual(len(self.associations(driver)), 2)

    def test_changed_reviewer_plan_creates_independent_execution(self):
        self.cycle([self.equivalent()])
        config = yaml.safe_load(self.config_path.read_text())
        config["workers"]["claude"]["model"] = "changed-model"
        self.config_path.write_text(yaml.safe_dump(config))
        self.cycle([self.equivalent("different-plan", request=True)])
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM review_sessions").fetchone()[0], 2)

    def test_changed_base_or_acceptance_is_independent(self):
        self.cycle(
            [
                self.equivalent(),
                self.equivalent("base", base_sha="c" * 40),
                self.equivalent("acceptance", source_body="Different acceptance"),
            ]
        )
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM review_sessions").fetchone()[0], 3)

    def test_changed_live_acceptance_is_rejected_before_snapshot(self):
        self.cycle([self.equivalent()])
        from robert_agent.workflows.review import coordinator

        for field in ("title", "body"):
            with self.subTest(field=field):
                driver = self.coordinator()
                driver.fetch_pr = lambda repo, number, field=field: (
                    self.pr(repo, number) | {field: "Changed acceptance"}
                )
                with patch.object(
                    coordinator.review_workspace,
                    "prepare_snapshot",
                    wraps=coordinator.review_workspace.prepare_snapshot,
                ) as snapshot:
                    with self.assertRaisesRegex(coordinator.StageSuperseded, "acceptance"):
                        driver.prepare()
                    snapshot.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_acceptance_change_during_context_preparation_is_rejected(self):
        self.cycle([self.equivalent()])
        driver = self.coordinator()
        from robert_agent.workflows.review import coordinator

        original = driver.prepare_context

        def prepare_then_change():
            original()
            driver.fetch_pr = lambda repo, number: self.pr(repo, number) | {"body": "Changed acceptance"}

        with patch.object(driver, "prepare_context", side_effect=prepare_then_change):
            with self.assertRaisesRegex(coordinator.StageSuperseded, "acceptance"):
                driver.prepare()
        self.assertEqual(self.calls, [])

    def test_cached_snapshot_acceptance_must_match_canonical_request(self):
        self.cycle([self.equivalent()])
        driver = self.coordinator()
        driver.prepare()
        from robert_agent.workflows.review import coordinator

        # A recovered bundle from the previous drift bug must not be reused,
        # even if its byte hashes verify and the current PR matches admission.
        driver.request["canonical_identity"]["acceptance"]["source_body"] = "Changed acceptance"
        driver.fetch_pr = lambda repo, number: self.pr(repo, number) | {"body": "Changed acceptance"}
        with self.assertRaisesRegex(coordinator.StageSuperseded, "acceptance"):
            driver.prepare()
        self.assertEqual(self.calls, [])

    def test_owner_rebind_keeps_trigger_authority_and_completion_binding_current(self):
        self.cycle([self.equivalent(), self.equivalent("second", request=True)])
        driver = self.coordinator()
        with fixture_database.connect(self.db) as conn:
            conn.execute("UPDATE tasks SET lifecycle='completed' WHERE task_id=?", (driver.task_id,))
            conn.execute(
                "INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) SELECT 'recovered-owner',workstream_id,'queued',created_at,updated_at FROM tasks WHERE task_id=?",
                (driver.task_id,),
            )
            store.update(conn, driver.round_id, task_id="recovered-owner")
            store.bind_triggers(conn, driver.round_id)
            self.assertEqual(
                {item["task_id"] for item in store.trigger_associations(conn, driver.round_id)}, {"recovered-owner"}
            )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM task_events WHERE task_id='recovered-owner'").fetchone()[0], 2
            )

    def test_report_registration_retry_reuses_render_and_models(self):
        self.cycle([self.equivalent()])
        driver = self.coordinator()
        from robert_agent.workflows.review import coordinator

        with patch.object(
            coordinator.db_write_artifacts,
            "insert_for_synthesize",
            side_effect=RuntimeError("registration interrupted"),
        ):
            with self.assertRaisesRegex(RuntimeError, "registration interrupted"):
                driver.run()
        calls = len(self.calls)
        with patch.object(coordinator.review_files, "render_merged", side_effect=AssertionError("render repeated")):
            report = self.coordinator().run()
        self.assertTrue(report.is_file())
        self.assertEqual(len(self.calls), calls)
