from contextlib import closing
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from tests.support import PACKAGE_ROOT
from tests.support.repository_cycle import (
    RepositoryCycleFixture,
    _verification_evidence,
)


class PromptMemoryTests(RepositoryCycleFixture):
    def test_project_memory_is_recorded_and_recalled_in_followup_prompt(self):
        from robert_agent.delivery.application import submission as result
        from robert_agent.runtime import cycle as run_once

        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(first["ok"], first)
        db_path = self.data_dir / "dd.sqlite3"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_id, attempt_id = conn.execute("SELECT task_id, attempt_id FROM attempts").fetchone()

        record = result.record_result(
            db_path,
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "output_type": "new_pr",
                "submission": {
                    "planned_github_actions": self._new_pr_actions_for_attempt(
                        db_path,
                        task_id,
                    ),
                    "consumed_event_fingerprints": ["comment:comment-1"],
                    "verification": [_verification_evidence()],
                    "handoff": "opened PR",
                    "used_skills": ["fast-small-pr"],
                    "memory_delta": {
                        "status": "has_memory",
                        "entries": [
                            {
                                "operation": "upsert",
                                "kind": "decision",
                                "title": "DD PR follow-up uses update-existing-pr",
                                "short_summary": "When dd-pr-followup review comments arrive, update the PR branch rather than reopening the origin issue task.",
                                "long_summary": "The PR workstream is the active implementation thread for review comments on a DD-created PR.",
                                "paths": ["src/robert_agent/runtime/cycle.py"],
                                "symbols": ["_is_dd_pr_followup_event"],
                                "keywords": ["dd-pr-followup", "update-existing-pr"],
                                "confidence": "medium",
                            }
                        ],
                    },
                },
            },
        )
        self.assertTrue(record["ok"], record)

        audited = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(audited["ok"], audited)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            memory_count = conn.execute("SELECT COUNT(*) FROM project_memory_entries").fetchone()[0]
            result_metadata = json.loads(
                conn.execute(
                    "SELECT metadata_json FROM worker_results WHERE result_id = ?",
                    (record["result_id"],),
                ).fetchone()[0]
            )
        self.assertEqual(memory_count, 1)
        self.assertEqual(result_metadata["project_memory"]["status"], "recorded")

        followup_fixture = self.root / "followup-memory.json"
        followup_fixture.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "comment-memory-followup",
                            "number": 124,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "body": "@robert-bot please fix this dd-pr-followup handling",
                            "intent": "bug_fix",
                            "url": "https://github.com/example/backend/issues/124",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        recalled = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=followup_fixture,
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(recalled["ok"], recalled)
        prompt = Path(recalled["prompt_paths"][0]).read_text(encoding="utf-8")
        self.assertIn("Relevant Project Memories", prompt)
        self.assertIn("DD PR follow-up uses update-existing-pr", prompt)
        self.assertIn("dd-pr-followup review comments", prompt)

    def test_approved_runtime_knowledge_is_injected_but_pending_candidate_is_not(self):
        from robert_agent.runtime import cycle as run_once

        db_path = self.data_dir / "dd.sqlite3"
        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(first["ok"], first)
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO knowledge_candidates(
                  candidate_id, repo_id, title, summary, prompt_text,
                  candidate_type, source_memory_ids_json, evidence_json,
                  confidence, status, created_at, metadata_json
                )
                VALUES (
                  'kc-pending', 'repo:example/backend',
                  'Pending knowledge must not appear',
                  'Pending candidates are proposals only.',
                  'PENDING KNOWLEDGE SHOULD NOT BE IN PROMPT',
                  'rule', '[]', '[]', 'medium', 'pending', ?, '{}'
                )
                """,
                (now,),
            )
            conn.execute(
                """
                INSERT INTO knowledge_candidates(
                  candidate_id, repo_id, title, summary, prompt_text,
                  candidate_type, source_memory_ids_json, evidence_json,
                  confidence, status, reviewed_at, reviewer, created_at, metadata_json
                )
                VALUES (
                  'kc-approved', 'repo:example/backend',
                  'Approved DD PR review rule',
                  'Approved candidates can enter worker prompts.',
                  'Use update-existing-pr for approved DD PR review follow-up handling.',
                  'rule', '[]', '[]', 'medium', 'approved', ?, 'wklken', ?, '{}'
                )
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO runtime_knowledge(
                  knowledge_id, candidate_id, repo_id, scope_type, scope_value,
                  title, prompt_text, retrieval_boost_json, active,
                  approved_by, approved_at, created_at, updated_at, metadata_json
                )
                VALUES (
                  'rk-approved', 'kc-approved', 'repo:example/backend',
                  'route', 'new-pr',
                  'Approved DD PR review rule',
                  'Use update-existing-pr for approved DD PR review follow-up handling.',
                  '{"keywords": ["dd-pr-followup"]}', 1,
                  'wklken', ?, ?, ?, '{}'
                )
                """,
                (now, now, now),
            )

        result = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(result["ok"], result)
        prompt = Path(result["prompt_paths"][0]).read_text(encoding="utf-8")
        self.assertIn("Approved Runtime Knowledge", prompt)
        self.assertIn("Approved DD PR review rule", prompt)
        self.assertIn("Use update-existing-pr", prompt)
        self.assertNotIn("PENDING KNOWLEDGE SHOULD NOT BE IN PROMPT", prompt)
