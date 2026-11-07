from tests.support.dashboard import DashboardFixture


class SummarizeTests(DashboardFixture):
    def test_summarize_counts_control_plane_rows(self):
        from robert_agent.readmodels import summarize

        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        summary = summarize.summarize_database(db_path)

        self.assertEqual(summary["repos"], 1)
        self.assertEqual(summary["workstreams"], 1)
        self.assertEqual(summary["tasks"], 2)

    def test_summarize_counts_rejected_results_and_action_backlog(self):
        from robert_agent.readmodels import summarize

        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        self._record_rejected_result(db_path)
        self._record_additional_publish_failure_action(db_path)
        self._record_additional_skipped_publish_action(db_path)
        self._record_pending_authorization_event(db_path)

        summary = summarize.summarize_database(db_path)

        self.assertEqual(summary["rejected_worker_results"], 1)
        self.assertEqual(summary["failed_github_actions"], 1)
        self.assertEqual(summary["pending_publish_actions"], 1)
        self.assertEqual(summary["publish_failed_actions"], 1)
        self.assertEqual(summary["skipped_publish_actions"], 1)
        self.assertEqual(summary["pending_actor_permission_events"], 1)
        self.assertEqual(summary["pending_authorization_events"], 1)
