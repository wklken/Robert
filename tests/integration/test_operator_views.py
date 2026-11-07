from robert_agent.readmodels import dashboard
from contextlib import closing
import json
import sqlite3
from tests.support.summary_database import SummaryFixture


class OperatorViewIntegrationTests(SummaryFixture):
    def test_web_exposes_acceptance_metrics_with_usage(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        usage_payload = {
            "usage_available": True,
            "source": "cbc_stream_json",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_creation_input_tokens": 2,
                "cache_read_input_tokens": 3,
            },
            "total_cost_usd": 0.0123,
        }
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                "UPDATE worker_results SET metadata_json=json_set(?, '$.result_contract', 'compact-v1') WHERE result_id = 'result-1'",
                (
                    json.dumps(
                        {
                            "audit": {"status": "accepted"},
                            "usage": usage_payload,
                        },
                        sort_keys=True,
                    ),
                ),
            )
            conn.execute(
                """
                UPDATE github_actions
                SET publish_status = 'published',
                    metadata_json = ?
                WHERE action_id = 'action-1'
                """,
                (
                    json.dumps(
                        {
                            "body": self._dd_comment_body("ready"),
                            "publish": {"status": "published", "deduplicated": True},
                        },
                        sort_keys=True,
                    ),
                ),
            )

        data = dashboard.build_dashboard_data(db_path)
        metrics = data["acceptance_metrics"]

        self.assertEqual(metrics["accepted_results"], 1)
        self.assertEqual(metrics["rejected_results"], 0)
        self.assertEqual(metrics["published_actions"], 1)
        self.assertEqual(metrics["deduplicated_actions"], 1)
        self.assertTrue(metrics["usage_available"])
        self.assertEqual(metrics["total_input_tokens"], 10)
        self.assertEqual(metrics["total_output_tokens"], 5)
        self.assertEqual(metrics["total_cache_creation_input_tokens"], 2)
        self.assertEqual(metrics["total_cache_read_input_tokens"], 3)
        self.assertEqual(metrics["total_cost_usd"], 0.0123)

    def test_web_status_and_chat_expose_latest_loop_summary(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        latest_loop = {
            "ok": True,
            "status": "completed",
            "cycles": 2,
            "stop_reason": "max_cycles",
        }
        (db_path.parent / "latest-loop.json").write_text(
            json.dumps(latest_loop, sort_keys=True),
            encoding="utf-8",
        )

        data = dashboard.build_dashboard_data(db_path)
        chat_code, chat_payload = self._run_chat_status("--db", str(db_path), "status")
        status_code, status_payload = self._run_status("--db", str(db_path), "status")

        self.assertEqual(data["latest_loop"]["stop_reason"], "max_cycles")
        self.assertEqual(chat_code, 0)
        self.assertIn("Latest loop: max_cycles", chat_payload["message"])
        self.assertEqual(status_code, 0)
        self.assertEqual(status_payload["data"]["latest_loop"]["cycles"], 2)
