from contextlib import closing
import json
from pathlib import Path
import sqlite3
from subprocess import CompletedProcess

from robert_agent.configuration.command import validate_config
from robert_agent.context import project
from robert_agent.delivery.application import publish
from tests.support.github_input import GitHubInputFixture


REPO = "repo:example/backend"
OTHER_REPO = "repo:example/elsewhere"
TITLE = "Tenant-scoped caching"
SUMMARY = "Include the tenant in cache keys. The worker claims this was deployed."
ISSUE_URL = "https://github.com/example/backend/issues/123"
COMMENT_URL = ISSUE_URL + "#issuecomment-987"


class MemoryFactStagesTests(GitHubInputFixture):
    def record_audited_memory(self):
        self.scan([self.event(body="@robert-bot analyze tenant caching", intent="analysis")])
        task_id, _ = self.finish(
            ["comment:trusted"],
            output_type="comment_analysis",
            planned_github_actions=[
                {"type": "comment", "target_url": ISSUE_URL, "body": "Use tenant-scoped cache keys."}
            ],
            memory_delta={
                "status": "has_memory",
                "entries": [
                    {
                        "kind": "decision",
                        "title": TITLE,
                        "short_summary": SUMMARY,
                        "keywords": ["caching"],
                        "confidence": "high",
                        "provenance": {"stage": "deployment_confirmed", "result_id": "worker-claimed-result"},
                    }
                ],
            },
        )
        [(attempt_id, result_id, raw_metadata)] = self.rows(
            "SELECT attempt_id,result_id,metadata_json FROM worker_results WHERE task_id=?", (task_id,)
        )
        metadata = json.loads(raw_metadata)
        self.assertEqual(metadata["audit"]["status"], "accepted")
        self.assertEqual(metadata["project_memory"]["status"], "recorded")
        self.assertEqual(
            self.rows("SELECT audit_status,publish_status FROM github_actions WHERE result_id=?", (result_id,)),
            [("accepted", "not_published")],
        )
        return {"task_id": task_id, "attempt_id": attempt_id, "result_id": result_id}

    def retrieve(self, repo_id=REPO):
        with closing(sqlite3.connect(self.db())) as conn:
            return project.retrieve_memories(
                conn,
                repo_id,
                "github:example/backend#124",
                {"route_id": "comment-analysis", "expected_output": "comment_analysis"},
                [{"body": "Review tenant caching", "intent": "analysis"}],
            )

    def memory_from(self, entries):
        [entry] = [entry for entry in entries if entry["title"] == TITLE]
        self.assertEqual(entry["short_summary"], SUMMARY)
        return entry

    def assert_provenance(self, entry, source, stage):
        provenance = entry.get("provenance", {})
        expected = {**source, "stage": stage}
        self.assertEqual({key: provenance.get(key) for key in expected}, expected, entry)

    def followup_prompt_memory(self, issue):
        result = self.scan(
            [
                self.event(
                    f"followup-{issue}",
                    body="@robert-bot analyze tenant caching again",
                    intent="analysis",
                    number=issue,
                    url=f"https://github.com/example/backend/issues/{issue}",
                )
            ]
        )
        [prompt_path] = result["prompt_paths"]
        prompt = Path(prompt_path).read_text(encoding="utf-8")
        _, heading, section = prompt.partition("Relevant Project Memories")
        self.assertTrue(heading, "project memory is absent from the follow-up prompt")
        _, opening, section = section.partition("\n\n    [")
        self.assertTrue(opening, "project memory JSON is absent from the follow-up prompt")
        memories, _ = json.JSONDecoder().raw_decode("[" + section)
        return self.memory_from(memories)

    def publish_comment(self, source):
        endpoint = "repos/example/backend/issues/123/comments"

        def runner(args, **kwargs):
            if args == ["gh", "api", f"{endpoint}?per_page=100", "--paginate", "--slurp"]:
                return CompletedProcess(args, 0, "[]", "")
            if args[:4] == ["gh", "api", endpoint, "-f"] and len(args) == 5 and args[4].startswith("body="):
                return CompletedProcess(
                    args,
                    0,
                    json.dumps(
                        {"id": 987, "html_url": COMMENT_URL, "user": {"login": "robert-bot"}, "body": args[4][5:]}
                    ),
                    "",
                )
            raise AssertionError(f"unexpected publication command: {args}")

        config = validate_config(self.config_path, skip_external=True)
        self.assertTrue(config["ok"], config)
        result = publish.publish_ready_actions(
            self.db(), task_ids=[source["task_id"]], config=config, run_command=runner
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["published_count"], 1, result)
        [(action_id, status, raw_evidence)] = self.rows(
            "SELECT action_id,publication_intents.status,remote_evidence_json FROM publication_intents"
            " JOIN github_actions USING (action_id) WHERE result_id=?",
            (source["result_id"],),
        )
        evidence = json.loads(raw_evidence)
        self.assertEqual((status, evidence["status"]), ("published", "confirmed_effect"))
        receipt = evidence["receipt"]
        self.assertEqual(
            (receipt["action_id"], receipt["task_id"], receipt["result_id"], receipt["external_id"]),
            (action_id, source["task_id"], source["result_id"], "987"),
        )

    def test_audited_fact_is_recalled_and_only_matching_publication_receipt_elevates_stage(self):
        source = self.record_audited_memory()
        before = self.memory_from(self.retrieve())
        with self.subTest(stage="audit", boundary="retrieval"):
            self.assert_provenance(before, source, "audit_accepted")
        with self.subTest(stage="audit", boundary="prompt"):
            self.assert_provenance(self.followup_prompt_memory(124), source, "audit_accepted")

        with closing(sqlite3.connect(self.db())) as conn, conn:
            conn.execute(
                "INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root)"
                " VALUES (?,'example/elsewhere','robert-bot','main','/elsewhere','/elsewhere-trees')",
                (OTHER_REPO,),
            )
        self.assertEqual(self.retrieve(OTHER_REPO), [])

        self.publish_comment(source)
        after = self.memory_from(self.retrieve())
        self.assertEqual(after["memory_id"], before["memory_id"])
        with self.subTest(stage="publication", boundary="retrieval"):
            self.assert_provenance(after, source, "publication_confirmed")
        with self.subTest(stage="publication", boundary="prompt"):
            self.assert_provenance(self.followup_prompt_memory(125), source, "publication_confirmed")

    def test_published_status_without_receipt_keeps_audit_stage(self):
        source = self.record_audited_memory()
        with closing(sqlite3.connect(self.db())) as conn, conn:
            # A legacy or inconsistent status can outlive its receipt.
            conn.execute(
                "UPDATE github_actions SET publish_status='published',external_id='987',target_url=? WHERE result_id=?",
                (COMMENT_URL, source["result_id"]),
            )
        self.assertEqual(self.rows("SELECT status FROM publication_intents WHERE status='published'"), [])
        self.assert_provenance(self.memory_from(self.retrieve()), source, "audit_accepted")
