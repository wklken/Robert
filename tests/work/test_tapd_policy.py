import unittest

from robert_agent.work.tapd_policy import matching_tag_rule, parse_comment


class CommentPolicyTests(unittest.TestCase):
    def test_ordinary_own_mention_after_quoting_bot_is_recognized(self):
        parsed = parse_comment(
            "<blockquote><p>robert-wklken： Earlier result</p></blockquote>"
            "<p>&#xff20;robe<span>rt</span>\u200b-wklken，继续调查</p>"
        )
        self.assertTrue(parsed.has_trigger)
        self.assertFalse(parsed.is_bot_prefixed)
        self.assertEqual(parsed.segments[0].kind, "quote")
        self.assertEqual(parsed.own_text, "@robert-wklken,继续调查")

    def test_structure_and_reserved_prefix_keep_automated_and_quoted_requests_out(self):
        cases = [
            "<blockquote>@robert-wklken original request</blockquote><p>Context only</p>",
            "<pre><code>@robert-wklken</code></pre><p>Context only</p>",
            "<p><code>@robert-wklken</code> Context only</p>",
            "> @robert-wklken\n\n`@robert-wklken`",
            "```python\n@robert-wklken\n```",
            "<p> \u2060robert-wklken：<br> @robert-wklken</p>",
            "<p>other@robert-wklken.com, @robert-wklken-extra</p>",
            "<blockquote>@robert-wklken<p>Unclosed quote with @robert-wklken</p>",
            "<p>@rob<code>example</code>ert-wklken</p>",
        ]
        for body in cases:
            with self.subTest(body=body):
                self.assertFalse(parse_comment(body).has_trigger)
        self.assertTrue(parse_comment(cases[5]).is_bot_prefixed)

    def test_unverified_service_reply_layout_cannot_supply_own_text_trigger(self):
        parsed = parse_comment("<p>Reply data @robert-wklken</p>", reply_id="123")
        self.assertFalse(parsed.structure_verified)
        self.assertFalse(parsed.has_trigger)
        ordinary = parse_comment("<p>回复 tester，@robert-wklken 请跟进</p>", reply_id="0")
        self.assertTrue(ordinary.structure_verified)
        self.assertTrue(ordinary.has_trigger)

    def test_tag_authority_uses_exact_known_facts(self):
        options = {
            "owners": ("member", "operator"),
            "labels": ("auto-queue",),
            "status": "active",
            "terminal_states": frozenset({"done", "tested"}),
            "archived": False,
            "owner": "member",
            "label": "auto-queue",
        }
        self.assertTrue(matching_tag_rule(**options))
        for changed in [
            {"owners": ("other-member",)},
            {"labels": None},
            {"labels": ("auto-queue-extra",)},
            {"status": "tested"},
            {"archived": None},
            {"archived": True},
            {"terminal_states": None},
        ]:
            with self.subTest(changed=changed):
                self.assertFalse(matching_tag_rule(**{**options, **changed}))

    def test_selected_marker_has_exact_boundaries_and_independent_reply_signature(self):
        identities = {"bot_name": "robot.ops", "reply_name": "queue.robot"}
        self.assertTrue(parse_comment("<p>＠ｒｏｂｏｔ．ｏｐｓ，调查</p>", **identities).has_trigger)
        for body in (
            "@robotXops",
            "@robot.ops.extra",
            "@robot.ops-extra",
            "someone@robot.ops",
            "@robert-wklken",
            "queue.robot： @robot.ops",
        ):
            with self.subTest(body=body):
                self.assertFalse(parse_comment(body, **identities).has_trigger)
        self.assertTrue(parse_comment("queue.robot： @robot.ops", **identities).is_bot_prefixed)
        self.assertTrue(parse_comment("robot.ops： @robot.ops", **identities).has_trigger)
        self.assertFalse(parse_comment("@robot-ops", bot_name="robert-ops").has_trigger)
        self.assertTrue(parse_comment("@robot-ops", bot_name="robot-ops").has_trigger)
