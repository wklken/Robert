import unittest

from robert_agent.foundation import tapd_identity


class TapdIdentityTests(unittest.TestCase):
    def test_ticket_urls_round_trip_for_story_and_bug(self):
        for kind, url in (
            ("story", "https://tapd.example.com/123/prong/stories/view/1120001234000000101"),
            ("bug", "https://tapd.example.com/123/bugtrace/bugs/view/1120001234000000202"),
        ):
            with self.subTest(kind=kind):
                external_id = url.rsplit("/", 1)[1]
                self.assertEqual(tapd_identity.ticket_url("tapd.example.com", "123", kind, external_id), url)
                self.assertEqual(tapd_identity.parse_ticket_url(url), ("tapd.example.com", "123", kind, external_id))

    def test_non_canonical_ticket_urls_are_rejected(self):
        for url in (
            "http://tapd.example.com/123/prong/stories/view/1",
            "https://tapd.example.com:8443/123/prong/stories/view/1",
            "https://tapd.example.com/123/prong/stories/view/1?x=1",
            "https://tapd.example.com/123/prong/stories/view/1#comment",
            "https://tapd.example.com/123/prong/stories/view/0",
            "https://tapd.example.com/123/prong/tasks/view/1",
            "https://tapd.example.com/123/prong/stories/view/1/extra",
            "https://github.com/owner/repo/issues/1",
            None,
        ):
            with self.subTest(url=url):
                self.assertIsNone(tapd_identity.parse_ticket_url(url))

    def test_numeric_ids_reject_zero_and_non_digits(self):
        self.assertTrue(tapd_identity.numeric_id_is_valid("20001234"))
        for value in ("0", "", "01", "12a", 12, None):
            with self.subTest(value=value):
                self.assertFalse(tapd_identity.numeric_id_is_valid(value))

    def test_identity_strings_keep_host_workspace_and_repository(self):
        self.assertEqual(
            tapd_identity.source_key("tapd.example.com", "123", "bug", "9", "owner/repo"),
            "tapd:tapd.example.com/123/bug/9:owner/repo",
        )
        self.assertEqual(
            tapd_identity.comment_fingerprint("tapd.example.com", "123", "77"),
            "tapd-comment:tapd.example.com/123/77",
        )
        self.assertEqual(tapd_identity.canonical_host("TAPD.Example.com"), "tapd.example.com")
        for host in ("https://tapd.example.com", "tapd.example.com:80", "tapd..com", "user@tapd.com"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                tapd_identity.canonical_host(host)


if __name__ == "__main__":
    unittest.main()
