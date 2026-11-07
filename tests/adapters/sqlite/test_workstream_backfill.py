from contextlib import closing
import sqlite3
from tests.support.repository_cycle import (
    RepositoryCycleFixture,
)


class WorkstreamBackfillTests(RepositoryCycleFixture):
    def test_storage_backfills_legacy_pr_task_into_pr_workstream(self):
        from robert_agent.adapters.sqlite import database as storage

        db_path = self.data_dir / "dd.sqlite3"
        storage.init_database(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            # This fixture represents a database that predates the mainline migration.
            conn.execute(
                "DELETE FROM schema_migrations WHERE name=?",
                (storage.PR_MAINLINE_MIGRATION_NAME,),
            )
            conn.execute(
                """
                INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root)
                VALUES ('repo:example/backend', 'example/backend', 'robert-bot', 'master', '/tmp/repo', '/tmp/repo/.worktrees')
                """
            )
            conn.execute(
                """
                INSERT INTO github_sources(source_id, repo_id, source_key, source_type, number, title, state, author_login)
                VALUES ('source:github:example/backend#123', 'repo:example/backend', 'github:example/backend#123', 'issue', 123, '', 'open', 'wklken')
                """
            )
            conn.execute(
                """
                INSERT INTO github_sources(source_id, repo_id, source_key, source_type, number, title, state, author_login)
                VALUES ('source:github:example/backend!456', 'repo:example/backend', 'github:example/backend!456', 'pull_request', 456, '', 'open', 'robert-bot')
                """
            )
            conn.execute(
                """
                INSERT INTO workstreams(workstream_id, repo_id, primary_source_id, lifecycle, active_task_id, created_at, updated_at)
                VALUES ('github:example/backend#123', 'repo:example/backend', 'source:github:example/backend#123', 'active', 'task-pr-followup', '2026-06-17T00:00:00+00:00', '2026-06-17T00:00:00+00:00')
                """
            )
            conn.execute(
                """
                INSERT INTO github_events(event_id, repo_id, source_id, event_fingerprint, event_type, actor_login, author_association, authorization_status, event_at, payload_json)
                VALUES ('event:comment:legacy', 'repo:example/backend', 'source:github:example/backend!456', 'comment:legacy', 'comment', 'wklken', 'COLLABORATOR', 'accepted_context', '2026-06-17T00:00:00Z', '{"workstream_id": "github:example/backend!456", "origin_workstream_id": "github:example/backend#123"}')
                """
            )
            conn.execute(
                """
                INSERT INTO tasks(task_id, workstream_id, lifecycle, priority, route_id, expected_output, created_at, updated_at)
                VALUES ('task-pr-followup', 'github:example/backend#123', 'queued', 'P1', 'update-existing-pr', 'update_existing_pr', '2026-06-17T00:00:00+00:00', '2026-06-17T00:00:00+00:00')
                """
            )
            conn.execute(
                """
                INSERT INTO task_events(task_id, event_id, relationship, created_at)
                VALUES ('task-pr-followup', 'event:comment:legacy', 'trigger', '2026-06-17T00:00:00+00:00')
                """
            )

        storage.init_database(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            row = conn.execute(
                "SELECT workstream_id FROM tasks WHERE task_id = 'task-pr-followup'"
            ).fetchone()
        self.assertEqual(row, ("github:example/backend!456",))
