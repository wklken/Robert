"""Shared fixtures for supervision repair tests."""

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent.adapters.sqlite import database as storage
from robert_agent.supervision import diagnosis as supervision_diagnose
from robert_agent.supervision import store as supervision_store


class RepairFixture(unittest.TestCase):
    def setUp(self):
        from robert_agent.workflows.maintenance import repair as supervision_repair

        self.repair = supervision_repair
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.db = root / "data" / "db.sqlite3"
        self.checkout = root / "repo"
        self.worktree_root = root / "trees"
        self.checkout.mkdir()
        self.worktree_root.mkdir()
        self.config_file = root / "config.yml"
        self.config_file.write_text("configuration: private\n")
        self.launcher = root / "launcher.py"
        self.launcher.write_text("import os,sys; os.execv(sys.argv[2],sys.argv[2:])\n")
        self.launcher.chmod(0o600)
        self.config_file.chmod(0o600)
        storage.init_database(self.db)
        self.when = datetime.now(timezone.utc)
        self.repo = {
            "repo_id": "r",
            "full_name": "acme/robert",
            "default_base_branch": "main",
            "enabled": True,
            "repo_root": str(self.checkout),
            "worktree_root": str(self.worktree_root),
        }
        self.config = {
            "db_path": str(self.db),
            "data_dir": str(self.db.parent),
            "config_path": str(self.config_file),
            "repos": [self.repo],
            "workers": [
                {
                    "name": "repair",
                    "agent": "command",
                    "command_argv": [sys.executable, "-c", "raise SystemExit(0)"],
                    "environment_allowlist": [],
                }
            ],
            "supervision": {"mode": "observe", "repair": {"repository": "acme/robert"}},
        }
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) VALUES (?,?,?,?,?,?)",
                (
                    "r",
                    "acme/robert",
                    "bot",
                    "main",
                    str(self.checkout),
                    str(self.worktree_root),
                ),
            )
        self.incident_id = self.incident("controller", "local")

    def incident(self, scope_type, scope_id):
        now = self.when.isoformat()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.row_factory = sqlite3.Row
            changes = supervision_store.apply_observations(
                conn,
                [
                    {
                        "scope_type": scope_type,
                        "scope_id": scope_id,
                        "rule_id": "controller_stalled",
                        "lineage_key": "lineage:" + scope_id,
                        "classification": "confirmed",
                        "reason_code": "consumer_failed",
                        "severity": "error",
                        "evidence": {"failure_class": "Known regression"},
                        "expected_progress": {"kind": "new_consumer_receipt"},
                    }
                ],
                now,
            )
            incident = changes[0]["incident"]
            target = supervision_diagnose._target(incident, "incident")
            snapshot = supervision_diagnose._snapshot(conn, target)
            bundle = supervision_diagnose._bundle(snapshot, target, "diagnosis-fixture")
            output = {
                "diagnosis_class": "product_defect",
                "recommended_action": "repair_task",
                "expected_progress": "The matching consumer records a receipt",
                "evidence_ids": ["e1"],
                "bundle_id": bundle["bundle_id"],
                "bundle_sha256": bundle["bundle_sha256"],
            }
            supervision_store.update_checkpoint(
                conn,
                "diagnosis",
                incident["incident_id"],
                "analysis",
                now=now,
                metadata={
                    "status": "succeeded",
                    "diagnosis": output,
                    "bundle": bundle,
                    "input_generation": bundle["input_generation"],
                },
            )
        return incident["incident_id"]

    def auto_policy(self, actions=None):
        policy = {
            "mode": "auto",
            "repository": "acme/robert",
            "worker": "repair",
            "allowed_actions": ["push_existing_pr", "open_pr"] if actions is None else actions,
            "launcher": [sys.executable, str(self.launcher)],
            "verification_commands": [[sys.executable, "-c", "pass"]],
        }
        self.config["supervision"]["repair"] = policy
        return policy
