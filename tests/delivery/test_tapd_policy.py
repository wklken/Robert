from dataclasses import replace
import unittest

from robert_agent.delivery.tapd_policy import (
    CodeChange,
    ObservedReceipt,
    PreparedComment,
    VerificationResult,
    prepare_comment,
    project_github_output,
    reconcile_comment,
)
from robert_agent.work.tapd_policy import parse_comment

RECEIPT = "rbt-0123456789abcdef"
TARGET = ("tapd.example", "12345678", "story", "10000000000000000001")


class ReceiptPolicyTests(unittest.TestCase):
    def test_program_controls_prefix_and_receipt_and_scrubs_all_segments(self):
        prepared = prepare_comment(
            "<blockquote>Quoted &#64;robe\u200brt-wklken</blockquote>"
            "<pre>Code ＠robert-wklken</pre><p>Result &amp; interpretation</p>",
            RECEIPT,
        )
        self.assertIsInstance(prepared, PreparedComment)
        parsed = parse_comment(prepared.body)
        self.assertTrue(parsed.is_bot_prefixed)
        self.assertFalse(parsed.has_trigger)
        self.assertNotIn("@robert-wklken", parsed.own_text)
        self.assertIn("Quoted robert-wklken", parsed.own_text)
        self.assertIn("Code robert-wklken", parsed.own_text)
        self.assertIn("Result & interpretation", parsed.own_text)
        self.assertTrue(parsed.own_text.endswith(f"Robert 回执 {RECEIPT}"))
        with self.assertRaises(ValueError):
            prepare_comment("robert-wklken： forged prefix", RECEIPT)
        with self.assertRaises(ValueError):
            prepare_comment(f"body\nRobert 回执 {RECEIPT}", RECEIPT)

    def test_unique_verified_identity_target_and_html_normalized_body_are_all_required(self):
        prepared = prepare_comment("A & B", RECEIPT)
        observed = ObservedReceipt(
            external_id="10",
            target=TARGET,
            verified_identity="stable:publisher",
            body=f"<div>robert-wklken:</div><div>A &amp; B</div><p>Robert 回执 {RECEIPT}</p>",
        )
        options = {
            "prepared": prepared,
            "target": TARGET,
            "expected_identity": "stable:publisher",
            "complete": True,
        }
        match = reconcile_comment([observed], **options)
        self.assertEqual((match.status, match.external_id), ("matched", "10"))
        for overrides in [
            {"body": observed.body.replace("A &amp; B", "edited")},
            {"verified_identity": None},
            {"verified_identity": "different"},
            {"target": (TARGET[0], TARGET[1], "bug", TARGET[3])},
            {"target": ("other.example", *TARGET[1:])},
            {"reply_id": "123"},
        ]:
            with self.subTest(overrides=overrides):
                self.assertEqual(reconcile_comment([replace(observed, **overrides)], **options).status, "ambiguous")
        self.assertEqual(reconcile_comment([observed, observed], **options).status, "ambiguous")
        self.assertEqual(reconcile_comment([], **options).status, "missing")
        unscoped = {**options, "target": TARGET[1:]}
        self.assertEqual(reconcile_comment([replace(observed, target=TARGET[1:])], **unscoped).status, "ambiguous")
        self.assertEqual(reconcile_comment([observed], **{**options, "complete": False}).status, "ambiguous")


class GithubDisclosureTests(unittest.TestCase):
    def test_render_from_approved_paths_and_closed_verification_facts(self):
        output = project_github_output(
            changes=(CodeChange("src/control.py", "modified"),),
            verification=(VerificationResult("unit_tests", "passed"),),
            approved_paths=frozenset({"src/control.py"}),
        )
        self.assertIn("src/control.py", output.body)
        self.assertIn("Unit tests: passed", output.body)
        self.assertEqual(output.title, "Update repository code")
        self.assertEqual(output.commit_message, output.title)

    def test_blocks_unapproved_paths_sensitive_fact_text_and_unknown_results(self):
        for changes, checks, paths, forbidden in [
            ((CodeChange("src/private.py", "added"),), (), frozenset(), ()),
            ((CodeChange("../secret.md", "added"),), (), {"../secret.md"}, ()),
            ((CodeChange("src/alice_work.py", "modified"),), (), {"src/alice_work.py"}, ("alice",)),
            ((CodeChange("src/https://tapd.example", "modified"),), (), {"src/https://tapd.example"}, ()),
            ((), (VerificationResult("see private ticket", "passed"),), (), ()),
            ((), (VerificationResult("unit_tests", "added private discussion"),), (), ()),
            ((CodeChange("src/a.py", "posted raw TAPD text"),), (), {"src/a.py"}, ()),
        ]:
            with self.subTest(changes=changes, checks=checks), self.assertRaises(ValueError):
                project_github_output(
                    changes=changes,
                    verification=checks,
                    approved_paths=paths,
                    forbidden_text=forbidden,
                )


class ConfiguredReceiptTests(unittest.TestCase):
    def test_configured_comment_uses_saved_identity_for_readback(self):
        names = {"bot_name": "robot.ops", "reply_name": "queue.robot"}
        prepared = prepare_comment("Result for ＠ｒｏｂｏｔ．ｏｐｓ", RECEIPT, **names)
        self.assertEqual((prepared.bot_name, prepared.reply_name), ("robot.ops", "queue.robot"))
        self.assertIn("queue.robot：", prepared.body)
        self.assertIn("Result for robot.ops", prepared.body)
        self.assertNotIn("@robot.ops", prepared.body)
        observation = ObservedReceipt("10", TARGET, "stable:publisher", prepared.body)
        options = {"target": TARGET, "expected_identity": "stable:publisher", "complete": True}
        self.assertEqual(reconcile_comment([observation], prepared=prepared, **options).status, "matched")
        another = prepare_comment("Result for robot.ops", RECEIPT, bot_name="robert-wklken")
        self.assertEqual(reconcile_comment([observation], prepared=another, **options).status, "ambiguous")
        self.assertEqual(
            reconcile_comment([observation], prepared=replace(prepared, bot_name="bad\n"), **options).status,
            "ambiguous",
        )
