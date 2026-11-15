import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Timer
import unittest

from robert_agent.adapters.tapd.client import TapdClient, TapdError

WORKSPACE = "12345678"
TICKET = "10000000000000000001"
COMMENT = "20000000000000000002"


def page(number, items, *, limit=2, **counts):
    return {"items": items, "page": number, "limit": limit, **counts}


def story(external_id=TICKET):
    return {"id": external_id, "name": "A synthetic story", "status": "open", "owner": "member", "modified": "time"}


def comment(entry_type, comment_id=COMMENT):
    return {
        "id": comment_id,
        "workspace_id": WORKSPACE,
        "entry_type": entry_type,
        "entry_id": TICKET,
        "author": "display-member",
        "description": "<p>&#64;robert-wklken</p>",
        "root_id": "0",
        "reply_id": "0",
        "created": "2026-10-01 12:00:00",
        "modified": "2026-10-01 12:00:00",
    }


class CliTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.responses = []
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.cwd = Path(scratch.name)
        self.client = TapdClient(
            executable=Path("/configured/tapd"),
            cwd=self.cwd,
            environment={"HOME": scratch.name, "TAPD_API_USER": "fixture-user", "UNRELATED": "discard"},
            runner=self.run_fixture,
        )

    def run_fixture(self, command, **options):
        self.calls.append((command, options))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, subprocess.CompletedProcess):
            return response
        return subprocess.CompletedProcess(command, 0, json.dumps(response).encode(), b"")

    def test_filtered_scan_ignores_global_count_and_continues_after_short_page(self):
        self.responses = [
            page(1, [story()], total=999, has_more=True),
            page(2, [story("10000000000000000003")]),
            page(3, [], total=999, has_more=True),
        ]
        result = self.client.list_items(WORKSPACE, "story", owner="member", label="queue", page_size=2)
        self.assertTrue(result.complete)
        self.assertEqual([row.external_id for row in result.items], [TICKET, "10000000000000000003"])
        self.assertEqual([command[-3] for command, _ in self.calls], ["1", "2", "3"])
        command, options = self.calls[0]
        self.assertIn("--workspace-id", command)
        self.assertIn(WORKSPACE, command)
        self.assertIn("--label=queue", command)
        self.assertEqual(options["cwd"], self.cwd.resolve())
        self.assertEqual(options["env"], {"HOME": str(self.cwd), "TAPD_API_USER": "fixture-user"})

    def test_repeated_rows_bad_page_and_exhausted_limit_do_not_return_partial_results(self):
        for responses, code in [
            ([page(1, [story()]), page(2, [story()])], "duplicate_record"),
            ([page(1, [story()]), page(7, [])], "invalid_response"),
            ([page(1, [story()]), page(2, [story("10000000000000000003")])], "scan_limit"),
            ([page(1, [{**story(), "id": int(TICKET)}])], "invalid_response"),
        ]:
            with self.subTest(code=code):
                self.responses = responses
                with self.assertRaises(TapdError) as failure:
                    self.client.list_items(WORKSPACE, "story", page_size=2, max_pages=2)
                self.assertEqual(failure.exception.code, code)

    def test_comment_context_and_discovery_include_bug_remarks(self):
        readback = [
            page(1, [comment("bug")]),
            page(2, []),
            page(1, [comment("bug_remark", "20000000000000000003")]),
            page(2, []),
        ]
        self.responses = readback.copy()
        result = self.client.list_comments(WORKSPACE, "bug", TICKET, page_size=2)
        self.assertEqual(
            [(row.kind, row.entry_type, row.external_id) for row in result.items],
            [("bug", "bug", COMMENT), ("bug", "bug_remark", "20000000000000000003")],
        )
        self.assertEqual(result.items[0].description, "<p>&#64;robert-wklken</p>")
        self.assertEqual(result.items[0].display_author, "display-member")
        self.assertTrue(all(f"--entry-id={TICKET}" in command for command, _ in self.calls))
        self.calls.clear()
        self.responses = [page(1, []), *readback]
        discovered = self.client.discover_comments(WORKSPACE, "display-member", page_size=2)
        self.assertEqual(discovered.items, result.items)
        self.assertEqual(len(self.calls), 5)
        self.assertTrue(all("--author=display-member" in command for command, _ in self.calls))
        self.assertTrue(all(not any(part.startswith("--entry-id=") for part in command) for command, _ in self.calls))

    def test_comment_discards_no_scope_mismatch(self):
        self.responses = [page(1, [{**comment("stories"), "entry_id": "222"}])]
        with self.assertRaises(TapdError) as failure:
            self.client.list_comments(WORKSPACE, "story", TICKET, page_size=2)
        self.assertEqual(failure.exception.code, "invalid_response")

    def test_details_preserve_missing_label_as_unknown_and_report_workflow_mapping(self):
        detail = {
            **story(),
            "workspace_id": WORKSPACE,
            "description": "Markdown body",
            "workitem_type_id": "456",
            "is_archived": "0",
            "custom_sample": "opaque evidence",
        }
        self.responses = [detail, {"s2": "done", "s3": "tested(已验证)"}]
        row = self.client.get_item(WORKSPACE, "story", TICKET)
        self.assertIsNone(row.label)
        self.assertEqual(row.raw["custom_sample"], "opaque evidence")
        self.assertIn("--no-comments", self.calls[0][0])
        self.assertEqual(
            self.client.terminal_states(WORKSPACE, "story", workitem_type_id=row.workitem_type_id),
            {"s2": "done", "s3": "tested(已验证)"},
        )
        self.assertIn("--workitem-type-id=456", self.calls[1][0])

    def test_write_payload_only_through_stdin_and_results_remain_unconfirmed(self):
        self.responses = [{"success": True, "id": COMMENT}, {"success": True}]
        self.assertEqual(self.client.add_comment(WORKSPACE, "story", TICKET, "Result"), COMMENT)
        command, options = self.calls[0]
        self.assertIn("--entry-type=stories", command)
        self.assertNotIn("Result", command)
        self.assertFalse(any(part.startswith(("--author", "--reply-id", "--description")) for part in command))
        self.assertEqual(options["input"], b"Result")
        with self.assertRaises(TapdError) as failure:
            self.client.add_comment(WORKSPACE, "story", TICKET, "Result")
        self.assertTrue(failure.exception.effect_unknown)

    def test_diagnostics_never_return_cli_output_or_constructor_environment(self):
        self.responses = [subprocess.CompletedProcess([], 3, b"private item", b"private credential")]
        with self.assertRaises(TapdError) as failure:
            self.client.add_comment(WORKSPACE, "bug", TICKET, "Private body")
        self.assertTrue(failure.exception.effect_unknown)
        self.assertNotIn("private", str(failure.exception).casefold())
        self.assertIsNone(failure.exception.__cause__)

    def test_malformed_or_oversize_output_is_never_empty_success(self):
        bounded = TapdClient(
            executable=Path("/configured/tapd"),
            cwd=self.cwd,
            environment={},
            runner=self.run_fixture,
            max_output_bytes=128,
        )
        for output, expected in [(b'{"items":[', "invalid_response"), (b"x" * 129, "output_limit")]:
            self.responses = [subprocess.CompletedProcess([], 0, output, b"")]
            with self.subTest(expected=expected), self.assertRaises(TapdError) as failure:
                bounded.list_items(WORKSPACE, "bug")
            self.assertEqual(failure.exception.code, expected)


class ProcessBoundaryTests(unittest.TestCase):
    def test_real_child_is_bounded_by_output_deadline_and_stop_event(self):
        with TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            # A controlled child command is substituted at the runner boundary.
            from robert_agent.adapters.tapd.process import run_bounded

            cases = [
                ("print('x' * 100000)", "output_limit", 3, None),
                ("import time; time.sleep(5)", "timeout", 0.1, None),
                ("import time; time.sleep(5)", "cancelled", 3, Event()),
            ]
            for source, expected, timeout, stop in cases:
                with self.subTest(expected=expected):
                    timer = Timer(0.15, stop.set) if stop is not None else None
                    if timer is not None:
                        timer.start()
                    try:
                        with self.assertRaises(TapdError) as failure:
                            run_bounded(
                                [sys.executable, "-c", source],
                                cwd=cwd,
                                env={},
                                input=None,
                                timeout=timeout,
                                max_output_bytes=128,
                                stop=stop,
                            )
                        self.assertEqual(failure.exception.code, expected)
                    finally:
                        if timer is not None:
                            timer.cancel()
                            timer.join()
