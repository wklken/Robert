from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent.adapters.sqlite import database as storage
from robert_agent.supervision import store as supervision_store


NOW = "2026-11-06T10:00:00+00:00"
LATER = "2026-11-06T10:01:00+00:00"


def observation(**changes):
    return {
        "scope_type": "task",
        "scope_id": "task-one",
        "rule_id": "result_not_consumed",
        "lineage_key": "request-one",
        "classification": "confirmed",
        "reason_code": "saved_result_without_audit",
        "severity": "warning",
        "evidence": {"result_id": "result-one"},
        "expected_progress": {"audit_result": "result-one"},
        "next_check_at": LATER,
        "progress_token": "spool:result-one",
        **changes,
    }


class SupervisionStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / "state.db"
        storage.init_database(self.db)
        self.conn = sqlite3.connect(self.db)
        self.addCleanup(self.conn.close)

    def apply(self, *observations, now=NOW):
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            return supervision_store.apply_observations(self.conn, observations, now)

    def test_confirmed_observations_deduplicate_across_restarts_and_reason_changes(
        self,
    ):
        self.assertEqual(self.apply(observation(classification="suspect")), [])
        (opened,) = self.apply(observation())
        self.assertEqual(opened["transition"], "opened")
        identifier = opened["incident"]["incident_id"]
        with closing(sqlite3.connect(self.db)) as reopened, reopened:
            reopened.execute("BEGIN IMMEDIATE")
            self.assertEqual(
                supervision_store.apply_observations(
                    reopened,
                    [
                        observation(
                            reason_code="audit_consumer_not_running",
                            evidence={"result_id": "result-one", "lane": "local"},
                        ),
                    ],
                    LATER,
                ),
                [],
            )
        (item,) = supervision_store.list_incidents(self.conn, status="active")
        self.assertEqual(item["incident_id"], identifier)
        self.assertEqual(item["first_observed_at"], NOW)
        self.assertEqual(item["last_observed_at"], LATER)
        self.assertEqual(item["reason_code"], "audit_consumer_not_running")
        self.assertEqual(item["evidence"], {"result_id": "result-one", "lane": "local"})

    def test_resolution_requires_explicit_positive_evidence_for_the_same_lineage(self):
        (opened,) = self.apply(observation())
        for status in ("healthy", "expected_wait", "suspect", "unobservable"):
            with self.subTest(status=status):
                self.assertEqual(self.apply(observation(classification=status), now=LATER), [])
                self.assertEqual(len(supervision_store.list_incidents(self.conn, status="active")), 1)
        self.assertEqual(
            self.apply(
                observation(
                    classification="healthy",
                    lineage_key="request-two",
                    positive_evidence={"audit": "accepted", "occurred_at": LATER},
                ),
                now=LATER,
            ),
            [],
        )
        self.assertEqual(
            self.apply(
                observation(
                    classification="healthy",
                    positive_evidence={"audit": "accepted"},
                ),
                now=LATER,
            ),
            [],
        )
        self.assertEqual(self.apply(now=LATER), [])
        self.assertEqual(len(supervision_store.list_incidents(self.conn, status="active")), 1)
        (resolved,) = self.apply(
            observation(
                classification="healthy",
                positive_evidence={
                    "audit": "accepted",
                    "result_id": "result-one",
                    "occurred_at": LATER,
                },
            ),
            now=LATER,
        )
        self.assertEqual(resolved["transition"], "resolved")
        self.assertEqual(resolved["incident"]["incident_id"], opened["incident"]["incident_id"])
        self.assertEqual(
            resolved["incident"]["resolution_evidence"],
            {"audit": "accepted", "result_id": "result-one", "occurred_at": LATER},
        )
        self.assertEqual(resolved["incident"]["resolved_by"], "natural")
        self.assertEqual(supervision_store.list_incidents(self.conn, status="active"), [])
        (recurrence,) = self.apply(observation(), now=LATER)
        self.assertNotEqual(recurrence["incident"]["incident_id"], opened["incident"]["incident_id"])
        self.assertEqual(
            recurrence["incident"]["prior_incident_id"],
            opened["incident"]["incident_id"],
        )

    def test_observation_updates_checkpoint_without_overwriting_metadata(self):
        key = ("task", "task-one", "result_not_consumed")
        with self.conn:
            supervision_store.update_checkpoint(self.conn, *key, now=NOW, metadata={"cursor": "first"})
        self.apply(observation())
        checkpoint = supervision_store.get_checkpoint(self.conn, *key)
        self.assertEqual(checkpoint["metadata"], {"cursor": "first"})
        self.assertEqual(checkpoint["last_progress_at"], NOW)
        self.apply(observation(evidence={"spool_bytes": 20}), now=LATER)
        self.assertEqual(supervision_store.get_checkpoint(self.conn, *key)["last_progress_at"], NOW)
        self.apply(observation(progress_token="audited:result-one"), now=LATER)
        self.assertEqual(supervision_store.get_checkpoint(self.conn, *key)["last_progress_at"], LATER)

    def test_wait_resets_eligibility_but_heartbeat_does_not_reset_progress(self):
        self.apply(observation(eligible_since=NOW, progress_observed_at=NOW))
        self.apply(
            observation(eligible_since=None, classification="expected_wait", progress_token=None),
            now=LATER,
        )
        checkpoint = supervision_store.get_checkpoint(self.conn, "task", "task-one", "result_not_consumed")
        self.assertIsNone(checkpoint["eligible_since"])
        self.assertEqual(checkpoint["last_progress_at"], NOW)
        self.apply(observation(eligible_since=LATER), now=LATER)
        checkpoint = supervision_store.get_checkpoint(self.conn, "task", "task-one", "result_not_consumed")
        self.assertEqual(checkpoint["eligible_since"], LATER)
        self.assertEqual(checkpoint["last_progress_at"], NOW)

    def test_upgrade_preserves_business_state_and_supervision_history(self):
        self.conn.execute(
            "INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) VALUES ('repo','example/r','bot','main','/repo','/trees')"
        )
        self.conn.execute(
            "INSERT INTO workstreams(workstream_id,repo_id,lifecycle,created_at,updated_at) VALUES ('workstream','repo','active',?,?)",
            (NOW, NOW),
        )
        self.conn.execute(
            "INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) VALUES ('task-one','workstream','queued',?,?)",
            (NOW, NOW),
        )
        self.conn.execute("DROP TABLE supervision_recovery_actions")
        self.conn.execute("DROP TABLE supervision_incidents")
        self.conn.execute("DROP TABLE supervision_checkpoints")
        self.conn.execute("DELETE FROM schema_migrations WHERE name='robert-supervision-v1'")
        self.conn.commit()
        self.assertFalse(supervision_store.has_schema(self.conn))
        storage.init_database(self.db)
        self.assertTrue(supervision_store.has_schema(self.conn))
        self.apply(observation())
        storage.init_database(self.db)
        self.assertEqual(
            self.conn.execute("SELECT lifecycle,updated_at FROM tasks WHERE task_id='task-one'").fetchone(),
            ("queued", NOW),
        )
        self.assertEqual(len(supervision_store.list_incidents(self.conn)), 1)


if __name__ == "__main__":
    unittest.main()
