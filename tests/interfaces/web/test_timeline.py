from dataclasses import replace
import json
from unittest.mock import patch

from robert_agent.interfaces.web import server
from tests.support.board import BoardReadModelFixture


class TimelineEndpointsTests(BoardReadModelFixture):
    def test_timeline_assets_and_navigation_are_available(self):
        for route in ("/", "/work", "/operations"):
            status, _, html = server.build_http_response(route, self.db_path)
            self.assertEqual(status, 200)
            self.assertIn(b'href="/timeline"', html)
        for route, mime, marker in (
            ("/timeline", "text/html", b'id="receipt-dialog"'),
            ("/assets/timeline.css", "text/css", b".activity-calendar"),
            ("/assets/timeline.js", "text/javascript", b"/api/timeline?"),
            ("/assets/receipt-image.js", "text/javascript", b"renderReceiptImage"),
        ):
            with self.subTest(route=route):
                status, headers, body = server.build_http_response(route, self.db_path)
                self.assertEqual(status, 200)
                self.assertEqual(headers["content-type"], f"{mime}; charset=utf-8")
                self.assertIn(marker, body)

    def test_live_display_routes_and_validation(self):
        for route, marker in (
            ("/timeline", b'href="/live-display"'),
            ("/live-display", b'id="receipt-dialog"'),
            ("/assets/live-display.css", b".display-metrics"),
            ("/assets/live-display.js", b"/api/live-display"),
        ):
            with self.subTest(route=route):
                status, _, body = server.build_http_response(route, self.db_path)
                self.assertEqual(status, 200)
                self.assertIn(marker, body)
        status, headers, body = server.build_http_response("/api/live-display?tz=Asia/Shanghai", self.db_path)
        self.assertEqual(status, 200)
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(
            json.loads(body), {"counts": {"total": 0, "week": 0, "today": 0, "running": 0}, "receipts": []}
        )
        for route, expected in (
            ("/api/live-display?tz=Unknown/Zone", 422),
            ("/api/live-display/receipt?task_id=task", 422),
            ("/api/live-display/receipt?task_id=task&result_id=missing", 404),
        ):
            with self.subTest(route=route):
                status, _, _ = server.build_http_response(route, self.db_path)
                self.assertEqual(status, expected)

    def test_empty_period_and_validation(self):
        status, _, body = server.build_http_response(
            "/api/timeline?view=week&date=2026-11-22&tz=Asia%2FShanghai", self.db_path
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual((data["start"], data["end"]), ("2026-11-16", "2026-11-22"))
        self.assertEqual(data["entries"], [])

        for query in ("view=year", "date=", "date=2026-02-30", "tz=Unknown%2FZone", "tz=.."):
            with self.subTest(query=query):
                status, _, body = server.build_http_response(f"/api/timeline?{query}", self.db_path)
                self.assertEqual(status, 422)
                self.assertEqual(json.loads(body)["status"], "failed_validation")

    def test_receipt_requires_task_and_result_and_returns_not_found_for_missing_records(self):
        for query, expected in (("task_id=task", 422), ("task_id=task&result_id=unknown", 404)):
            with self.subTest(query=query):
                status, headers, _ = server.build_http_response(f"/api/timeline/receipt?{query}", self.db_path)
                self.assertEqual(status, expected)
                self.assertEqual(headers["content-type"], "application/json; charset=utf-8")

    def test_receipt_converts_only_reported_dollars_and_identifies_the_reference_rate(self):
        for usd, expected in ((0.012, "0.0805572"), (0.0, "0.00000"), (None, None)):
            with self.subTest(usd=usd):
                receipt = {"usage": {"reported_cost_usd": usd}}
                with patch.object(server.timeline, "get_receipt", return_value=receipt):
                    status, _, body = server.build_http_response(
                        "/api/timeline/receipt?task_id=task&result_id=result", self.db_path
                    )
                self.assertEqual(status, 200)
                self.assertEqual(
                    json.loads(body)["usage"],
                    {
                        "reported_cost_usd": usd,
                        "reported_cost_cny": expected,
                        "usd_to_cny": "6.7131",
                        "fx_date": "2026-11-20",
                        "fx_source": "cfets_reference",
                    },
                )

    def test_operator_receipt_exchange_rate_overrides_dated_default(self):
        context = server.build_read_only_control_context(self.db_path, host="127.0.0.1", port=8765)
        context = replace(context, receipt_fx=server._receipt_exchange_rate("6.8", "2026-11-19"))
        with patch.object(server.timeline, "get_receipt", return_value={"usage": {"reported_cost_usd": 0.012}}):
            status, _, body = server.build_http_response(
                "/api/timeline/receipt?task_id=task&result_id=result", self.db_path, context
            )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["usage"]["reported_cost_cny"], "0.0816")
        self.assertEqual(json.loads(body)["usage"]["fx_source"], "operator_reference")
        self.assertEqual(json.loads(body)["usage"]["fx_date"], "2026-11-19")

    def test_invalid_or_incomplete_receipt_exchange_rate_is_rejected_at_startup(self):
        for flags in (
            ["--receipt-fx-rate", "6.8"],
            ["--receipt-fx-rate", "NaN", "--receipt-fx-date", "2026-11-19"],
            ["--receipt-fx-rate", "-2", "--receipt-fx-date", "2026-11-19"],
            ["--receipt-fx-rate", "6.8", "--receipt-fx-date", "2026-02-30"],
        ):
            with (
                self.subTest(flags=flags),
                patch.object(server, "serve") as serve,
                patch.object(server, "emit") as emit,
            ):
                server.main(["--db", str(self.db_path), *flags])
                serve.assert_not_called()
                failure, code = emit.call_args.args
                self.assertEqual(code, 3)
                self.assertEqual(failure["status"], "failed_input")
