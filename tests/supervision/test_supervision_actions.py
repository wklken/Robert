from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent.persistence.schema import database as storage
from robert_agent.supervision import actions
from robert_agent.configuration.command import validate_config


NOW = "2026-11-06T10:00:00+00:00"
LATER = "2026-11-06T11:00:00+00:00"
RECHECK = "2026-11-06T11:01:00+00:00"
RECHECK_AGAIN = "2026-11-06T11:02:00+00:00"


class SupervisionActionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / "state.db"
        storage.init_database(self.db)
        self.conn = sqlite3.connect(self.db)
        self.addCleanup(self.conn.close)
        self.conn.execute(
            "INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) VALUES('repo','example/repo','default','main','/tmp','/tmp')"
        )
        self.task("task", "lineage")
        self.config = {
            "repos": [{"full_name": "example/repo"}],
            "supervision": {
                "mode": "recover_known",
                "recovery": {"repositories": ["example/repo"], "allowed_actions": ["spool_replay", "audit_retry"]},
            },
        }

    def task(self, task_id, lineage, status="running"):
        stream = "stream-" + task_id
        self.conn.execute(
            "INSERT INTO workstreams(workstream_id,repo_id,active_task_id,created_at,updated_at) VALUES(?,'repo',?,?,?)",
            (stream, task_id, NOW, NOW),
        )
        self.conn.execute(
            "INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at,metadata_json) VALUES(?,?,'running',?,?,?)",
            (task_id, stream, NOW, NOW, json.dumps({"supervision_lineage_key": lineage})),
        )
        self.conn.execute(
            "INSERT INTO attempts(attempt_id,task_id,attempt_no,status) VALUES(?,?,1,?)",
            ("attempt-" + task_id, task_id, status),
        )

    def observation(self, task_id="task"):
        return {
            "scope_type": "task",
            "scope_id": task_id,
            "classification": "suspect",
            "lineage_key": "lineage",
            "rule_id": "result_not_consumed",
            "reason_code": "result_waiting_ingestion",
            "evidence": {"attempt_id": "attempt-" + task_id, "spool_fingerprint": "fingerprint"},
            "expected_progress": {"kind": "result_ingested", "attempt_id": "attempt-" + task_id},
        }

    def plan(self, *, config=None):
        return actions.plan(self.conn, observations=[self.observation()], now=NOW, config=config or self.config)

    def begin(self, *, config=None, now=NOW, generation="fingerprint"):
        return actions.begin(
            self.conn,
            task_id="task",
            kind="spool_replay",
            target_id="attempt-task",
            generation=generation,
            now=now,
            config=config or self.config,
        )

    def test_plan_reserves_once_without_mutating_business_state(self):
        first = self.plan()
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["status"], "requested")
        self.assertEqual(self.plan(), [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT status FROM attempts").fetchone()[0], "running")

    def test_expired_unclaimed_request_cannot_repeat_same_generation(self):
        self.assertEqual(len(self.plan()), 1)
        actions.verify(self.conn, observations=[], now=LATER)
        self.assertEqual(actions.plan(self.conn, observations=[self.observation()], now=LATER, config=self.config), [])
        self.assertEqual(
            self.conn.execute("SELECT status,budget_cost FROM supervision_recovery_actions").fetchall(),
            [("rejected", 0)],
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM notifications WHERE notification_type='supervision_recovery_attention'"
            ).fetchone()[0],
            1,
        )

    def test_policy_exact_repo_and_known_lineage_required(self):
        disabled = {
            "repos": self.config["repos"],
            "supervision": {
                "mode": "recover_known",
                "recovery": {"repositories": ["example/other"], "allowed_actions": ["spool_replay"]},
            },
        }
        self.assertEqual(self.plan(config=disabled), [])
        self.conn.execute("UPDATE tasks SET metadata_json='{}'")
        self.assertEqual(self.plan(), [])

    def test_changed_spool_generation_rejects_before_claim(self):
        self.plan()
        self.assertEqual(self.begin(generation="replacement")["status"], "blocked")
        self.assertEqual(
            self.conn.execute("SELECT status,budget_cost FROM supervision_recovery_actions").fetchone(), ("rejected", 0)
        )

    def test_fence_blocks_duplicate_and_stale_finish(self):
        self.plan()
        result = self.begin()
        self.assertEqual(result["status"], "claimed")
        claim = result["claim"]
        self.assertEqual(self.begin()["status"], "blocked")
        self.assertFalse(
            actions.finish(
                self.conn,
                {**claim, "claim_generation": claim["claim_generation"] - 1},
                outcome="observing",
                receipt={},
                now=NOW,
            )
        )
        self.assertTrue(actions.finish(self.conn, claim, outcome="observing", receipt={}, now=NOW))
        actions.verify(self.conn, observations=[], now=NOW)
        self.assertEqual(
            self.conn.execute("SELECT status FROM supervision_recovery_actions").fetchone()[0], "observing"
        )

    def test_expired_claim_keeps_budget_until_same_result_is_verified(self):
        self.plan()
        self.assertEqual(self.begin()["status"], "claimed")
        self.conn.commit()  # Simulate losing the executing process after claiming.
        with closing(sqlite3.connect(self.db)) as restarted, restarted:
            actions.verify(restarted, observations=[], now=LATER)
        self.assertEqual(
            self.conn.execute("SELECT status,budget_cost FROM supervision_recovery_actions").fetchone(), ("unknown", 1)
        )
        self.assertEqual(self.begin(config={"supervision": {"mode": "observe"}}, now=LATER)["status"], "blocked")
        self.conn.execute(
            "INSERT INTO worker_results(result_id,task_id,attempt_id,output_type,created_at,metadata_json) VALUES('result','task','attempt-task','comment_analysis',?,?)",
            (LATER, json.dumps({"submission_fingerprint": "different"})),
        )
        self.conn.execute(
            """INSERT INTO wakeups(wakeup_id,repo_id,task_id,attempt_id,result_id,reason,dedupe_key,created_at,updated_at,not_before_at)
            VALUES('wakeup','repo','task','attempt-task','result','worker_result_ready','key',?,?,?)""",
            (LATER, LATER, LATER),
        )
        actions.verify(self.conn, observations=[], now=RECHECK)
        self.assertEqual(self.conn.execute("SELECT status FROM supervision_recovery_actions").fetchone()[0], "unknown")
        self.conn.execute(
            "UPDATE worker_results SET metadata_json=?", (json.dumps({"submission_fingerprint": "fingerprint"}),)
        )
        actions.verify(self.conn, observations=[], now=RECHECK_AGAIN)
        self.assertEqual(
            self.conn.execute("SELECT status,budget_cost FROM supervision_recovery_actions").fetchone(),
            ("succeeded", 1),
        )

    def test_native_retries_preserve_domain_budgets_without_supervisor_request(self):
        self.conn.commit()
        for kind in actions.STAGES:
            with self.subTest(kind=kind), patch("robert_agent.configuration.command.validate_config") as validate:
                config = {**self.config, "config_path": "/fixture/unused.yml"}
                for _ in range(3):
                    self.assertFalse(
                        actions.blocks_consumer(
                            self.conn,
                            task_id="task",
                            kind=kind,
                            target_id="native-target",
                            now=NOW,
                            config=config,
                            native_retry=True,
                        )
                    )
                    self.assertEqual(
                        actions.begin(
                            self.conn,
                            task_id="task",
                            kind=kind,
                            target_id="native-target",
                            now=NOW,
                            config=config,
                            native_retry=True,
                        )["status"],
                        "legacy",
                    )
                    self.assertFalse(self.conn.in_transaction)
                validate.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM supervision_recovery_actions").fetchone()[0], 0)

    def test_requested_consumer_hint_does_not_reload_policy_but_claim_does(self):
        self.plan()
        self.conn.commit()
        config = {**self.config, "config_path": "/fixture/unavailable.yml"}
        with patch("robert_agent.configuration.command.validate_config", return_value={"ok": False}) as validate:
            for _ in range(3):
                self.assertFalse(
                    actions.blocks_consumer(
                        self.conn,
                        task_id="task",
                        kind="spool_replay",
                        target_id="attempt-task",
                        now=NOW,
                        config=config,
                        native_retry=True,
                    )
                )
            validate.assert_not_called()
            self.assertEqual(self.begin(config=config)["reason_code"], "recovery_policy_unavailable")
            validate.assert_called_once()

    def test_cancellation_after_claim_prevents_later_application(self):
        self.plan()
        claim = self.begin()["claim"]
        self.conn.execute("UPDATE tasks SET lifecycle='canceled' WHERE task_id='task'")
        self.assertFalse(actions.current(self.conn, claim, config=self.config, now=NOW, generation="fingerprint"))

    def test_killed_audit_claim_releases_scope_without_refunding_budget(self):
        self.conn.execute("UPDATE attempts SET status='completed'")
        self.conn.execute(
            """INSERT INTO worker_results(result_id,task_id,attempt_id,output_type,created_at,metadata_json)
            VALUES('result','task','attempt-task','comment_analysis',?,?)""",
            (
                NOW,
                json.dumps(
                    {
                        "submission_fingerprint": "fingerprint",
                        "audit_observation": {"attempt_count": 1, "retryable": True},
                    }
                ),
            ),
        )
        actions.plan(
            self.conn,
            observations=[
                {**self.observation(), "reason_code": "result_waiting_audit", "evidence": {"result_id": "result"}}
            ],
            config=self.config,
            now=NOW,
        )
        self.conn.commit()
        child_script = """
import json, sqlite3, sys, time
from robert_agent.supervision import actions
db, raw_config, now = sys.argv[1:]
with sqlite3.connect(db) as conn:
    verdict = actions.begin(conn, task_id='task', kind='audit_retry', target_id='result',
                            config=json.loads(raw_config), now=now, native_retry=True)
print(json.dumps(verdict), flush=True)
time.sleep(30)
"""
        with subprocess.Popen(
            [sys.executable, "-B", "-c", child_script, str(self.db), json.dumps(self.config), NOW],
            stdout=subprocess.PIPE,
            text=True,
        ) as child:
            try:
                verdict = json.loads(child.stdout.readline())
                self.assertEqual(verdict["status"], "claimed")
            finally:
                child.kill()
                child.wait(timeout=10)
        with closing(sqlite3.connect(self.db)) as restarted, restarted:
            actions.verify(restarted, observations=[], now=LATER)
        self.assertEqual(
            self.conn.execute("SELECT status,budget_cost FROM supervision_recovery_actions").fetchone(), ("failed", 1)
        )
        self.assertIsNone(
            self.conn.execute("SELECT json_extract(metadata_json,'$.audit') FROM worker_results").fetchone()[0]
        )
        self.assertEqual(
            actions.begin(
                self.conn, task_id="task", kind="audit_retry", target_id="result", config=self.config, now=LATER
            )["status"],
            "legacy",
        )
        self.assertFalse(actions.current(self.conn, verdict["claim"], config=self.config, now=LATER))

    def test_disk_policy_changes_fence_stale_config(self):
        root = self.db.parent
        (root / ".git").mkdir()
        trees = root / "trees"
        trees.mkdir()
        path = root / "robert.yml"
        raw = {
            "version": 2,
            "data_dir": str(root),
            "database": self.db.name,
            "github": {"account": "default"},
            "default_worker": "default",
            "workers": {"default": {"adapter": "cbc", "command": "cbc", "model": "default", "effort": "default"}},
            "repos": [
                {
                    "full_name": "example/repo",
                    "github_account": "default",
                    "trusted_actors": ["owner"],
                    "default_branch": "main",
                    "checkout": str(root),
                    "worktrees": str(trees),
                }
            ],
            "supervision": self.config["supervision"],
        }

        def save():
            path.write_text(yaml.safe_dump(raw), encoding="utf-8")

        save()
        startup = validate_config(path, skip_external=True)
        self.assertTrue(startup["ok"], startup)
        self.assertEqual(len(self.plan(config=startup)), 1)
        claim = self.begin(config=startup)["claim"]
        self.assertTrue(actions.current(self.conn, claim, config=startup, now=NOW, generation="fingerprint"))
        raw["supervision"] = {"mode": "observe"}
        save()
        self.assertFalse(actions.current(self.conn, claim, config=startup, now=NOW, generation="fingerprint"))
        self.task("replacement", "fresh-lineage")
        fresh = {**self.observation("replacement"), "lineage_key": "fresh-lineage"}
        self.assertEqual(actions.plan(self.conn, observations=[fresh], now=NOW, config=startup), [])
        raw["supervision"] = self.config["supervision"]
        save()
        self.assertEqual(len(actions.plan(self.conn, observations=[fresh], now=NOW, config=startup)), 1)
        raw["database"] = "other.db"
        save()
        native = actions.begin(
            self.conn,
            task_id="replacement",
            kind="spool_replay",
            target_id="attempt-replacement",
            config=startup,
            now=NOW,
            generation="fingerprint",
            native_retry=True,
        )
        self.assertEqual(native.get("reason_code"), "recovery_policy_unavailable")
