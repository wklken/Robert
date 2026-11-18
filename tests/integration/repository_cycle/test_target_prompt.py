from pathlib import Path
import unittest
from tests.support import PACKAGE_ROOT  # noqa: F401
from tests.support import attempt_context as context_tests
from robert_agent.work.application import preparation
from robert_agent.foundation import values
from robert_agent.persistence.rounds import pre_pr_review


class TargetPromptTests(unittest.TestCase):
    setUp = context_tests.AttemptContextFixture.setUp
    add_event = context_tests.AttemptContextFixture.add_event
    refresh = context_tests.AttemptContextFixture.refresh

    def targets(self, prompt):
        return [line[2:] for line in prompt.splitlines() if line.startswith("- https://github.com/")]

    def test_initial_refresh_and_resume_prompts_expose_bound_targets(self):
        self.assertEqual(
            self.targets(self.prompt.read_text()),
            ["https://github.com/example/backend/issues/1"],
        )
        event = self.add_event()
        source_id = values.github_source_id(event["source_key"])
        self.conn.execute(
            "INSERT INTO workstream_sources VALUES (?,?,?,?)",
            (self.stream, source_id, "related", values.utc_now()),
        )
        self.conn.commit()
        self.refresh(event)
        expected = [
            "https://github.com/example/backend/issues/1",
            "https://github.com/example/backend/pull/2",
        ]
        self.assertEqual(self.targets(self.prompt.read_text()), expected)
        resumed = preparation.create_resume_attempt_and_prompt(
            self.conn,
            self.data,
            self.db,
            self.repo,
            values.repository_id("example/backend"),
            self.task_id,
            self.stream,
            self.attempt_id,
            values.utc_now(),
            {"summary": "retry"},
        )
        self.assertEqual(self.targets(Path(resumed["prompt_path"]).read_text()), expected)

    def test_refresh_to_existing_pr_removes_the_unstarted_candidate_gate(self):
        pre_pr_review.create(self.conn, self.task_id, {"plan": {}})
        self.conn.execute(
            "UPDATE tasks SET metadata_json=json_set(metadata_json, '$.pre_pr_review_required', 1) WHERE task_id=?",
            (self.task_id,),
        )
        self.conn.commit()
        self.refresh(self.add_event())
        route_id, expected_output = self.conn.execute(
            "SELECT route_id,expected_output FROM tasks WHERE task_id=?", (self.task_id,)
        ).fetchone()
        self.assertEqual((route_id, expected_output), ("update-existing-pr", "update_existing_pr"))
        self.assertFalse(pre_pr_review.required(self.conn, self.task_id))
        self.assertIsNone(pre_pr_review.session(self.conn, task_id=self.task_id))
