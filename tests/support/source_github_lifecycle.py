"""Canonical native PR and current discussion fixtures for TAPD integration tests."""

import json
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from robert_agent.adapters.github import pull_requests
from robert_agent.adapters.processes.source_model import SourceModelOutcome
from robert_agent.delivery import native_contract, tapd_policy
from robert_agent.delivery.application.source_github_publication import publish_github_action
from robert_agent.execution import workspace
from robert_agent.foundation.values import utc_now
from robert_agent.persistence import database
from tests.support.source_cycle import SourceCycleFixture


class SourceGitHubLifecycleFixture(SourceCycleFixture):
    def provider(self, client, ticket, **options):
        observed = super().provider(client, ticket, **options)
        return replace(
            observed,
            facts=replace(
                observed.facts,
                labels=getattr(self, "labels", observed.facts.labels),
                body=getattr(self, "ticket_body", observed.facts.body),
            ),
        )

    def accepted_development(self, *, existing=False):
        self.config(development=True)
        self.comments = [self.comment("1", suffix=" TOP-SECRET-771 https://tapd.example/internal")]
        repo = Path(self.configured["repos"][0]["repo_root"])
        self.git(repo, "init", "-b", "main")
        self.git(repo, "config", "user.name", "Repository Bot")
        self.git(repo, "config", "user.email", "repo-bot@example.com")
        self.git(repo, "remote", "add", "upstream", "https://github.com/org/alpha.git")
        self.git(repo, "remote", "add", "origin", "https://github.com/bot/alpha.git")
        self.git(repo, "add", "README.md")
        self.git(repo, "commit", "-m", "Prior existing commit")
        initial = self.git(repo, "rev-parse", "HEAD")
        self.git(repo, "update-ref", "refs/remotes/upstream/main", initial)
        self.base = initial
        self.initial_pr_head = initial if existing else None
        self.tick("admitted", ticket=self.ticket)
        target_pr = self.seed_existing_pr(initial) if existing else None
        self.tick("prepared", pr_target=lambda *_args, **_kw: target_pr)
        self.tick("dispatched")
        self.submit("classification_result", recommended_route="update-existing-pr" if existing else "new-pr")
        self.tick("waiting")
        self.tick("audited")

        def prepare_checkout(repository, purpose, *, pr=None):
            self.assertEqual(pr is not None, existing)
            branch = pr["branch_name"] if pr else f"codex/dd-{purpose['workspace_key']}-source-work"
            target = Path(repository["worktree_root"]) / "prepared"
            target.parent.mkdir(parents=True)
            self.git(repo, "worktree", "add", "-b", branch, str(target), initial)
            return {"ok": True, "worktree_path": str(target), "branch_name": branch}

        with patch.object(workspace, "prepare_source_worktree", side_effect=prepare_checkout):
            prepared = self.tick("prepared", pr_target=lambda *_args, **_kw: target_pr)
        [[tree, branch]] = self.read(
            "SELECT worktree_path,branch_name FROM attempts WHERE task_id=?", (prepared.identity,)
        )
        self.tree, self.branch = Path(tree), branch
        self.tick("dispatched")
        (self.tree / "README.md").write_text("Corrected repository behavior")
        self.git(self.tree, "add", "README.md")
        self.git(self.tree, "commit", "-m", tapd_policy.NATIVE_COMMIT_MESSAGE)
        self.head = self.git(self.tree, "rev-parse", "HEAD")
        output = "update_existing_pr" if existing else "new_pr"
        actions = native_contract.selectors_for(output)
        actions[-1]["body"] = "Updated README.md"
        self.submit(
            output,
            planned_actions=actions,
            verification=[
                {
                    "command": ["pytest"],
                    "source": "worker_reported",
                    "exit_code": 0,
                    "status": "passed",
                    "required": True,
                    "purpose": "local test",
                }
            ],
        )
        self.tick("waiting")
        self.tick("audited")
        root = self.tick("publication_pending")
        self.assertEqual(
            self.read("SELECT lifecycle FROM tasks WHERE task_id=?", (prepared.identity,)), [("completed",)]
        )
        self.root_id = root.identity
        self.action_ids = dict(
            self.read(
                "SELECT action_type,action_id FROM github_actions WHERE provider='github' AND task_id=?",
                (prepared.identity,),
            )
        )

    def seed_existing_pr(self, head):
        from robert_agent.foundation.values import utc_now
        from robert_agent.work.source_pr_selection import ExistingPRTarget

        # Model an existing linked PR from the earlier supported projection.
        # New lineage creation itself belongs to the public c2 consumer.
        now = utc_now()
        url = "https://github.com/org/alpha/pull/11"
        with closing(sqlite3.connect(self.db)) as conn, conn:
            origin, repo = "prior-origin-stream", "repo:org/alpha"
            conn.execute(
                """INSERT INTO workstreams(workstream_id,repo_id,primary_source_id,created_at,updated_at)
                    VALUES(?,?,?,?,?)""",
                (origin, repo, self.ticket.source_id, now, now),
            )
            conn.execute(
                """INSERT INTO sources(source_id,repo_id,source_key,source_type,provider,host,project_key,external_id,number,html_url)
                    VALUES('prior-pr',?,'prior-pr','pull_request','github','github.com','org/alpha','11',11,?)""",
                (repo, url),
            )
            conn.execute(
                """INSERT INTO workstreams(workstream_id,repo_id,primary_source_id,origin_workstream_id,created_at,updated_at)
                    VALUES('prior-pr-stream',?,'prior-pr',?,?,?)""",
                (repo, origin, now, now),
            )
            conn.execute(
                """INSERT INTO workstream_sources(workstream_id,source_id,relationship,created_at)
                    VALUES('prior-pr-stream','prior-pr','primary',?)""",
                (now,),
            )
            conn.execute(
                """INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at)
                    VALUES('prior-pr-task',?,'completed',?,?)""",
                (origin, now, now),
            )
            conn.execute(
                """INSERT INTO github_actions(action_id,task_id,action_type,audit_status,publish_status,created_at,target_url)
                    VALUES('prior-open','prior-pr-task','open_pr','accepted','published',?,?)""",
                (now, url),
            )
            conn.execute(
                """INSERT INTO publication_intents(publication_id,action_id,idempotency_key,request_fingerprint,status,remote_evidence_json,created_at,updated_at)
                    VALUES('prior-intent','prior-open','prior-key','prior-fingerprint','published',?,?,?)""",
                (
                    json.dumps(
                        {
                            "receipt": {
                                "action_id": "prior-open",
                                "target_url": url,
                                "external_id": "11",
                                "confirmed_at": now,
                            }
                        }
                    ),
                    now,
                    now,
                ),
            )
        return ExistingPRTarget("prior-pr-stream", "prior-open", 11, "bot/topic", head, "main", self.base, "bot/alpha")

    def runner(self, *, push_timeout=False, pr_edit=None, wrong_ack=True, publisher="bot", pr_number=17):
        calls, pulls = [], []
        state = {"sha": self.initial_pr_head}

        def handle(command, **kwargs):
            calls.append(tuple(command))
            if command[:4] == ["gh", "api", "--hostname", "github.com"]:
                path = command[4]
                if path == "user":
                    body = {"login": publisher}
                elif path == "repos/org/alpha/git/matching-refs/heads/main":
                    body = [{"ref": "refs/heads/main", "object": {"type": "commit", "sha": self.base}}]
                elif "/git/matching-refs/" in path:
                    self.assertTrue(path.startswith("repos/bot/alpha/"))
                    body = (
                        []
                        if state["sha"] is None
                        else [{"ref": f"refs/heads/{self.branch}", "object": {"type": "commit", "sha": state["sha"]}}]
                    )
                elif path in ("repos/org/alpha/pulls/11", f"repos/org/alpha/pulls/{pr_number}"):
                    number = int(path.rsplit("/", 1)[-1])
                    body = {
                        "number": number,
                        "html_url": f"https://github.com/ORG/ALPHA/pull/{number}",
                        "user": {"login": publisher},
                        "state": "open",
                        "merged": False,
                        "merged_at": None,
                        "head": {"repo": {"full_name": "BOT/ALPHA"}, "ref": self.branch, "sha": state["sha"]},
                        "base": {"repo": {"full_name": "ORG/ALPHA"}, "ref": "main", "sha": self.base},
                    }
                elif path.startswith("repos/org/alpha/pulls?"):
                    query = parse_qs(urlparse(path).query)
                    self.assertEqual(query["state"], ["all"])
                    self.assertEqual(query["head"], [f"bot:{self.branch}"])
                    body = pulls if query["page"] == ["1"] else []
                else:
                    self.fail(f"Unexpected read {command}")
                return SimpleNamespace(returncode=0, stdout=json.dumps(body), stderr="")
            if command[:2] == ["git", "push"]:
                self.assertEqual(
                    command[2], f"--force-with-lease=refs/heads/{self.branch}:{self.initial_pr_head or ''}"
                )
                self.assertEqual(command[-1], f"{self.head}:refs/heads/{self.branch}")
                self.assertEqual(
                    self.read(
                        "SELECT status FROM publication_intents WHERE action_id=?",
                        (self.action_ids["push_existing_pr"],),
                    ),
                    [("executing",)],
                )
                state["sha"] = self.head
                if push_timeout:
                    raise subprocess.TimeoutExpired(command, 20)
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if command[:3] == ["gh", "pr", "create"]:
                options = dict(zip(command[3::2], command[4::2], strict=True))
                self.assertEqual(options["--repo"], "org/alpha")
                self.assertEqual(options["--head"], f"bot:{self.branch}")
                self.assertEqual(options["--base"], "main")
                safe_text = options["--title"] + options["--body"]
                for word in ("TOP-SECRET-771", "tapd.example", "requester", self.ticket.external_id):
                    self.assertNotIn(word, safe_text)
                body = {
                    "number": pr_number,
                    "html_url": f"https://github.com/ORG/ALPHA/pull/{pr_number}",
                    "user": {"login": "bot"},
                    "title": options["--title"],
                    "body": options["--body"],
                    "head": {"repo": {"full_name": "bot/alpha"}, "ref": self.branch, "sha": self.head},
                    "base": {"repo": {"full_name": "org/alpha"}, "ref": "main"},
                    # A PR can close after a successful create and before readback.
                    "state": "closed",
                }
                pulls.append(body)
                if pr_edit:
                    pr_edit(body)
                return SimpleNamespace(
                    returncode=0,
                    stdout="https://github.com/org/other/pull/7" if wrong_ack else body["html_url"],
                    stderr="",
                )
            if command[:2] in (["git", "remote"], ["git", "ls-remote"]):
                return subprocess.run(command, **kwargs)
            self.fail(f"Unexpected command {command}")

        return calls, state, pulls, handle

    def publish(self, kind, run, **options):
        options.setdefault("owner", "test-coordinator")
        options.setdefault("observation_provider", self.provider)
        return publish_github_action(
            self.db,
            self.configured,
            source_result_id=self.root_id,
            action_id=self.action_ids[kind],
            tapd_client_factory=self.factory,
            run_command=run,
            **options,
        )

    def _tapd_publisher(self, *, unknown=False):
        from hashlib import sha256

        from robert_agent.adapters.tapd.client import TapdError
        from robert_agent.adapters.tapd.publication import TapdPublisherIdentity
        from robert_agent.delivery.application import publish
        from robert_agent.foundation.values import utc_now
        from robert_agent.work import tapd_authority as facts

        test = self
        written = []

        class MockTapd:
            def __init__(self, **_):
                pass

            def add_comment(self, workspace_id, kind, external_id, body, *, stop):
                test.assertEqual((workspace_id, kind, external_id), test.ticket.key[1:])
                used = {item.version.external_id for item in test.comments}
                identifier = str(next(number for number in range(100, 1000) if str(number) not in used))
                version = facts.CommentVersion(
                    test.ticket, "bug", identifier, "2026-11-15 00:00:00", None, sha256(body.encode()).hexdigest()
                )
                test.comments.append(
                    facts.CommentObservation(
                        version,
                        body,
                        "tapd-service",
                        facts.CreatorEvidence(version, "tapd-service", "creator-proof"),
                        facts.OwnBodyEvidence(version, "tapd-service", "body-proof"),
                    )
                )
                written.append(body)
                if unknown and len(written) == 1:
                    raise TapdError("timeout", effect_unknown=True)
                return identifier

        def identity(_client, ticket, **_):
            return TapdPublisherIdentity(ticket.host, ticket.workspace_id, "tapd-service", "account-proof", utc_now())

        def execute(**options):
            settings = {
                "provider": "tapd",
                "source_owner": "test-coordinator",
                "tapd_client_factory": MockTapd,
                "tapd_observation_provider": self.provider,
                "tapd_identity_provider": identity,
            }
            return publish.publish_ready_actions(self.db, config=self.configured, **{**settings, **options})

        return written, execute

    def _delivered_previous_pr(self):
        self.accepted_development()
        _, _, _, github = self.runner(pr_edit=lambda pr: pr.update(state="open"))
        self.assertEqual(self.publish("push_existing_pr", github).status, "published")
        self.assertEqual(self.publish("open_pr", github).status, "published")
        bodies, tapd = self._tapd_publisher()
        self.assertEqual(tapd()["native_completed_count"], 1)
        return bodies, tapd

    def _github_discussion(self, *, identifier=617, body="@bot Please inspect this change.", actor="owner", **extra):
        created = "2026-11-14T20:00:00Z"
        url = "https://github.com/org/alpha/pull/17"
        return {
            "source_type": "pull_request",
            "number": 17,
            "url": url,
            "state": "open",
            "id": identifier,
            "event_type": "comment",
            "actor_kind": "github_user",
            "actor_login": actor,
            "body": body,
            "body_observed": True,
            "body_authorization_complete": True,
            "authorization_lookup_complete": True,
            "event_at": created,
            "body_updated_at": created,
            "github_discussion_item": {
                "id": identifier,
                "url": f"{url}#issuecomment-{identifier}",
                "author": actor,
                "author_type": "User",
                "created_at": created,
                "updated_at": created,
            },
            **extra,
        }

    def _admit_github_discussion(self, raw, *, config=None, budget=1, dry_run=False, known=True):
        from robert_agent.adapters.github.discovery import normalize_events
        from robert_agent.foundation import values
        from robert_agent.security.authorization import authorize_events
        from robert_agent.work.application.intake import admit_requests

        config = self.configured if config is None else config
        repo = config["repos"][0]
        known_workstreams = {"github:org/alpha!17"} if known else set()
        decisions = authorize_events(normalize_events(raw, repo), repo, known_workstreams=known_workstreams)
        dispatch_queue = []
        with (
            closing(database.connect(self.db, foreign_keys=True)) as conn,
            conn,
            patch.object(
                workspace, "prepare_worktree", side_effect=AssertionError("ordinary GitHub dispatch")
            ) as start,
        ):
            failures = admit_requests(
                conn,
                data_dir=self.root,
                db_path=self.db,
                repo=repo,
                repo_id=values.repository_id(repo["full_name"]),
                run_id="github-ingress",
                run_now=values.utc_now(),
                dry_run=dry_run,
                dispatch_queue=dispatch_queue,
                dispatch_budget={"remaining": budget},
                prompt_paths=[],
                config_result=config,
                decisions=decisions,
                known_workstreams=known_workstreams,
                active_workstreams=set(),
                summary={"current_state_closed_skip_count": 0},
            )
        start.assert_not_called()
        self.assertEqual((failures, dispatch_queue), ([], []))

    def _current_github_runner(
        self,
        *,
        identifier=617,
        body="@bot please inspect",
        actor="owner",
        creator="owner",
        parent_status=200,
        item_status=200,
        scope_status=200,
        parent_state="open",
        base_branch="main",
        parent_edit=None,
        item_edit=None,
        during_read=None,
        edited="2026-11-14T20:01:00Z",
    ):
        calls = []
        created = "2026-11-14T20:00:00Z"
        url = "https://github.com/org/alpha/pull/17"

        def run(command, *, cwd, env, input, timeout, max_output_bytes, stop):
            self.assertEqual(command[:5], ["gh", "api", "--hostname", "github.com", "--include"])
            self.assertEqual(env["GH_HOST"], "github.com")
            self.assertNotIn("GH_REPO", env)
            self.assertLessEqual(timeout, 10)
            self.assertLessEqual(max_output_bytes, 1024 * 1024)
            endpoint = command[7]
            calls.append(endpoint)
            if during_read:
                during_read(endpoint)
            if endpoint == "repos/org/alpha/pulls/17":
                status = parent_status
                payload = {
                    "number": 17,
                    "html_url": url,
                    "state": parent_state,
                    "merged": False,
                    "merged_at": None,
                    "user": {"login": "bot"},
                    "head": {"repo": {"full_name": "bot/alpha"}, "ref": self.branch, "sha": self.head},
                    "base": {"repo": {"full_name": "org/alpha"}, "ref": base_branch, "sha": self.base},
                }
                if parent_edit:
                    parent_edit(payload)
            elif endpoint == f"repos/org/alpha/issues/comments/{identifier}":
                status = item_status
                payload = {
                    "id": identifier,
                    "node_id": f"current-comment-{identifier}",
                    "html_url": url + f"#issuecomment-{identifier}",
                    "issue_url": "https://api.github.com/repos/org/alpha/issues/17",
                    "user": {"login": creator, "type": "User"},
                    "body": body,
                    "created_at": created,
                    "updated_at": edited,
                }
                if item_edit:
                    item_edit(payload)
            elif endpoint in {
                f"repos/org/alpha/pulls/17/reviews/{identifier}",
                f"repos/org/alpha/pulls/comments/{identifier}",
            }:
                status = item_status
                review = endpoint.endswith(f"/reviews/{identifier}")
                fragment = f"pullrequestreview-{identifier}" if review else f"discussion_r{identifier}"
                payload = {
                    "id": identifier,
                    "html_url": url + "#" + fragment,
                    "pull_request_url": "https://api.github.com/repos/org/alpha/pulls/17",
                    "body": body,
                    "user": {"login": actor, "type": "User"},
                    **({"submitted_at": created} if review else {"created_at": created, "updated_at": edited}),
                }
            elif endpoint == "graphql":
                status = 200
                self.assertEqual(command[6], "POST")
                self.assertIn(f"id=current-comment-{identifier}", command)
                payload = {
                    "data": {
                        "node": {
                            "id": f"current-comment-{identifier}",
                            "body": body,
                            "author": {"login": creator},
                            "editor": {"login": actor},
                            "lastEditedAt": edited,
                            "updatedAt": edited,
                        }
                    }
                }
            elif endpoint == "repos/org/alpha/issues/17/comments?per_page=1":
                status, payload = scope_status, []
            else:
                self.fail(f"Unexpected endpoint {endpoint}")
            if status != 200:
                payload = {"message": "provider diagnostic carries no authority"}
            out = f"HTTP/2.0 {status} Result\r\nX-GitHub-Request-Id: matched\r\n\r\n" + json.dumps(payload)
            return SimpleNamespace(stdout=out.encode(), stderr=b"", returncode=int(status != 200))

        return calls, run

    def accepted_mixed_development(self, *, handle_github=True):
        self._mixed_pending_fixture()
        self.previous_root_id = self.root_id
        _, read = self._current_github_runner()
        admitted = self.tick("admitted", github_discussion_runner=read)
        self.tick("prepared", github_discussion_runner=read)
        self.tick("dispatched", github_discussion_runner=read)
        self.submit("classification_result", recommended_route="new-pr")
        self.tick("waiting", github_discussion_runner=read)
        self.tick("audited", github_discussion_runner=read)

        def current_pr(_repo, number, **_):
            return {
                "number": number,
                "html_url": "https://github.com/org/alpha/pull/17",
                "state": "open",
                "merged": False,
                "merged_at": None,
                "user": {"login": "bot"},
                "head": {"repo": {"full_name": "bot/alpha"}, "ref": self.branch, "sha": self.head},
                "base": {"repo": {"full_name": "org/alpha"}, "ref": "main", "sha": self.base},
            }

        def checkout(_repository, _purpose, *, pr):
            self.assertEqual(pr["number"], 17)
            return {"ok": True, "worktree_path": str(self.tree), "branch_name": self.branch}

        with (
            patch.object(pull_requests, "read_pr", side_effect=current_pr),
            patch.object(workspace, "prepare_source_worktree", side_effect=checkout),
        ):
            prepared = self.tick("prepared", github_discussion_runner=read)
            self.tick("dispatched", github_discussion_runner=read)
        self.initial_pr_head = self.head
        (self.tree / "README.md").write_text("A second verified local code change")
        self.git(self.tree, "add", "README.md")
        self.git(self.tree, "commit", "-m", tapd_policy.NATIVE_COMMIT_MESSAGE)
        self.head = self.git(self.tree, "rev-parse", "HEAD")
        actions = native_contract.selectors_for("update_existing_pr")
        actions[-1]["body"] = "Private Worker canary ZX991"
        handled = {}
        if not handle_github:
            handled["consumed_event_fingerprints"] = [
                row[0]
                for row in self.read(
                    "SELECT e.event_fingerprint FROM source_round_requests r JOIN request_events l USING(request_id) "
                    "JOIN source_events e USING(event_id) WHERE r.round_id=? AND e.repo_id IS NULL "
                    "ORDER BY e.event_fingerprint",
                    (admitted.round_id,),
                )
            ]
        self.submit(
            "update_existing_pr",
            planned_actions=actions,
            **handled,
            verification=[
                {
                    "command": ["pytest"],
                    "source": "worker_reported",
                    "exit_code": 0,
                    "status": "passed",
                    "required": True,
                    "purpose": "local test",
                }
            ],
        )
        self.tick("waiting", github_discussion_runner=read)
        self.tick("audited", github_discussion_runner=read)
        accepted = self.tick("publication_pending", github_discussion_runner=read)

        self.root_id = accepted.identity
        self.action_ids = dict(
            self.read(
                "SELECT a.action_type,a.action_id FROM source_required_actions r JOIN github_actions a USING(action_id) "
                "WHERE r.source_result_id=?",
                (self.root_id,),
            )
        )
        return admitted, prepared, accepted

    def prepare_mixed_model(self, identifiers=(617,), *, handle_github=True):
        self._delivered_previous_pr()
        self.labels = ()
        self.tick("idle", ticket=self.ticket)
        self._admit_github_discussion([self._github_discussion(identifier=identifier) for identifier in identifiers])
        followup = self.comment("93")
        version = replace(followup.version, created=utc_now())
        self.comments.append(
            replace(
                followup,
                version=version,
                creator=replace(followup.creator, observation=version),
                own_body=replace(followup.own_body, observation=version),
            )
        )
        readers = {identifier: self._current_github_runner(identifier=identifier)[1] for identifier in identifiers}
        selected = identifiers[0]

        def read(command, **options):
            nonlocal selected
            endpoint = command[7]
            if "/issues/comments/" in endpoint:
                selected = int(endpoint.rsplit("/", 1)[1])
            return readers[selected](command, **options)

        admitted = self.tick("admitted", github_discussion_runner=read)
        self.tick("prepared", github_discussion_runner=read)
        self.tick("dispatched", github_discussion_runner=read)
        root = Path(self.configured["repos"][0]["repo_root"])
        reference = {
            "repo_id": "repo:org/alpha",
            "path": "README.md",
            "revision": "sha256:" + sha256((root / "README.md").read_bytes()).hexdigest(),
        }
        self.submit("local_result", evidence=[reference])
        self.tick("waiting", github_discussion_runner=read)
        self.tick("audited", github_discussion_runner=read)

        def model(_profile, prompt, _stop, started):
            identity = {
                "pid": 643028,
                "process_group_id": 643028,
                "identity_source": "proc",
                "process_start_token": "mixed-delivery",
                "command_fingerprint": "b" * 64,
            }
            self.assertTrue(started(identity))
            output = json.loads(prompt)
            output.pop("prompt")
            events = self.read(
                "SELECT e.event_fingerprint FROM source_round_requests r JOIN request_events l USING(request_id) "
                "JOIN source_events e USING(event_id) WHERE r.round_id=? "
                "AND (? OR e.repo_id IS NULL) ORDER BY e.event_fingerprint",
                (admitted.round_id, int(handle_github)),
            )
            output.update(
                kind="question",
                question="Which component needs more detail? Private canary ZX991.",
                handled_event_fingerprints=[event[0] for event in events],
            )
            return SourceModelOutcome("read", json.dumps(output).encode(), "group_exited", identity)

        self.model = model
        self.tick("stage_recorded", github_discussion_runner=read)
        return admitted.round_id, read

    def _mixed_pending_fixture(self):
        from dataclasses import replace

        from robert_agent.foundation import values
        from robert_agent.persistence import transactions
        from robert_agent.persistence.queries import source_members
        from robert_agent.persistence.stores import source_sessions
        from robert_agent.work.application import source_observation, tapd_intake

        self._delivered_previous_pr()
        edited = "2026-11-14T20:01:00Z"
        github = self._github_discussion(body_updated_at=edited, body_edited_at=edited, body_editor_login="owner")
        github["github_discussion_item"]["updated_at"] = edited
        self._admit_github_discussion([github])
        followup = self.comment("2")
        version = replace(followup.version, created=values.utc_now())
        self.comments.append(
            replace(
                followup,
                version=version,
                creator=replace(followup.creator, observation=version),
                own_body=replace(followup.own_body, observation=version),
            )
        )
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            claim = source_sessions.acquire_for_owner(
                conn,
                source_id=self.ticket.source_id,
                owner="test-coordinator",
                now=values.utc_now(),
                lease_seconds=120,
            )
            observed = tapd_intake.observe_ticket(
                conn,
                self.configured["tapd"],
                self.provider(None, self.ticket),
                source_observation.publication_facts(conn, self.ticket),
                now=values.utc_now(),
            )
            owned = source_members.pending_requests(conn, self.ticket.source_id)
        return claim, observed.revision, {item.request["provider"]: item for item in owned}
