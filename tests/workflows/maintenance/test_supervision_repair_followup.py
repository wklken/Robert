from contextlib import closing
from datetime import timedelta
import json
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

from tests.support import supervision_repair as fixtures
from robert_agent.workflows.maintenance import repair as supervision_repair
from robert_agent.supervision import store as supervision_store


class RepairFollowupTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RepairFixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.config = self.fixture.config
        self.incident = self.fixture.incident_id
        self.now = self.fixture.when
        self.db = self.fixture.db
        self.checkout = self.fixture.worktree_root / "delivery"
        self.checkout.mkdir()

    def delivery(self, revision):
        self.fixture.auto_policy()
        with patch.object(
            supervision_repair,
            "probe_restricted_launcher",
            return_value={"ok": True, "status": "verified"},
        ):
            item = supervision_repair.propose(self.config, self.incident, now=self.now)
        task = item["task_id"]
        self.assertTrue(task, item)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "INSERT INTO attempts(attempt_id,task_id,attempt_no,status,worktree_path) VALUES('delivery',?,1,'completed',?)",
                (task, str(self.checkout)),
            )
            conn.execute(
                """INSERT INTO worker_results(result_id,task_id,attempt_id,output_type,created_at,metadata_json)
                VALUES('result',?,'delivery','new_pr',?,?)""",
                (
                    task,
                    self.now.isoformat(),
                    json.dumps(
                        {
                            "audit": {
                                "status": "accepted",
                                "action_scope": {"result_head_sha": revision},
                            },
                        }
                    ),
                ),
            )

    def test_waits_for_audited_delivery_before_claim(self):
        from robert_agent.workflows.maintenance import (
            followup as supervision_repair_followup,
        )

        supervision_repair.propose(self.config, self.incident, now=self.now)
        with patch.object(supervision_repair, "verify", side_effect=AssertionError("no delivery")):
            self.assertEqual(
                supervision_repair_followup.poll(self.config, now=self.now)["status"],
                "idle",
            )
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertIsNone(supervision_store.get_checkpoint(conn, "repair", self.incident, "followup"))

    def test_claim_is_durable_before_io_and_complete_stops_polling(self):
        from robert_agent.workflows.maintenance import (
            followup as supervision_repair_followup,
        )

        self.delivery("a" * 40)
        visits = []

        def verify(config, incident_id, *, checkout, runtime, now, expected_delivery):
            self.assertEqual((incident_id, checkout), (self.incident, self.checkout.resolve()))
            self.assertEqual(expected_delivery, ("result", "a" * 40))
            with closing(sqlite3.connect(self.db)) as conn:
                state = supervision_store.get_checkpoint(conn, "repair", self.incident, "followup")["metadata"]
            self.assertEqual(state["status"], "checking")
            self.assertTrue(state["claim_id"])
            self.assertGreaterEqual(state["next_check_at"], (now + timedelta(minutes=5)).isoformat())
            self.assertEqual(supervision_repair_followup.poll(config, now=now)["status"], "busy")
            visits.append(now)
            return {"status": "verification_pending" if len(visits) == 1 else "verified"}

        with patch.object(supervision_repair, "verify", side_effect=verify):
            self.assertEqual(
                supervision_repair_followup.poll(self.config, now=self.now)["status"],
                "verification_pending",
            )
            self.assertEqual(
                supervision_repair_followup.poll(self.config, now=self.now + timedelta(minutes=4))["status"],
                "idle",
            )
            self.assertEqual(
                supervision_repair_followup.poll(self.config, now=self.now + timedelta(minutes=5))["status"],
                "verified",
            )
            self.assertEqual(
                supervision_repair_followup.poll(self.config, now=self.now + timedelta(days=1))["status"],
                "idle",
            )
        self.assertEqual(len(visits), 2)

    def test_verified_code_reuses_only_matching_policy_and_clean_revision(self):
        def git(*args):
            return subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "-c",
                    "commit.gpgsign=false",
                    *args,
                ],
                cwd=self.checkout,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

        git("init", "-q")
        git("commit", "--allow-empty", "-qm", "repair")
        self.delivery(git("rev-parse", "HEAD"))
        with patch.object(supervision_repair, "probe_restricted_launcher", return_value={"ok": True}) as probe:
            first = supervision_repair.verify(self.config, self.incident, checkout=self.checkout, now=self.now)
            self.assertEqual(first["verification"]["code_verified"]["status"], "verified")
            second = supervision_repair.verify(self.config, self.incident, checkout=self.checkout, now=self.now)
            self.assertEqual(
                second["verification"]["code_verified"],
                first["verification"]["code_verified"],
            )
            self.assertEqual(probe.call_count, 1)
            self.config["supervision"]["repair"]["verification_commands"] = [[sys.executable, "-c", "assert True"]]
            changed = supervision_repair.verify(self.config, self.incident, checkout=self.checkout, now=self.now)
            self.assertEqual(changed["verification"]["code_verified"]["status"], "verified")
            self.assertEqual(probe.call_count, 2)
            (self.checkout / "dirty").write_text("unexpected")
            dirty = supervision_repair.verify(self.config, self.incident, checkout=self.checkout, now=self.now)
            self.assertEqual(dirty["verification"]["code_verified"]["status"], "unobservable")
            self.assertEqual(probe.call_count, 2)

    def test_mainline_checks_immutable_remote_shas_when_local_history_is_absent(self):
        revision, merged, tip = "a" * 40, "b" * 40, "c" * 40
        delivery = {
            "revision": revision,
            "pr_url": "https://github.com/acme/robert/pull/42",
        }
        policy = {"repository": "acme/robert"}
        for comparison_status, expected in [
            ("ahead", "verified"),
            ("diverged", "unobservable"),
        ]:
            with self.subTest(comparison=comparison_status):

                def remote(path, _jq):
                    if path.endswith("/pulls/42"):
                        return {
                            "merged": True,
                            "head_sha": revision,
                            "merge_commit_sha": merged,
                            "base_ref": "main",
                            "repository": "acme/robert",
                        }
                    if path.endswith("/git/ref/heads/main"):
                        return {"sha": tip}
                    self.assertEqual(path, f"repos/acme/robert/compare/{merged}...{tip}?per_page=1")
                    return {
                        "status": comparison_status,
                        "base_sha": merged,
                        "merge_base_sha": merged,
                    }

                with patch.object(supervision_repair, "_github", side_effect=remote):
                    self.assertEqual(
                        supervision_repair._merged_on_target(delivery, policy, self.fixture.repo, "main")["status"],
                        expected,
                    )


if __name__ == "__main__":
    unittest.main()
