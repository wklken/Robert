from datetime import datetime, timedelta, timezone
import unittest

from robert_agent.work import tapd_policy

NAME = "robert-wklken"
SHANGHAI = timezone(timedelta(hours=8))


def project(**overrides):
    return {
        "host": "tapd.example.com",
        "workspace_id": "123",
        "trusted_user": "alice",
        "accept_after": "2026-11-20T09:00:00+08:00",
        "label_rules": [
            {"label": "repo-a", "repository": "owner/repo-a"},
            {"label": "repo-b", "repository": "owner/repo-b"},
        ],
        **overrides,
    }


def comment(**overrides):
    return {
        "comment_id": "501",
        "author": "alice",
        "created": "2026-11-20 10:00:00",
        "body": f"<p>@{NAME} 请分析一下</p>",
        **overrides,
    }


class MentionTests(unittest.TestCase):
    def assert_trigger(self, body, expected):
        self.assertEqual(tapd_policy.parse_comment(body, NAME).mentions, expected, body)

    def test_visible_ascii_mention_triggers_in_html_and_plain_text(self):
        for body in (
            f"<p>@{NAME} 看下</p>",
            f"<p>前面<br  />@{NAME.upper()}</p>",
            f"@{NAME}",
            f"请 @{NAME}，谢谢",
        ):
            with self.subTest(body=body):
                self.assert_trigger(body, True)

    def test_quotes_code_variants_and_prefixed_comments_do_not_trigger(self):
        for body in (
            f"<blockquote><p>@{NAME} 旧请求</p></blockquote><p>补充</p>",
            f"<p><code>@{NAME}</code></p>",
            f"<pre><code>@{NAME}\n</code></pre>",
            f"<p>＠{NAME}</p>",
            "<p>@robert\u200b-wklken</p>",
            f"<p>mail@{NAME}</p>",
            f"<p>@{NAME}-bot</p>",
            f"> @{NAME} quoted\nlazy line",
            f"`@{NAME}`",
            f"```\n@{NAME}\n```",
            f"<p>{NAME}：已完成 @{NAME}</p>",
            f"<p>{NAME}: done @{NAME}</p>",
            "<p>没有标记</p>",
        ):
            with self.subTest(body=body):
                self.assert_trigger(body, False)

    def test_prefix_marks_automatic_comments(self):
        self.assertTrue(tapd_policy.parse_comment(f"<p>{NAME}：结论</p><p>回执 abc</p>", NAME).automatic)
        self.assertFalse(tapd_policy.parse_comment(f"<p>说明 {NAME}：</p>", NAME).automatic)

    def test_html_inside_markdown_context_does_not_authorize_a_mention(self):
        for body in (
            f"```html\n<span>example</span>\n@{NAME}\n```",
            f"~~~html\n<div>example</div>\n@{NAME}\n~~~",
            f"<p>说明</p>\n```\n@{NAME}\n```",
            f"<p>说明</p>\n    <span>@{NAME}</span>",
            f"<p>说明</p>\n`<span>@{NAME}</span>`",
            f"<p>说明</p>\n> <span>@{NAME}</span>",
        ):
            with self.subTest(body=body):
                parsed = tapd_policy.parse_comment(body, NAME)
                self.assertFalse(parsed.mentions)
                self.assertIn(f"@{NAME}", parsed.text)

    def test_visible_text_keeps_paragraphs_and_entities(self):
        parsed = tapd_policy.parse_comment("<p>a &amp; b</p><p><br  /></p><p>c&nbsp;d</p>", NAME)
        self.assertEqual(parsed.text, "a & b\n\nc\xa0d")

    def test_outbound_mentions_are_neutralized(self):
        self.assertEqual(
            tapd_policy.neutralize_mentions(f"已处理 @{NAME} 和 @{NAME.upper()}，mail@{NAME} 保留", NAME),
            f"已处理 {NAME} 和 {NAME.upper()}，mail@{NAME} 保留",
        )


class RepositoryTests(unittest.TestCase):
    def test_one_matching_label_selects_its_repository(self):
        self.assertEqual(
            tapd_policy.select_repository("x|repo-b|y", project()["label_rules"]),
            ("selected", "owner/repo-b", "repo-b"),
        )

    def test_missing_or_multiple_labels_select_nothing(self):
        rules = project()["label_rules"]
        self.assertEqual(tapd_policy.select_repository(None, rules), ("missing", None, None))
        self.assertEqual(tapd_policy.select_repository("other", rules), ("missing", None, None))
        self.assertEqual(tapd_policy.select_repository("repo-a|repo-b", rules), ("conflict", None, None))


class AuthorizationTests(unittest.TestCase):
    def decide(self, *, known=False, automatic_ids=(), **changes):
        return tapd_policy.authorize_comment(
            comment(**changes), project(), mention_name=NAME, automatic_ids=set(automatic_ids), known=known
        )["authorization_status"]

    def test_trusted_mention_after_start_is_a_trigger_or_a_reply(self):
        self.assertEqual(self.decide(), "authorized_trigger")
        self.assertEqual(self.decide(known=True), "accepted_context")

    def test_other_inputs_remain_context(self):
        self.assertEqual(self.decide(author="mallory"), "ignored_context")
        self.assertEqual(self.decide(created="2026-11-20 08:59:59"), "ignored_context")
        self.assertEqual(self.decide(body="<p>没有标记</p>"), "ignored_context")
        self.assertEqual(self.decide(automatic_ids={"501"}), "context_only")
        self.assertEqual(self.decide(body=f"<p>{NAME}：结论 @{NAME}</p>"), "context_only")

    def test_tapd_times_are_shanghai_wall_clock(self):
        self.assertEqual(tapd_policy.parse_time("2026-11-20 09:00:00"), datetime(2026, 11, 20, 9, tzinfo=SHANGHAI))
        self.assertIsNone(tapd_policy.parse_time("2026-11-20T09:00:00"))

    def test_saved_request_reauthorizes_against_current_configuration(self):
        event = {
            "provider": "tapd",
            "project_key": "tapd.example.com/123",
            "workstream_id": "tapd:tapd.example.com/123/story/9:owner/repo-a",
            "repo": "owner/repo-a",
            "repository_label": "repo-a",
            "actor_login": "alice",
            "comment_created": "2026-11-20 10:00:00",
            "mentions_dd": True,
            "automatic": False,
        }
        config = {"enabled": True, "mention_name": NAME, "projects": [project()]}
        accepted = tapd_policy.reauthorize(event, config, known_workstreams=())
        self.assertEqual(
            (accepted["authorization_status"], accepted["tapd_trusted_user"]), ("authorized_trigger", "alice")
        )
        self.assertEqual(
            tapd_policy.reauthorize(event, config, known_workstreams=[event["workstream_id"]])["authorization_status"],
            "accepted_context",
        )
        ended = tapd_policy.reauthorize(event, config, known_workstreams=(), ended=True)
        self.assertEqual((ended["authorization_status"], ended["state"]), ("ignored_current_state_closed", "closed"))
        for changed in (
            {"enabled": False},
            {"projects": [project(trusted_user="bob")]},
            {"projects": [project(label_rules=[{"label": "repo-a", "repository": "owner/repo-b"}])]},
            {"projects": [project(accept_after="2026-11-21T00:00:00+08:00")]},
            {"projects": []},
        ):
            with self.subTest(changed=changed):
                decision = tapd_policy.reauthorize(event, {**config, **changed}, known_workstreams=())
                self.assertFalse(decision["drives_execution"])
        self.assertFalse(tapd_policy.reauthorize(event, None, known_workstreams=())["drives_execution"])

    def test_end_state_requires_a_listed_status(self):
        self.assertTrue(tapd_policy.is_ended("status_3", {"status_3": "done"}))
        self.assertFalse(tapd_policy.is_ended("status_17", {"status_3": "done"}))
        self.assertFalse(tapd_policy.is_ended("status_3", {}))


if __name__ == "__main__":
    unittest.main()
