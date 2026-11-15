"""Launch native Worker attempts only across fresh mixed-source boundaries."""

from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch

from robert_agent.adapters.github import pull_requests
from robert_agent.execution import workspace
from robert_agent.work.application import source_observation, tapd_intake

from robert_agent.foundation.values import utc_now
from tests.support.source_github_lifecycle import SourceGitHubLifecycleFixture


class SourceGitHubDispatchTests(SourceGitHubLifecycleFixture):
    def _admit_readonly(self, identifiers=(617,)):
        self._delivered_previous_pr()
        self.labels = ()
        self.tick("idle", ticket=self.ticket)
        self._admit_github_discussion([self._github_discussion(identifier=identifier) for identifier in identifiers])
        followup = self.comment("93")
        version = replace(followup.version, created=utc_now())
        self.comments.append(
            replace(
                followup,
                version=version,
                creator=replace(followup.creator, observation=version),
                own_body=replace(followup.own_body, observation=version),
            )
        )
        return self.tick("admitted", github_discussion_runner=self._reader(identifiers))

    def test_mixed_readonly_prepares_and_dispatches_one_exact_worker(self):
        admitted = self._admit_readonly()
        calls, read = self._current_github_runner()
        before = len(self.started)
        prepared = self.tick("prepared", github_discussion_runner=read)
        self.assertEqual(len(self.started), before)
        [[attempt_id, metadata, route]] = self.read(
            "SELECT a.attempt_id,a.metadata_json,t.route_id FROM attempts a JOIN tasks t USING(task_id) WHERE t.task_id=?",
            (prepared.identity,),
        )
        self.assertEqual(route, "source-evidence")
        self.assertEqual(json.loads(metadata)["native_workspace"]["mode"], "readonly")
        launched = self.tick("dispatched", github_discussion_runner=read)
        self.assertEqual((launched.round_id, launched.identity), (admitted.round_id, attempt_id))
        self.assertEqual(len(self.started), before + 1)
        self.assertEqual(self.started[-1]["task_id"], prepared.identity)
        self.assertEqual(self.started[-1]["attempt_id"], attempt_id)
        self.assertIn("@bot please inspect", self.started[-1]["prompt_path"].read_text())
        self.assertEqual(calls.count("repos/org/alpha/issues/comments/617"), 3)
        self.assertEqual(
            self.read(
                "SELECT DISTINCT e.state FROM source_round_requests r JOIN request_events e USING(request_id) WHERE r.round_id=?",
                (admitted.round_id,),
            ),
            [("pending",)],
        )

    def _reader(self, identifiers, *, altered=None, during_launch=None):
        """Use the supplied alternate only after the per-tick active proof has read every member."""
        reads = {
            identifier: (self._current_github_runner(identifier=identifier)[1], (altered or {}).get(identifier))
            for identifier in identifiers
        }
        selected = identifiers[0]
        items = 0

        def run(command, **options):
            nonlocal selected, items
            endpoint = command[7]
            if "/issues/comments/" in endpoint:
                selected = int(endpoint.rsplit("/", 1)[1])
                items += 1
                if items == len(identifiers) + 1 and during_launch:
                    during_launch()
            original, alternate = reads[selected]
            reader = (alternate or original) if items > len(identifiers) else original
            return reader(command, **options)

        return run

    def _no_launch(self, prepared, count):
        self.assertEqual(len(self.started), count)
        self.assertEqual(
            self.read(
                "SELECT status, json_extract(metadata_json,'$.dispatch') FROM attempts WHERE task_id=?",
                (prepared.identity,),
            ),
            [("prepared", None)],
        )

    def test_dispatch_only_marker_removal_revokes_member_without_starting_worker(self):
        admitted = self._admit_readonly()
        prepared = self.tick("prepared", github_discussion_runner=self._reader((617,)))
        count = len(self.started)
        _, withdrawn = self._current_github_runner(body="No robot mention here")
        self.tick("held", github_discussion_runner=self._reader((617,), altered={617: withdrawn}))
        self._no_launch(prepared, count)
        self.assertEqual(
            self.read("SELECT reason FROM source_round_cancellations WHERE round_id=?", (admitted.round_id,)),
            [("github_current_mention_removed",)],
        )
        self.assertEqual(
            self.read("SELECT state FROM request_events WHERE event_id='event:comment:617'"), [("revoked",)]
        )

    def test_dispatch_only_deleted_member_has_exact_absence_and_remains_unhandled(self):
        admitted = self._admit_readonly()
        prepared = self.tick("prepared", github_discussion_runner=self._reader((617,)))
        count = len(self.started)
        _, deleted = self._current_github_runner(item_status=404)
        self.tick("held", github_discussion_runner=self._reader((617,), altered={617: deleted}))
        self._no_launch(prepared, count)
        self.assertEqual(
            self.read("SELECT reason FROM source_round_cancellations WHERE round_id=?", (admitted.round_id,)),
            [("github_item_absence_confirmed",)],
        )
        self.assertEqual(
            self.read(
                "SELECT state FROM source_round_requests r JOIN request_events e USING(request_id) "
                "WHERE r.round_id=? ORDER BY state",
                (admitted.round_id,),
            ),
            [("pending",), ("revoked",)],
        )

    def test_dispatch_refuses_untrusted_editor(self):
        admitted = self._admit_readonly()
        prepared = self.tick("prepared", github_discussion_runner=self._reader((617,)))
        count = len(self.started)
        _, edited = self._current_github_runner(actor="outsider")
        self.tick("held", github_discussion_runner=self._reader((617,), altered={617: edited}))
        self._no_launch(prepared, count)
        self.assertEqual(
            self.read("SELECT state FROM request_events WHERE event_id='event:comment:617'"), [("revoked",)]
        )
        self.assertTrue(self.read("SELECT 1 FROM source_round_cancellations WHERE round_id=?", (admitted.round_id,)))

    def test_dispatch_positive_edit_preserves_prepared_prompt_until_coordinator_refreezes(self):
        admitted = self._admit_readonly()
        prepared = self.tick("prepared", github_discussion_runner=self._reader((617,)))
        count = len(self.started)
        [[path]] = self.read(
            "SELECT path FROM artifacts WHERE task_id=? AND artifact_type='prompt'", (prepared.identity,)
        )
        before = Path(path).read_bytes()
        _, edited = self._current_github_runner(body="@bot changed at dispatch", edited="2026-11-14T20:02:00Z")
        self.tick("held", github_discussion_runner=self._reader((617,), altered={617: edited}))
        self._no_launch(prepared, count)
        self.assertEqual(Path(path).read_bytes(), before)
        self.assertEqual(
            self.read("SELECT status FROM source_rounds WHERE round_id=?", (admitted.round_id,)), [("active",)]
        )
        refrozen = self.tick("readmitted", github_discussion_runner=edited)
        self.assertNotEqual(refrozen.round_id, admitted.round_id)
        self.assertEqual(Path(path).read_bytes(), before)

    def test_dispatch_rechecks_operation_fence_revision_and_prompt_inside_final_writer(self):
        admitted = self._admit_readonly()
        read = self._reader((617,))
        prepared = self.tick("prepared", github_discussion_runner=read)
        count = len(self.started)
        [[path]] = self.read(
            "SELECT path FROM artifacts WHERE task_id=? AND artifact_type='prompt'", (prepared.identity,)
        )
        prompt = Path(path)
        original = prompt.read_bytes()
        validate = workspace.validate_source_workspace

        for drift in ("operation", "revision", "claim", "prompt"):
            with self.subTest(drift=drift):

                def change(*args, **kwargs):
                    ok = validate(*args, **kwargs)
                    self.assertTrue(ok)
                    if drift == "prompt":
                        prompt.write_bytes(original + b"\nChanged after first writer")
                    else:
                        with closing(sqlite3.connect(self.db, timeout=0)) as conn, conn:
                            if drift == "operation":
                                conn.execute("UPDATE source_github_observations SET operation_id='superseded'")
                            elif drift == "revision":
                                conn.execute(
                                    "UPDATE sources SET metadata_json=json_set(metadata_json,'$.revision','superseded') "
                                    "WHERE source_id=?",
                                    (self.ticket.source_id,),
                                )
                            else:
                                conn.execute(
                                    "UPDATE source_sessions SET token=token+1 WHERE source_id=?",
                                    (self.ticket.source_id,),
                                )
                    return ok

                with patch.object(workspace, "validate_source_workspace", side_effect=change):
                    self.tick("held", github_discussion_runner=self._reader((617,)))
                self._no_launch(prepared, count)
                self.assertEqual(
                    self.read(
                        "SELECT DISTINCT e.state FROM source_round_requests r JOIN request_events e USING(request_id) "
                        "WHERE round_id=?",
                        (admitted.round_id,),
                    ),
                    [("pending",)],
                )
        prompt.write_bytes(original)
        self.tick("dispatched", github_discussion_runner=self._reader((617,)))
        self.assertEqual(len(self.started), count + 1)

    def test_dispatch_only_bad_last_member_of_multiple_batches_holds_without_revoking(self):
        identifiers = tuple(range(617, 623))
        admitted = self._admit_readonly(identifiers)
        prepared = self.tick("prepared", github_discussion_runner=self._reader(identifiers))
        count = len(self.started)
        [(last,)] = self.read(
            "SELECT e.event_id FROM request_events e JOIN ingress_requests i USING(request_id) "
            "JOIN source_round_requests r USING(request_id) WHERE r.round_id=? AND i.provider='github' "
            "ORDER BY r.request_id DESC LIMIT 1",
            (admitted.round_id,),
        )
        identifier = int(last.rsplit(":", 1)[1])
        calls, malformed = self._current_github_runner(identifier=identifier, item_edit=lambda row: row.pop("user"))
        self.tick("held", github_discussion_runner=self._reader(identifiers, altered={identifier: malformed}))
        self._no_launch(prepared, count)
        self.assertIn(f"repos/org/alpha/issues/comments/{identifier}", calls)
        self.assertFalse(self.read("SELECT * FROM source_round_cancellations WHERE round_id=?", (admitted.round_id,)))
        self.assertEqual(
            self.read(
                "SELECT DISTINCT e.state FROM source_round_requests r JOIN request_events e USING(request_id) "
                "WHERE round_id=?",
                (admitted.round_id,),
            ),
            [("pending",)],
        )

    def test_development_classification_prepares_and_dispatches_on_selected_canonical_pr(self):
        self._mixed_pending_fixture()
        _, read = self._current_github_runner()
        admitted = self.tick("admitted", github_discussion_runner=read)
        classified = self.tick("prepared", github_discussion_runner=read)
        self.assertEqual(
            self.read("SELECT route_id FROM tasks WHERE task_id=?", (classified.identity,)),
            [("classification-result",)],
        )
        self.tick("dispatched", github_discussion_runner=read)
        self.submit("classification_result", recommended_route="new-pr")
        self.tick("waiting", github_discussion_runner=read)
        self.tick("audited", github_discussion_runner=read)

        def current_pr(repository, number, **_):
            self.assertEqual((repository, number), ("org/alpha", 17))
            return {
                "number": number,
                "html_url": "https://github.com/org/alpha/pull/17",
                "state": "open",
                "merged": False,
                "merged_at": None,
                "user": {"login": "bot"},
                "head": {"repo": {"full_name": "bot/alpha"}, "ref": self.branch, "sha": self.head},
                "base": {"repo": {"full_name": "org/alpha"}, "ref": "main", "sha": self.base},
            }

        def existing_checkout(_repo, _purpose, *, pr=None):
            self.assertEqual((pr["number"], pr["head_sha"]), (17, self.head))
            return {"ok": True, "worktree_path": str(self.tree), "branch_name": self.branch}

        with (
            patch.object(pull_requests, "read_pr", side_effect=current_pr) as read_pr,
            patch.object(workspace, "prepare_source_worktree", side_effect=existing_checkout),
        ):
            prepared = self.tick("prepared", github_discussion_runner=read)
            [[raw]] = self.read("SELECT metadata_json FROM attempts WHERE task_id=?", (prepared.identity,))
            resolved = json.loads(raw)["native_route_resolution"]
            self.assertEqual(
                (resolved["recommended_route"], resolved["selected_route"]), ("new-pr", "update-existing-pr")
            )
            launched = self.tick("dispatched", github_discussion_runner=read)
            self.assertEqual(launched.round_id, admitted.round_id)
            self.assertEqual(self.started[-1]["task_id"], prepared.identity)
            self.assertEqual(read_pr.call_count, 2)
        self.assertEqual(
            self.read(
                "SELECT DISTINCT e.state FROM source_round_requests r JOIN request_events e USING(request_id) WHERE round_id=?",
                (admitted.round_id,),
            ),
            [("pending",)],
        )

    def test_current_active_round_operation_cannot_replace_attempt_bound_proof(self):
        admitted = self._admit_readonly()
        read = self._reader((617,))
        make_proof = tapd_intake.refresh_mixed_round

        prepared = self.tick("prepared", github_discussion_runner=read)
        # Capture a genuinely current coordinator proof from the final prepare writer.
        captured = []

        def record_proof(*args, **kwargs):
            checked = make_proof(*args, **kwargs)
            captured.append(checked.worker_proof)
            return checked

        factory = source_observation.make_source_refresh

        def substitute_operation(*args, **kwargs):
            genuine = factory(*args, **kwargs)

            def refresh(ticket, *, task_id=None, attempt_id=None):
                observation = genuine(ticket)
                if task_id is None:
                    return observation
                self.assertIsNotNone(captured[-1])
                self.assertIsNone(captured[-1].target)
                return replace(observation, mixed=captured[-1])

            return refresh

        count = len(self.started)
        with (
            patch.object(tapd_intake, "refresh_mixed_round", side_effect=record_proof),
            patch.object(source_observation, "make_source_refresh", side_effect=substitute_operation),
        ):
            step = self.tick("held", github_discussion_runner=self._reader((617,)))
        self.assertEqual((step.round_id, step.reason), (admitted.round_id, "native_authorization_unavailable"))
        self._no_launch(prepared, count)
