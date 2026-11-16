"""TAPD discovery: trusted mentions become requests once; everything else stays context."""

from pathlib import Path
import json
import tempfile
import unittest
from unittest import mock

from robert_agent.persistence.schema import database as storage
from robert_agent.work.application import tapd_scan
from tests.support.database import connect
from tests.support.tapd import BUG, STORY, FakeClient, comment

HOST, WS = "tapd.example", "100"
KEY = f"{HOST}/{WS}"
PROJECT = {
    "host": HOST,
    "workspace_id": WS,
    "trusted_user": "alice",
    "accept_after": "2026-11-20T01:00:00+00:00",
    "label_rules": [{"label": "repo-a", "repository": "example/repo"}],
}
SETTINGS = {"enabled": True, "executable": "tapd", "mention_name": "robert-wklken", "projects": [PROJECT]}
REPO = {
    "full_name": "example/repo",
    "github_account": "robot",
    "default_base_branch": "main",
    "repo_root": "/repo",
    "worktree_root": "/worktrees",
}


class TapdScanTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / "robert.sqlite3"
        storage.init_database(self.db)
        self.client = FakeClient()

    def scan(self, now="2026-11-20T03:00:00+00:00"):
        return tapd_scan.scan_project(str(self.db), SETTINGS, PROJECT, {"example/repo": REPO}, self.client, now=now)

    def events(self):
        with connect(self.db) as conn:
            rows = conn.execute(
                "SELECT json_extract(payload_json, '$.comment_id'), authorization_status FROM github_events "
                "ORDER BY event_at, event_id"
            ).fetchall()
        return {row[0]: row[1] for row in rows}

    def test_only_trusted_mentions_after_start_trigger_and_rescans_do_not_duplicate(self):
        self.client.comments = [
            comment("1", "@robert-wklken old", "2026-11-20 08:59:59"),
            comment("2", "@robert-wklken from bob", author="bob"),
            comment("3", "plain note", "2026-11-20 10:01:00"),
            comment("4", "<p>@robert-wklken 请分析</p>", "2026-11-20 10:02:00"),
        ]
        first = self.scan()
        second = self.scan()
        with connect(self.db) as conn:
            source = conn.execute("SELECT provider, state, metadata_json FROM github_sources").fetchone()
            watermark = conn.execute("SELECT watermark_created, watermark_comment_id FROM tapd_scan_state").fetchone()
        self.assertEqual(self.events(), {"3": "ignored_context", "4": "authorized_trigger"})
        self.assertEqual((first["events"], second["events"]), (2, 0))
        self.assertEqual((source[0], source[1]), ("tapd", "open"))
        snapshot = json.loads(source[2])["tapd"]
        self.assertEqual([item["comment_id"] for item in snapshot["comments"]], ["1", "2", "3", "4"])
        self.assertEqual(snapshot["repository"], "example/repo")
        self.assertEqual(tuple(watermark), ("2026-11-20 10:02:00", "4"))
        self.assertEqual(self.client.author_calls, ["2026-11-20 09:00:00", "2026-11-20 09:52:00"])

    def test_automatic_quoted_full_width_and_zero_width_mentions_do_not_trigger(self):
        self.client.comments = [
            comment("5", "robert-wklken：结论 @robert-wklken"),
            comment("6", "<blockquote>@robert-wklken</blockquote><p>agree</p>"),
            comment("7", "＠robert-wklken 看看"),
            comment("8", "@robert\u200b-wklken 看看"),
            comment("9", "@robert-wklken 正常请求", "2026-11-20 10:05:00"),
        ]
        self.scan()
        events = self.events()
        self.assertEqual(events["9"], "authorized_trigger")
        self.assertEqual({events[key] for key in ("6", "7", "8")}, {"ignored_context"})
        self.assertEqual(events["5"], "context_only")

    def test_marker_added_by_editing_an_observed_comment_is_not_a_request(self):
        self.client.comments = [comment("10", "@robert-wklken first", "2026-11-20 10:00:00")]
        self.scan()
        self.client.comments += [
            comment("11", "@robert-wklken edited later", "2026-11-20 09:55:00", modified="2026-11-20 10:30:00"),
            comment("12", "@robert-wklken delayed", "2026-11-20 09:58:00"),
        ]
        self.scan()
        self.assertEqual(
            self.events(), {"10": "authorized_trigger", "11": "ignored_context", "12": "authorized_trigger"}
        )

    def test_missing_label_replies_once_and_only_a_new_mention_runs_after_relabeling(self):
        self.client.labels[STORY] = None
        self.client.comments = [comment("20", "@robert-wklken 帮忙")]
        first = self.scan()
        self.scan()
        with connect(self.db) as conn:
            notices = conn.execute("SELECT channel, status, notification_type FROM notifications").fetchall()
            sources = conn.execute("SELECT COUNT(*) FROM github_sources").fetchone()[0]
        self.assertEqual(first["label_notices"], 1)
        self.assertEqual([tuple(row) for row in notices], [("tapd", "pending", "tapd_repository_label")])
        self.assertEqual(sources, 0)
        self.client.labels[STORY] = "repo-a"
        self.client.comments.append(comment("21", "@robert-wklken 已加标签", "2026-11-20 10:03:00"))
        self.scan()
        self.assertEqual(self.events(), {"20": "ignored_context", "21": "authorized_trigger"})

    def test_failed_ticket_read_keeps_its_request_and_a_rerun_records_each_request_once(self):
        self.client.comments = [
            comment("30", "@robert-wklken story"),
            comment("31", "@robert-wklken bug", "2026-11-20 10:01:00", entry=BUG, kind="bug"),
        ]
        self.client.fail_items = {BUG}
        failed = self.scan()
        self.client.comments.append(comment("32", "later note", "2026-11-20 10:30:00", entry=BUG, kind="bug"))
        self.scan()
        self.client.fail_items = set()
        self.scan()
        self.scan()
        self.assertEqual(failed["failures"], [{"stage": "ticket", "external_id": BUG, "reason": "command_failed"}])
        self.assertEqual(
            self.events(), {"30": "authorized_trigger", "31": "authorized_trigger", "32": "ignored_context"}
        )

    def test_missing_ticket_keeps_its_request_until_the_ticket_can_be_read(self):
        self.client.comments = [comment("33", "@robert-wklken bug", entry=BUG, kind="bug")]
        self.client.missing_items = {BUG}
        missing = self.scan()
        self.client.comments.append(comment("34", "later note", "2026-11-20 10:30:00"))
        self.scan()
        self.client.missing_items = set()
        self.scan()
        self.assertEqual(missing["failures"], [{"stage": "ticket", "external_id": BUG, "reason": "not_found"}])
        self.assertEqual(self.events(), {"33": "authorized_trigger"})

    def test_failed_scan_does_not_let_an_edit_turn_an_observed_comment_into_a_request(self):
        self.client.comments = [
            comment("35", "plain note"),
            comment("36", "@robert-wklken bug", "2026-11-20 10:01:00", entry=BUG, kind="bug"),
        ]
        self.client.fail_items = {BUG}
        self.scan()
        self.client.fail_items = set()
        self.client.comments[0] = comment("35", "@robert-wklken plain note", modified="2026-11-20 10:30:00")
        self.scan()
        self.assertEqual(self.events(), {"36": "authorized_trigger"})

    def test_request_edited_before_its_failed_ticket_read_still_runs_on_retry(self):
        self.client.comments = [
            comment("37", "@robert-wklken bug", entry=BUG, kind="bug", modified="2026-11-20 10:05:00"),
            comment("38", "plain note", "2026-11-20 10:30:00"),
        ]
        self.client.fail_items = {BUG}
        self.scan()
        self.client.fail_items = set()
        self.scan()
        self.assertEqual(self.events(), {"37": "authorized_trigger"})

    def test_tracked_ticket_refresh_records_end_state_and_new_context(self):
        self.client.comments = [comment("40", "@robert-wklken 开始")]
        self.scan()
        with connect(self.db) as conn:
            conn.execute(
                "INSERT INTO workstreams(workstream_id, repo_id, primary_source_id, lifecycle, created_at, updated_at) "
                "SELECT source_key, repo_id, source_id, 'active', 'now', 'now' FROM github_sources"
            )
        self.client.status[STORY] = "status_3"
        self.client.comments.append(comment("41", "补充说明", "2026-11-20 10:20:00"))
        result = self.scan()
        with connect(self.db) as conn:
            state = conn.execute("SELECT state, json_extract(metadata_json, '$.tapd.ended') FROM github_sources")
            self.assertEqual(tuple(state.fetchone()), ("closed", 1))
        self.assertEqual(result["refreshed"], 1)
        self.assertEqual(self.events(), {"40": "authorized_trigger", "41": "ignored_context"})

    def test_mention_seen_during_tracked_refresh_uses_new_labels_on_next_discovery(self):
        repo_b = {**REPO, "full_name": "example/second"}
        project = {
            **PROJECT,
            "label_rules": [*PROJECT["label_rules"], {"label": "repo-b", "repository": "example/second"}],
        }
        repos = {REPO["full_name"]: REPO, repo_b["full_name"]: repo_b}
        self.client.comments = [comment("40", "@robert-wklken 初次请求")]
        self.scan()
        author_comments = self.client.author_comments

        def mention_after_discovery(*args, **kwargs):
            comments = author_comments(*args, **kwargs)
            self.client.labels[STORY] = "repo-b"
            self.client.comments.append(comment("41", "@robert-wklken 新仓库请求", "2026-11-20 10:21:00"))
            return comments

        with mock.patch.object(self.client, "author_comments", side_effect=mention_after_discovery):
            tapd_scan.scan_project(str(self.db), SETTINGS, project, repos, self.client)
        tapd_scan.scan_project(str(self.db), SETTINGS, project, repos, self.client)
        with connect(self.db) as conn:
            row = conn.execute(
                "SELECT repo_id FROM github_events WHERE json_extract(payload_json, '$.comment_id') = '41'"
            ).fetchone()
        self.assertEqual(row[0], "repo:example/second")

    def test_prefix_alone_does_not_identify_an_earlier_robert_reply(self):
        self.client.comments = [
            comment("45", "@robert-wklken normal request"),
            comment("46", "robert-wklken：unverified reply", author="other"),
        ]
        self.scan()
        with connect(self.db) as conn:
            snapshot = json.loads(conn.execute("SELECT metadata_json FROM github_sources").fetchone()[0])["tapd"]
        claimed_reply = next(item for item in snapshot["comments"] if item["comment_id"] == "46")
        self.assertFalse(claimed_reply.get("robert_reply", claimed_reply.get("automatic")))

    def test_disabled_configuration_does_not_build_a_client(self):
        self.assertEqual(tapd_scan.scan({"tapd": {"enabled": False}}), {"enabled": False, "projects": []})


if __name__ == "__main__":
    unittest.main()
