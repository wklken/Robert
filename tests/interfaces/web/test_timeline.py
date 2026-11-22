import json

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
