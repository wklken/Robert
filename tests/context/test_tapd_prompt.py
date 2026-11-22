"""TAPD prompts separate the current request, the ticket discussion and earlier Robert replies."""

import unittest

from robert_agent.context import prompts
from robert_agent.context.worker_prompt import worker_prompt_input
from robert_agent.workflows import routes

URL = "https://tapd.example/100/prong/stories/view/1100"


def discussion(**extra):
    return {
        "url": URL,
        "kind": "story",
        "external_id": "1100",
        "title": "Ticket",
        "status": "status_17",
        "labels": ["repo-a"],
        "description": "需求描述",
        "description_truncated": False,
        "comments": [
            {"comment_id": "1", "author": "bob", "created": "2026-11-20 09:00:00", "text": "背景", "automatic": False},
            {
                "comment_id": "2",
                "author": "robert",
                "created": "2026-11-20 09:10:00",
                "text": "旧结论",
                "robert_reply": True,
            },
            {
                "comment_id": "3",
                "author": "alice",
                "created": "2026-11-20 10:00:00",
                "text": "请分析",
                "automatic": False,
            },
        ],
        "omitted_comments": 0,
        "observed_at": "2026-11-20T02:00:00+00:00",
        **extra,
    }


REQUEST = {
    "event_fingerprint": "tapd:comment:3",
    "provider": "tapd",
    "comment_id": "3",
    "actor_login": "alice",
    "comment_created": "2026-11-20 10:00:00",
    "body": "请分析",
    "url": URL,
}


def render(route_id, ticket, events=(REQUEST,)):
    task = {"task_id": "task-1", "attempt_id": "attempt-1", "workstream_id": "tapd:tapd.example/100/story/1100:o/r"}
    prompt_input = worker_prompt_input(
        task, routes.ROUTES[route_id], list(events), {"tapd_discussions": [ticket], "result_script": "/r.py"}
    )
    return prompt_input, prompts.render_prompt(prompt_input)


class TapdPromptTests(unittest.TestCase):
    def test_request_discussion_and_prior_results_are_separate_items(self):
        prompt_input, rendered = render("comment-analysis", discussion())
        items = {item.id: item for item in prompt_input.context_items}
        self.assertEqual(set(items), {"tapd-request", "tapd-ticket", "tapd-discussion", "tapd-prior-results"})
        self.assertEqual([entry["comment_id"] for entry in items["tapd-request"].content], ["3"])
        self.assertEqual([entry["comment_id"] for entry in items["tapd-discussion"].content], ["1"])
        self.assertEqual([entry["comment_id"] for entry in items["tapd-prior-results"].content], ["2"])
        self.assertEqual({item.completeness for item in items.values()}, {"complete"})
        self.assertIn("Keep TAPD out of GitHub", rendered.text)
        self.assertIn("需求描述", rendered.text)

    def test_classification_is_limited_to_tapd_routes(self):
        _, rendered = render("classification-result", discussion())
        self.assertIn("recommended_route to comment-analysis", rendered.text)
        self.assertIn("Robert rejects any other route", rendered.text)

    def test_truncation_is_reported_explicitly(self):
        long_comments = [
            {"comment_id": str(10 + index), "author": "bob", "created": f"2026-11-20 09:{index:02d}:00",
             "text": "x" * 3990, "truncated": index == 9, "automatic": False}
            for index in range(10)
        ]  # fmt: skip
        prompt_input, rendered = render(
            "comment-analysis",
            discussion(description_truncated=True, comments=long_comments, omitted_comments=40),
        )
        items = {item.id: item for item in prompt_input.context_items}
        self.assertEqual(items["tapd-ticket"].completeness, "truncated")
        limit = items["tapd-discussion"].reason
        self.assertIn("40 earliest ticket comments were not loaded", limit)
        self.assertIn("7 earlier comments are omitted to fit the prompt", limit)
        self.assertIn("cut to their first 4000 characters", limit)
        self.assertEqual(items["tapd-discussion"].content[-1]["comment_id"], "19")
        self.assertIn("Limit: 40 earliest ticket comments were not loaded", rendered.text)

    def test_github_tasks_never_receive_tapd_guidance(self):
        task = {"task_id": "task-1", "attempt_id": "attempt-1", "workstream_id": "github:o/r#1"}
        prompt_input = worker_prompt_input(task, routes.ROUTES["comment-analysis"], [{"event_fingerprint": "c:1"}], {})
        self.assertNotIn("tapd", prompt_input.guidance)
        self.assertEqual([item.id for item in prompt_input.context_items], ["request", "decision-owner"])


if __name__ == "__main__":
    unittest.main()
