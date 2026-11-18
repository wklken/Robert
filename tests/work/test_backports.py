"""Explicit commands keep branch targets literal and quoted context inert."""

import unittest

from robert_agent.work import backports


class BackportCommandTests(unittest.TestCase):
    def test_supported_command_spellings_and_duplicate_targets(self):
        for text in (
            "@robert cherry-pick #3533 to release/1.25, release/1.26, release/1.25",
            "@robert 请回移 #3533 到 release/1.25 和 release/1.26",
            "<p>@robert please backport #3533 to release/1.25 and release/1.26</p>",
        ):
            with self.subTest(text=text):
                request = backports.parse_request(text, "robert")
                self.assertEqual(request.source_pr, 3533)
                self.assertEqual(request.targets, ("release/1.25", "release/1.26"))

    def test_words_inside_literal_branch_names_are_preserved(self):
        request = backports.parse_request(
            "@robert backport #10 to fixes/search-and-replace, release/研发和测试 and stable",
            "robert",
        )
        self.assertEqual(request.targets, ("fixes/search-and-replace", "release/研发和测试", "stable"))

    def test_context_invalid_targets_and_multiple_commands_are_inert(self):
        for text in (
            "> @robert cherry-pick #10 to release/1.25",
            "```\n@robert cherry-pick #10 to release/1.25\n```",
            "<blockquote>@robert cherry-pick #10 to release/1.25</blockquote>",
            "@robert cherry-pick #10 to -main",
            "@robert cherry-pick #10 to main~1",
            "@robert cherry-pick #10 to main..other",
            "@robert cherry-pick #10 to refs/.hidden",
            "@robert cherry-pick #10 to main.lock",
            "@robert cherry-pick #10 to main,",
            "@robert cherry-pick #10 to main\ncherry-pick #11 to other",
            "@robert can you explain cherry-pick #10 to main?",
        ):
            with self.subTest(text=text):
                self.assertIsNone(backports.parse_request(text, "robert"))

    def test_html_examples_in_fences_cannot_supply_or_hide_body_commands(self):
        for example in ("<span>example</span>", "<pre>", "</blockquote>"):
            for fence in ("```", "~~~"):
                with self.subTest(example=example, fence=fence):
                    body = f"{fence}html\n{example}\ncherry-pick #10 to release/old\n{fence}"
                    self.assertIsNone(backports.parse_request(body, "robert"))
                    request = backports.parse_request(body + "\n@robert cherry-pick #11 to release/current", "robert")
                    self.assertIsNotNone(request)
                    self.assertEqual(request.source_pr, 11)
                    self.assertEqual(request.targets, ("release/current",))
