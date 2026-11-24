"""Current PR observations never hold the writer or confer authority."""

from tests.support import database
from tests.support.review_coordinator import ReviewCoordinatorFixture
from robert_agent.configuration.command import validate_config
from robert_agent.work.application import github_requests
from robert_agent.work import source_policy
from robert_agent.workflows import routing
from robert_agent.persistence.rounds import review
from robert_agent.persistence import transactions


class ReviewAdmissionTests(ReviewCoordinatorFixture):
    def test_real_injected_observation_releases_writer_then_attaches_complete_pr(self):
        self.cycle([self.event()])
        config = validate_config(self.config_path, skip_external=True)
        repo = config["repos"][0]
        event = source_policy.reauthorize_saved_request(
            self.event("injected")
            | {
                "event_fingerprint": "comment:injected",
                "source_key": "github:example/backend!42",
                "workstream_id": "github:example/backend!42",
                "body_authorization_complete": True,
            },
            repo,
        )
        with database.connect(self.db) as conn:
            transactions.begin_immediate(conn)

            def fetch(repo_name, number):
                self.assertFalse(conn.in_transaction)
                with database.connect(self.db) as writer:
                    transactions.begin_immediate(writer)
                    writer.execute("UPDATE review_sessions SET error='concurrent writer'")
                return self.pr(repo_name, number)

            observation = github_requests.observe_review_candidates(conn, repo, [event], config, fetch_pr=fetch)
            route = routing.effective_route(config, repo, event)
            github_requests.enqueue_native_request(
                conn,
                review,
                repo,
                event,
                route,
                route["native_review_workers"],
                None,
                "now",
                observed_pr=observation[event["event_fingerprint"]],
            )
            self.assertTrue(conn.in_transaction)
            self.assertEqual(conn.execute("SELECT count(*) FROM review_executions").fetchone()[0], 1)
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM review_triggers WHERE trigger_fingerprint='comment:injected'"
                ).fetchone()[0],
                1,
            )
