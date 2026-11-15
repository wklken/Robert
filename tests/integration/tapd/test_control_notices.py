"""Exercise fixed pre-admission TAPD notices through the actual source cycle."""

from contextlib import closing
from dataclasses import asdict, replace
from hashlib import sha256
import json
import sqlite3

from robert_agent.adapters.tapd.client import TapdError
from robert_agent.adapters.tapd.publication import TapdPublisherIdentity
from robert_agent.delivery.application import source_results
from robert_agent.delivery.application.source_publication import publish_tapd_action
from robert_agent.delivery.tapd_policy import prepare_comment
from robert_agent.foundation.values import utc_now
from robert_agent.persistence import database, execution_control, transactions
from robert_agent.persistence.queries import source_results as read_results, source_sessions as read_sessions
from robert_agent.persistence.stores import source_results as write_results, source_sessions
from robert_agent.work import tapd_authority as facts
from robert_agent.work.application import source_observation, tapd_intake

from tests.support.source_cycle import SourceCycleFixture


class ControlNoticeTests(SourceCycleFixture):
    def configure_conflict(self):
        self.config(two=True, development=True)
        project = self.configured["tapd"]["projects"][0]
        project["rules"].append({**project["rules"][0], "id": "alternate", "label": "other", "repo": "org/beta"})
        self.labels = ("robot", "other")
        self.coverage = "complete"
        self.writes = []
        self.remote_comments = []
        self.hide_remote = False

    def provider(self, client, ticket, **options):
        observed = super().provider(client, ticket, **options)
        remote = () if self.hide_remote else tuple(self.remote_comments)
        return replace(
            observed,
            facts=replace(observed.facts, labels=self.labels),
            coverage=self.coverage,
            comments=(*observed.comments, *remote),
        )

    def observe(self):
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            observed = tapd_intake.observe_ticket(
                conn,
                self.configured["tapd"],
                self.provider(None, self.ticket),
                source_observation.publication_facts(conn, self.ticket),
                now=utc_now(),
            )
            return observed

    def write_sql(self, sql, parameters=()):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(sql, parameters)

    def action(self, root):
        with closing(database.connect(self.db)) as conn:
            [action] = read_results.required_actions(conn, root)
            return action

    def publish(self, root):
        test = self

        class Client:
            def __init__(self, **_):
                pass

            def add_comment(self, workspace_id, kind, external_id, body, *, stop):
                test.assertEqual((workspace_id, kind, external_id), test.ticket.key[1:])
                test.writes.append(body)
                identifier = str(100 + len(test.writes))
                version = facts.CommentVersion(
                    test.ticket, "bug", identifier, utc_now(), None, sha256(body.encode()).hexdigest()
                )
                test.remote_comments.append(
                    facts.CommentObservation(
                        version,
                        body,
                        "controller",
                        facts.CreatorEvidence(version, "controller", "verified-creator"),
                        facts.OwnBodyEvidence(version, "controller", "verified-own-body"),
                    )
                )
                if test.hide_remote:
                    raise TapdError("write result unknown", effect_unknown=True)
                return identifier

        def publisher(_client, ticket, **_):
            return TapdPublisherIdentity(ticket.host, ticket.workspace_id, "controller", "publisher-proof", utc_now())

        return publish_tapd_action(
            self.db,
            self.configured,
            source_result_id=root,
            action_id=self.action(root)["action_id"],
            owner="test-coordinator",
            client_factory=Client,
            observation_provider=self.provider,
            identity_provider=publisher,
        )

    def prepare_without_starting(self, root):
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            action = self.action(root)
            now = utc_now()
            tapd = self.configured["tapd"]
            prepared = prepare_comment(
                json.loads(action["metadata_json"])["body"],
                "rbt-0123456789abcd",
                bot_name=tapd["bot_name"],
                reply_name=tapd["reply_name"],
            )
            packet = {
                **asdict(prepared),
                "action_id": action["action_id"],
                "target": json.loads(action["target_json"]),
                "publisher_principal": "controller",
                "publisher_identity_evidence_id": "publisher-proof",
                "publisher_observed_at": now,
            }
            intent = execution_control.prepare_publication_intent(
                conn, action["action_id"], f"tapd-comment:{action['action_id']}", facts.digest(packet), now
            )
            write_results.insert_tapd_publication_preparation(
                conn, publication_id=intent["publication_id"], evidence=packet
            )
            return intent

    def test_corrected_unstarted_conflict_notice_no_longer_blocks_initial_admission(self):
        self.configure_conflict()
        notice = self.tick("publication_pending", ticket=self.ticket)
        self.labels = ("robot",)
        self.assertEqual(self.publish(notice.identity).reason, "source_grant_unavailable")
        self.assertEqual(self.writes, [])
        admitted = self.tick("admitted", ticket=self.ticket)
        self.assertIsNotNone(admitted.round_id)
        self.assertEqual(
            self.read("SELECT publish_status FROM github_actions WHERE source_result_id=?", (notice.identity,)),
            [("skipped",)],
        )
        self.assertEqual(self.read("SELECT COUNT(*) FROM publication_intents"), [(0,)])
        self.assertEqual(self.read("SELECT COUNT(*) FROM request_events WHERE state='consumed'"), [(0,)])

    def test_prepared_obsolete_action_preserves_local_unstarted_proof_and_exact_candidate(self):
        self.configure_conflict()
        root = self.tick("publication_pending", ticket=self.ticket).identity
        intent = self.prepare_without_starting(root)
        action = self.action(root)
        self.labels = ("robot",)
        self.tick("admitted", ticket=self.ticket)
        settled = self.action(root)
        self.assertEqual(settled["publish_status"], "skipped")
        self.assertEqual(settled["intent_status"], "failed")
        self.assertIsNone(settled["intent_started_at"])
        self.assertEqual(settled["publication_id"], intent["publication_id"])
        self.assertEqual(settled["prepared_evidence_json"], action["prepared_evidence_json"])
        self.assertEqual(json.loads(settled["remote_evidence_json"]), {})
        proof = json.loads(settled["intent_error_json"])
        self.assertEqual(proof["disposition"], "local_never_started")
        self.assertEqual(proof["source_result_id"], root)
        self.assertEqual(proof["publication_id"], intent["publication_id"])
        metadata = json.loads(settled["metadata_json"])
        self.assertEqual(metadata.pop("fixed_notice_retirement"), proof)
        self.assertEqual(metadata, json.loads(action["metadata_json"]))
        self.assertEqual(self.publish(root).reason, "action_not_retryable")
        self.assertEqual(self.writes, [])

        # Altering the saved no-start proof cannot silently release the blocker.
        self.write_sql(
            "UPDATE publication_intents SET error_json='{}' WHERE publication_id=?", (intent["publication_id"],)
        )
        with closing(database.connect(self.db)) as conn:
            blockers = read_sessions.blockers_for_source(conn, self.ticket.source_id)
        self.assertIn(intent["publication_id"], [row.identity for row in blockers])

    def test_contradictory_prepared_effect_evidence_stays_a_blocker(self):
        self.configure_conflict()
        root = self.tick("publication_pending", ticket=self.ticket).identity
        intent = self.prepare_without_starting(root)
        self.write_sql(
            "UPDATE publication_intents SET remote_evidence_json=? WHERE publication_id=?",
            (json.dumps({"status": "unknown"}), intent["publication_id"]),
        )
        self.labels = ("robot",)
        self.assertEqual(self.tick("held", ticket=self.ticket).reason, "prior_source_effect_or_execution")
        self.assertEqual(self.action(root)["publish_status"], "not_published")
        self.assertEqual(self.action(root)["intent_status"], "prepared")
        self.assertEqual(self.writes, [])

    def test_authoritative_terminal_observation_remains_selectable_for_local_retirement(self):
        self.configure_conflict()
        root = self.tick("publication_pending", ticket=self.ticket).identity
        self.terminal = True
        self.coverage = "incomplete"
        self.assertEqual(self.tick("held", ticket=self.ticket).reason, "prior_source_effect_or_execution")
        self.assertEqual(self.action(root)["publish_status"], "not_published")
        self.coverage = "complete"
        self.assertEqual(self.observe().reason, "terminal")
        self.assertEqual(self.tick("idle").reason, "terminal")
        self.assertEqual(self.action(root)["publish_status"], "skipped")
        self.assertEqual(self.read("SELECT COUNT(*) FROM source_rounds"), [(0,)])
        self.assertEqual(self.writes, [])

    def test_missing_scope_and_wrong_fence_do_not_retire(self):
        self.configure_conflict()
        root = self.tick("publication_pending", ticket=self.ticket).identity
        self.labels = None
        self.assertEqual(self.tick("held", ticket=self.ticket).reason, "prior_source_effect_or_execution")
        self.assertEqual(self.action(root)["publish_status"], "not_published")
        self.labels = ("robot",)
        revision = self.observe().revision
        [[owner, token]] = self.read(
            "SELECT owner,token FROM source_sessions WHERE source_id=?", (self.ticket.source_id,)
        )
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            for claim_token, seen in ((token + 1, revision), (token, "stale")):
                self.assertEqual(
                    source_results.retire_obsolete_control_notices(
                        conn,
                        self.configured,
                        self.ticket,
                        source_sessions.SourceClaim(self.ticket.source_id, owner, claim_token),
                        revision=seen,
                        now=utc_now(),
                    ),
                    (),
                )
        self.assertEqual(self.action(root)["publish_status"], "not_published")
        self.tick("admitted", ticket=self.ticket)

    def test_repeated_conflict_and_comments_deduplicate_but_resolved_conflict_recurs(self):
        self.configure_conflict()
        root = self.tick("publication_pending", ticket=self.ticket).identity
        self.comments.append(self.comment("20", marked=False))
        self.assertEqual(self.tick("publication_pending", ticket=self.ticket).identity, root)
        self.assertEqual(self.publish(root).status, "published")
        self.assertEqual(self.tick("idle", ticket=self.ticket).reason, "repository_conflict")
        self.labels = ()
        self.tick("idle", ticket=self.ticket)
        self.labels = ("robot", "other")
        next_root = self.tick("publication_pending", ticket=self.ticket).identity
        self.assertNotEqual(next_root, root)
        self.assertEqual(self.tick("publication_pending", ticket=self.ticket).identity, next_root)
        self.assertEqual(self.publish(next_root).status, "published")
        self.assertEqual(self.action(root)["publish_status"], "published")
        self.assertEqual(self.action(root)["external_id"], "101")
        self.assertEqual(self.action(next_root)["external_id"], "102")
        self.assertEqual(len(self.writes), 2)
        for table in ("source_rounds", "worker_results", "source_model_runs", "request_events"):
            self.assertEqual(self.read(f"SELECT COUNT(*) FROM {table}"), [(0,)])

    def test_started_unknown_blocks_new_episode_until_old_positive_readback(self):
        self.configure_conflict()
        root = self.tick("publication_pending", ticket=self.ticket).identity
        self.hide_remote = True
        self.assertEqual(self.publish(root).status, "unknown")
        self.labels = ()
        self.assertEqual(self.tick("held", ticket=self.ticket).reason, "prior_source_effect_or_execution")
        self.labels = ("robot", "other")
        self.assertEqual(self.tick("held", ticket=self.ticket).reason, "prior_source_effect_or_execution")
        self.assertEqual(self.action(root)["intent_status"], "attention")
        self.assertIsNotNone(self.action(root)["intent_started_at"])
        self.assertEqual(self.read("SELECT COUNT(*) FROM source_results"), [(1,)])
        self.hide_remote = False
        self.assertEqual(self.publish(root).status, "published")
        self.assertEqual(len(self.writes), 1)
        next_root = self.tick("publication_pending", ticket=self.ticket).identity
        self.assertNotEqual(root, next_root)
        self.assertEqual(self.publish(root).status, "deduplicated")
        self.assertEqual(len(self.writes), 1)

    def test_conflict_to_paused_confirmation_retires_only_ordinary_notice(self):
        self.configure_conflict()
        self.labels = ("robot",)
        self.observe()
        self.labels = ("robot", "other")
        old = self.tick("publication_pending", ticket=self.ticket).identity
        self.labels = ("robot",)
        question = self.tick("publication_pending", ticket=self.ticket).identity
        self.assertNotEqual(old, question)
        self.assertEqual(self.action(old)["publish_status"], "skipped")
        # Identical wording alone does not make this independently owned question an ordinary notice.
        metadata = json.loads(self.action(old)["metadata_json"])
        self.write_sql(
            "UPDATE github_actions SET metadata_json=? WHERE source_result_id=?",
            (json.dumps({key: metadata[key] for key in ("body", "target_id")}), question),
        )
        self.assertEqual(self.tick("publication_pending", ticket=self.ticket).identity, question)
        self.assertEqual(self.action(question)["publish_status"], "not_published")
        self.assertEqual(self.read("SELECT phase FROM ingress_requests"), [("paused",)])

    def test_same_wording_or_corrupted_fixed_candidate_is_not_retired(self):
        self.configure_conflict()
        root = self.tick("publication_pending", ticket=self.ticket).identity
        self.write_sql("UPDATE source_results SET evidence_json='{}' WHERE source_result_id=?", (root,))
        self.labels = ("robot",)
        self.assertEqual(self.tick("held", ticket=self.ticket).reason, "prior_source_effect_or_execution")
        self.assertEqual(self.action(root)["publish_status"], "not_published")
        self.assertEqual(self.read("SELECT COUNT(*) FROM source_rounds"), [(0,)])
