from contextlib import closing
from datetime import timedelta
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from unittest.mock import patch

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent.execution import runtime_state as daemon_state
from robert_agent.adapters.processes import identity as worker_identity
from robert_agent.context import project
from tests.support.supervision_repair import RepairFixture


class RepairTests(RepairFixture):
    def test_backlog_is_system_owned_and_idempotent_without_starting(self):
        first = self.repair.propose(self.config, self.incident_id, now=self.when)
        again = self.repair.propose(self.config, self.incident_id, now=self.when)
        self.assertEqual(first["status"], "backlog")
        self.assertEqual(first["work_item_id"], again["work_item_id"])
        with closing(sqlite3.connect(self.db)) as conn:
            item = conn.execute(
                "SELECT created_by, activated_at, metadata_json, description FROM work_items"
            ).fetchone()
            self.assertEqual(item[:2], ("system:supervisor", None))
            self.assertEqual(
                json.loads(item[2])["supervision_repair"]["incident_id"],
                self.incident_id,
            )
            self.assertIn("Known regression", item[3])
            self.assertIn("The matching consumer records a receipt", item[3])
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 0)
        stages = self.repair.status(self.config, self.incident_id)["verification"]
        self.assertEqual(
            set(stages),
            {
                "code_verified",
                "mainline_verified",
                "deployed_verified",
                "original_progress",
            },
        )
        self.assertTrue(all(stage["status"] == "pending" for stage in stages.values()))

    def test_incomplete_permissions_or_real_unsandboxed_wrapper_stays_backlog(self):
        self.auto_policy(["open_pr"])
        one = self.repair.propose(self.config, self.incident_id, now=self.when)
        self.assertEqual(one["status"], "backlog")
        self.assertEqual(one["auto_status"], "maintenance_policy_incomplete")
        another = self.incident("controller", "publish")
        self.auto_policy()
        with (
            tempfile.TemporaryDirectory() as controller_home,
            patch.dict(
                os.environ,
                {
                    "HOME": controller_home,
                    "GH_CONFIG_DIR": "",
                    "XDG_CONFIG_HOME": "",
                },
            ),
        ):
            two = self.repair.propose(self.config, another, now=self.when)
        self.assertEqual(two["status"], "backlog")
        self.assertEqual(two["auto_status"], "restricted_launcher_unverified")
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 0)

    def test_auto_requires_fixed_route_current_policy_and_rejects_recursion(self):
        policy = self.auto_policy()
        with patch.object(
            self.repair,
            "probe_restricted_launcher",
            return_value={"ok": True, "status": "verified"},
        ):
            created = self.repair.propose(self.config, self.incident_id, now=self.when)
        self.assertEqual(created["status"], "activated")
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(
                self.repair.preparation_route(conn, created["task_id"], self.config, self.repo),
                "new-pr",
            )
            self.assertIsNotNone(self.repair.authorized_repair(conn, created["task_id"], self.config, "r", "open_pr"))
            self.assertIsNone(self.repair.authorized_repair(conn, created["task_id"], self.config, "r", "comment"))
            policy["allowed_actions"] = ["open_pr"]
            self.assertIsNone(self.repair.authorized_repair(conn, created["task_id"], self.config, "r", "open_pr"))
        repair_failure = self.incident("task", created["task_id"])
        rejected = self.repair.propose(self.config, repair_failure, now=self.when)
        self.assertEqual(rejected["status"], "repair_recursion_blocked")
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM work_items").fetchone()[0], 1)

    def test_verification_separates_real_checks_merge_deployment_and_original_progress(
        self,
    ):
        self.auto_policy()
        with patch.object(
            self.repair,
            "probe_restricted_launcher",
            return_value={"ok": True, "status": "verified"},
        ):
            created = self.repair.propose(self.config, self.incident_id, now=self.when)
        self.assertEqual(created["status"], "activated")

        def git(*args):
            return subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Robert Test",
                    "-c",
                    "user.email=robert@example.invalid",
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
        (self.checkout / "evidence.txt").write_text("baseline\n")
        (self.checkout / ".gitignore").write_text("override.py\n")
        git("add", ".")
        git("commit", "-qm", "baseline")
        old_revision = git("rev-parse", "HEAD")
        (self.checkout / "evidence.txt").write_text("fixed\n")
        git("commit", "-qam", "repair")
        revision = git("rev-parse", "HEAD")
        git("commit", "-qm", "merge", "--allow-empty")
        merged = git("rev-parse", "HEAD")
        checkout = self.worktree_root / "verify"
        git("clone", "--no-local", "--", str(self.checkout), str(checkout))
        subprocess.run(
            ["git", "switch", "--detach", revision],
            cwd=checkout,
            check=True,
            capture_output=True,
        )
        (checkout / "override.py").write_text("forged_verification = True\n")
        # Use a real approved command. Its success depends on the pinned checkout.
        self.config["supervision"]["repair"]["verification_commands"] = [
            [
                sys.executable,
                "-c",
                'from pathlib import Path; assert Path("evidence.txt").read_text() == "fixed\\n"; '
                'assert not Path("override.py").exists()',
            ]
        ]
        now = self.when.isoformat()
        task_id = created["task_id"]
        pr_url = "https://github.com/acme/robert/pull/42"
        action_scope = {"result_head_sha": revision}
        with closing(sqlite3.connect(self.db)) as conn, conn:
            (repo_id,) = conn.execute(
                "SELECT repo_id FROM tasks JOIN workstreams USING (workstream_id) WHERE task_id=?",
                (task_id,),
            ).fetchone()
            conn.execute(
                "UPDATE tasks SET lifecycle='completed',route_id='new-pr',expected_output='new_pr' WHERE task_id=?",
                (task_id,),
            )
            conn.execute(
                "INSERT INTO attempts(attempt_id,task_id,attempt_no,status) VALUES ('attempt',?,1,'completed')",
                (task_id,),
            )
            conn.execute(
                """INSERT INTO worker_results(result_id,task_id,attempt_id,output_type,created_at,metadata_json)
                VALUES ('result',?,'attempt','new_pr',?,?)""",
                (
                    task_id,
                    now,
                    json.dumps(
                        {
                            "audit": {
                                "status": "accepted",
                                "action_scope": action_scope,
                            }
                        }
                    ),
                ),
            )
            for action, external_id in (("push_existing_pr", "repair"), ("open_pr", "42")):
                receipt = {
                    "schema_version": 1,
                    "action_id": action,
                    "action_type": action,
                    "result_id": "result",
                    "task_id": task_id,
                    "scope": action_scope,
                    "external_id": external_id,
                    "result_head_sha": revision,
                    "target_url": pr_url,
                    "confirmation_method": "publisher_acknowledgement",
                    "confirmed_at": now,
                }
                conn.execute(
                    """INSERT INTO github_actions(action_id,result_id,task_id,action_type,audit_status,publish_status,
                    created_at,external_id,target_url,metadata_json)
                    VALUES (?,'result',?,?,'accepted','published',?,?,?,?)""",
                    (
                        action,
                        task_id,
                        action,
                        now,
                        external_id,
                        pr_url,
                        json.dumps({"publish": {"receipt": receipt}}),
                    ),
                )
                conn.execute(
                    """INSERT INTO publication_intents(publication_id,action_id,idempotency_key,request_fingerprint,
                    status,created_at,updated_at,remote_evidence_json)
                    VALUES (?,?,?,?,'published',?,?,?)""",
                    (
                        "intent-" + action,
                        action,
                        "key-" + action,
                        "request-" + action,
                        now,
                        now,
                        json.dumps({"status": "confirmed_effect", "receipt": receipt}),
                    ),
                )
            daemon_state.start_daemon_run(conn, config_path=self.config_file, owner_id="daemon", now=now)
            daemon_state.acquire_daemon_lease(conn, resource_key="fixture", owner_id="daemon", ttl_seconds=60, now=now)

        def provenance():
            with closing(sqlite3.connect(self.db)) as conn:
                return project.memory_provenance(conn, repo_id, task_id, "result")

        self.assertEqual(provenance()["stage"], "publication_confirmed")

        def remote(path, jq):
            if path.endswith("/pulls/42"):
                return {
                    "merged": True,
                    "merge_commit_sha": merged,
                    "head_sha": revision,
                    "base_ref": "main",
                    "repository": "acme/robert",
                }
            self.assertTrue(path.endswith("/git/ref/heads/main"))
            return {"sha": merged}

        identity = worker_identity.capture_process_identity(os.getpid())
        runtime = {
            "build_status": "verified",
            "build_id": "fixture-build",
            "run_id": "daemon",
            "started_at": now,
            "process_identity": identity,
            "pid": identity["pid"],
            "source_revision": old_revision,
        }
        with (
            patch.object(self.repair, "probe_restricted_launcher", return_value={"ok": True}),
            patch.object(self.repair, "_github", side_effect=remote),
        ):
            old = self.repair.verify(
                self.config,
                self.incident_id,
                checkout=checkout,
                runtime=runtime,
                now=self.when,
            )
            self.assertEqual(old["verification"]["code_verified"]["status"], "verified")
            self.assertEqual(old["verification"]["mainline_verified"]["status"], "verified")
            self.assertEqual(old["verification"]["deployed_verified"]["status"], "pending")
            self.assertEqual(old["verification"]["original_progress"]["status"], "pending")
            self.assertEqual(provenance()["stage"], "publication_confirmed")
            runtime["source_revision"] = merged
            deployed = self.repair.verify(
                self.config,
                self.incident_id,
                checkout=checkout,
                runtime=runtime,
                now=self.when,
            )
            self.assertEqual(deployed["verification"]["deployed_verified"]["status"], "verified")
            self.assertEqual(deployed["verification"]["original_progress"]["status"], "pending")
            deployed_memory = provenance()
            self.assertEqual(deployed_memory["stage"], "deployment_confirmed")
            self.assertEqual(deployed_memory["deployment"]["incident_id"], self.incident_id)
            self.assertEqual(deployed_memory["deployment"]["revision"], revision)
            self.assertEqual(deployed_memory["deployment"]["source_revision"], merged)

            def progress(evidence):
                with closing(sqlite3.connect(self.db)) as conn, conn:
                    conn.execute(
                        """UPDATE supervision_incidents SET status='resolved',resolved_by='natural',resolved_at=?,
                        resolution_evidence_json=? WHERE incident_id=?""",
                        (now, json.dumps(evidence), self.incident_id),
                    )
                return self.repair.verify(
                    self.config,
                    self.incident_id,
                    checkout=checkout,
                    runtime=runtime,
                    now=self.when,
                )

            evidence = {"receipt_id": "new-consumer-receipt"}
            old_progress = (self.when - timedelta(minutes=5)).isoformat()
            future_progress = (self.when + timedelta(minutes=5)).isoformat()
            for occurred_at in (
                None,
                old_progress,
                future_progress,
                "2026-11-06T10:00:00",
            ):
                with self.subTest(occurred_at=occurred_at):
                    pending = progress({**evidence, "occurred_at": occurred_at})
                    self.assertEqual(
                        pending["verification"]["original_progress"]["status"],
                        "pending",
                    )
            verified = progress({**evidence, "occurred_at": now})
            self.assertEqual(verified["status"], "verified")
