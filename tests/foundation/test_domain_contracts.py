"""Value contracts must retain the installed schemas and wire encodings."""

from contextlib import closing
from enum import StrEnum
import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

import yaml
from sqlalchemy import create_engine, select

from robert_agent.foundation.contracts import delivery, execution, supervision, work
from robert_agent.persistence.expressions import sql_constant
from robert_agent.persistence.schema.database import init_database
from robert_agent.resource_files import resource


class DomainContractTests(unittest.TestCase):
    def test_domain_values_match_existing_database_constraints(self):
        contracts = {
            ("tasks", "lifecycle"): work.TaskLifecycle,
            ("tasks", "task_kind"): work.TaskKind,
            ("workstreams", "lifecycle"): work.WorkstreamLifecycle,
            ("attempts", "status"): execution.AttemptStatus,
            ("wakeups", "status"): work.WakeupStatus,
            ("pr_remediation_episodes", "status"): work.RemediationStatus,
            ("github_actions", "audit_status"): delivery.AuditStatus,
            ("github_actions", "publish_status"): delivery.PublishStatus,
            ("publication_intents", "status"): delivery.PublicationStatus,
            ("execution_subjects", "binding_status"): execution.BindingStatus,
            ("supervision_incidents", "status"): supervision.IncidentStatus,
            ("supervision_recovery_actions", "status"): supervision.RecoveryStatus,
        }
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "contracts.sqlite3"
            init_database(database)
            with closing(sqlite3.connect(database)) as conn:
                ddl = dict(conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'"))
        for (table, column), enum in contracts.items():
            with self.subTest(table=table, column=column):
                match = re.search(rf"\b{column}\s+IN\s*\(([^)]+)\)", ddl[table], re.I)
                self.assertIsNotNone(match)
                stored_values = set(re.findall(r"'([^']+)'", match[1]))
                self.assertEqual({member.value for member in enum}, stored_values)

    def test_domain_values_match_existing_worker_schemas(self):
        snapshot = json.loads(resource("schemas", "snapshot.schema.json").read_text())
        result = json.loads(resource("schemas", "result.schema.json").read_text())["properties"]
        evaluation = result["review_point_evaluation"]["items"]["properties"]
        contracts = (
            (execution.WorkerPhase, snapshot["properties"]["phase"]["enum"]),
            (delivery.QuestionKind, result["operator_question"]["properties"]["kind"]["enum"]),
            (delivery.VerificationStatus, result["verification"]["items"]["properties"]["status"]["enum"]),
            (delivery.ReviewVerdict, evaluation["verdict"]["enum"]),
            (delivery.ReviewAction, evaluation["action"]["enum"]),
            (
                delivery.GitHubActionType,
                [item["properties"]["type"]["const"] for item in result["planned_github_actions"]["items"]["oneOf"]],
            ),
        )
        for enum, values in contracts:
            with self.subTest(enum=enum.__name__):
                self.assertEqual({member.value for member in enum}, set(values))

    def test_values_keep_plain_string_json_yaml_and_sql_encodings(self):
        engine = create_engine("sqlite://")
        try:
            with engine.connect() as conn:
                for module in (work, execution, delivery, supervision):
                    for enum in vars(module).values():
                        if not isinstance(enum, type) or not issubclass(enum, StrEnum) or enum is StrEnum:
                            continue
                        for member in enum:
                            with self.subTest(enum=enum.__name__, member=member.name):
                                self.assertIs(type(member.value), str)
                                self.assertEqual(str(member), member.value)
                                self.assertEqual(json.loads(json.dumps({"value": member})), {"value": member.value})
                                self.assertEqual(yaml.safe_load(yaml.safe_dump(member.value)), member.value)
                                row = conn.execute(select(sql_constant(member.value))).scalar_one()
                                self.assertIs(type(row), str)
                                self.assertEqual(row, member.value)
                                self.assertEqual(enum(row), member)
                        with self.assertRaises(ValueError):
                            enum("unknown-domain-value")
        finally:
            engine.dispose()

    def test_task_activity_and_terminal_groups_retain_distinct_meanings(self):
        # Waiting for a user is live for ownership, but not actively executing.
        self.assertNotIn("waiting_for_user", work.ACTIVE_TASK_LIFECYCLES)
        self.assertIn("waiting_for_user", work.LIVE_TASK_LIFECYCLES)
        self.assertEqual(work.TERMINAL_TASK_LIFECYCLES, {"completed", "failed", "canceled", "ignored"})
        self.assertFalse(work.LIVE_TASK_LIFECYCLES & work.TERMINAL_TASK_LIFECYCLES)
        self.assertEqual(work.LIVE_TASK_LIFECYCLES | work.TERMINAL_TASK_LIFECYCLES, set(work.TaskLifecycle))
