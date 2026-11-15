import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from robert_agent.adapters.tapd.client import TapdClient, TapdError
from robert_agent.foundation.commands import ControlDeadlineExceeded

WORKSPACE = "123"
STORY = "1120001234000000101"


def page(number, items, *, limit=200, total=999):
    return {"items": items, "page": number, "limit": limit, "total": total, "has_more": True}


def comment(comment_id, created, *, entry_type="stories", author="alice", entry_id=STORY):
    return {
        "id": comment_id,
        "workspace_id": WORKSPACE,
        "entry_type": entry_type,
        "entry_id": entry_id,
        "author": author,
        "description": "<p>@robert-wklken</p>",
        "title": "在状态 [doing] 添加",
        "root_id": "0",
        "reply_id": "0",
        "created": created,
        "modified": created,
    }


class TapdClientTests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.cwd = Path(scratch.name)
        self.calls, self.responses = [], []
        self.client = TapdClient(
            executable=Path("/opt/tapd"),
            cwd=self.cwd,
            environment={"HOME": scratch.name, "TAPD_ACCESS_TOKEN": "token-value", "GH_TOKEN": "discard"},
            runner=self.run_fixture,
        )

    def run_fixture(self, command, **options):
        self.calls.append((command, options))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, subprocess.CompletedProcess):
            return response
        return subprocess.CompletedProcess(command, 0, json.dumps(response).encode(), b"")

    def test_author_scan_reads_each_type_until_the_boundary_with_scoped_process_options(self):
        self.responses = [
            page(1, [comment("12", "2026-11-20 10:00:00"), comment("11", "2026-11-20 09:00:00")], limit=2),
            page(2, [comment("10", "2026-11-19 23:00:00")], limit=2),
            page(1, [comment("21", "2026-11-20 09:30:00", entry_type="bug")], limit=2),
        ]
        rows = self.client.author_comments(WORKSPACE, "alice", since="2026-11-20 00:00:00", page_size=2)
        self.assertEqual([row.comment_id for row in rows], ["12", "11", "21"])
        self.assertEqual({row.kind for row in rows}, {"story", "bug"})
        command, options = self.calls[0]
        self.assertEqual(command[:4], ["/opt/tapd", "--workspace-id", WORKSPACE, "--json"])
        self.assertIn("--author=alice", command)
        self.assertIn("--order=created desc", command)
        self.assertIn("--entry-type=stories", command)
        self.assertIn("--entry-type=bug", self.calls[2][0])
        self.assertEqual(options["cwd"], self.cwd.resolve())
        self.assertEqual(options["env"], {"HOME": str(self.cwd), "TAPD_ACCESS_TOKEN": "token-value"})
        self.assertEqual(options["input"], b"")

    def test_author_scan_fails_closed_on_page_limit_mismatch_and_bad_rows(self):
        for responses, code in (
            (
                [
                    page(1, [comment("12", "2026-11-20 10:00:00")], limit=1),
                    page(2, [comment("11", "2026-11-20 09:00:00")], limit=1),
                ],
                "scan_limit",
            ),
            ([page(1, [comment("12", "2026-11-20 10:00:00", author="bob")], limit=1)], "invalid_response"),
            ([page(1, [{**comment("12", "2026-11-20 10:00:00"), "workspace_id": "999"}], limit=1)], "invalid_response"),
            (
                [
                    page(1, [comment("12", "2026-11-20 10:00:00")], limit=1),
                    page(2, [comment("12", "2026-11-20 10:00:00")], limit=1),
                ],
                "duplicate_record",
            ),
            ([page(3, [], limit=1)], "invalid_response"),
        ):
            with self.subTest(code=code):
                self.calls.clear()
                self.responses = list(responses)
                with self.assertRaises(TapdError) as raised:
                    self.client.author_comments(
                        WORKSPACE, "alice", since="2026-11-01 00:00:00", page_size=1, max_pages=2
                    )
                self.assertEqual(raised.exception.code, code)

    def test_ticket_comments_read_all_pages_and_validate_the_entry(self):
        self.responses = [
            page(1, [comment("12", "2026-11-20 10:00:00", author="bob")], limit=1),
            page(2, [], limit=1),
        ]
        rows = self.client.ticket_comments(WORKSPACE, "story", STORY, page_size=1)
        self.assertEqual([(row.comment_id, row.author) for row in rows], [("12", "bob")])
        self.assertIn(f"--entry-id={STORY}", self.calls[0][0])
        self.responses = [page(1, [comment("12", "2026-11-20 10:00:00", entry_id="777")], limit=1)]
        with self.assertRaises(TapdError):
            self.client.ticket_comments(WORKSPACE, "story", STORY, page_size=1)
        with self.assertRaises(TapdError) as raised:
            self.client.ticket_comments(WORKSPACE, "story", "0")
        self.assertEqual(raised.exception.code, "invalid_input")

    def test_ticket_details_and_end_states(self):
        self.responses = [
            {
                "id": STORY,
                "workspace_id": WORKSPACE,
                "name": "title",
                "status": "status_17",
                "label": "repo-a|x",
                "workitem_type_id": "1120001234000000001",
                "is_archived": "0",
                "description": "<p>body</p>",
            },
            {"status_2": "refuse", "status_3": "done"},
        ]
        item = self.client.get_item(WORKSPACE, "story", STORY)
        self.assertEqual(
            (item.title, item.status, item.labels, item.archived), ("title", "status_17", "repo-a|x", False)
        )
        self.assertEqual(self.calls[0][0][-4:], ["story", "show", STORY, "--no-comments"])
        states = self.client.terminal_states(WORKSPACE, "story", item.workitem_type_id)
        self.assertEqual(states, {"status_2": "refuse", "status_3": "done"})
        self.assertIn("--workitem-type-id=1120001234000000001", self.calls[1][0])

    def test_errors_are_classified_without_remote_text(self):
        failures = (
            (
                subprocess.CompletedProcess([], 4, b"", b'{"error":"api_error","message":"story 9 not found"}'),
                "not_found",
            ),
            (
                subprocess.CompletedProcess([], 4, b"", b'{"error":"api_error","message":"HTTP 500: secret"}'),
                "command_failed",
            ),
            (subprocess.CompletedProcess([], 0, b"not json", b""), "invalid_response"),
            (subprocess.TimeoutExpired(["tapd"], 1), "timeout"),
            (FileNotFoundError("tapd"), "command_failed"),
        )
        for response, code in failures:
            with self.subTest(code=code):
                self.responses = [response]
                with self.assertRaises(TapdError) as raised:
                    self.client.get_item(WORKSPACE, "story", STORY)
                self.assertEqual(raised.exception.code, code)
                self.assertNotIn("secret", str(raised.exception))
                self.assertFalse(raised.exception.effect_unknown)
        self.responses = [ControlDeadlineExceeded(["tapd"], 1)]
        with self.assertRaises(ControlDeadlineExceeded):
            self.client.get_item(WORKSPACE, "story", STORY)

    def test_comment_writes_use_stdin_and_mark_unknown_effects(self):
        self.responses = [{"success": True, "id": "1120001234000009999"}]
        self.assertEqual(
            self.client.add_comment(WORKSPACE, "bug", "1120001234000000202", "正文"), "1120001234000009999"
        )
        command, options = self.calls[0]
        self.assertEqual(command[4:], ["comment", "add", "--entry-type=bug", "--entry-id=1120001234000000202"])
        self.assertEqual(options["input"], "正文".encode())
        for response in (subprocess.TimeoutExpired(["tapd"], 1), {"success": False}):
            with self.subTest(response=response):
                self.responses = [response]
                with self.assertRaises(TapdError) as raised:
                    self.client.add_comment(WORKSPACE, "bug", "1120001234000000202", "正文")
                self.assertTrue(raised.exception.effect_unknown)


if __name__ == "__main__":
    unittest.main()
