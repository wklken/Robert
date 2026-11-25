"""Publish a reviewed candidate and later update that exact pull request."""

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import subprocess

from robert_agent.adapters.git.remotes import github_repository
from robert_agent.adapters.git.review_revision import clean_head, git
from robert_agent.adapters.github import client
from robert_agent.configuration.validation import validate_config
from robert_agent.delivery.application import audit, effects, guards, reviewed_pr, tapd_publication, validation
from robert_agent.delivery.lock import task_publication_lock
from robert_agent.execution.stages import StageStopped, attempt_is_active
from robert_agent.foundation.commands import run_command
from robert_agent.foundation.contracts.delivery import ReviewPublicationMode
from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.persistence import transactions
from robert_agent.persistence.rounds import pre_pr_review as store
from robert_agent.security.redaction import redact_report
from robert_agent.work.application.admission import ordinary_dispatch_allowed
from robert_agent.work.queries import tapd_origin


_OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_MAX_EFFECT_ATTEMPTS = 3


class TransientPublicationError(RuntimeError):
    """A remote command failed with a retryable transport or service error."""


def enabled(plan):
    return plan.get("review_publication") == ReviewPublicationMode.PR_FIRST_V1


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _observed_times(remote=None):
    observed = store.now()
    created = utc_timestamp((remote or {}).get("created_at"))
    return {
        "confirmed_at": observed,
        "observed_at": observed,
        "source_at": created.isoformat() if created else None,
        "precision": "provider_reported" if created else "observation_only",
    }


class Publication:
    def __init__(self, db_path, round_id, task_id, attempt_id, *, check_active, config_path, runner=None):
        self.db_path, self.round_id, self.task_id, self.attempt_id = db_path, round_id, task_id, attempt_id
        self.check_active = check_active
        self.runner = runner
        self.config_path = config_path
        self.current = None
        self.config = {}

    def _run(self, command, **kwargs):
        kwargs.setdefault("timeout", 30)
        remote = command[0] == "gh" or command[:2] in (["git", "ls-remote"], ["git", "push"])
        try:
            response = run_command(command, runner=self.runner, **kwargs)
        except (subprocess.TimeoutExpired, ConnectionError) as exc:
            if not remote:
                raise
            raise TransientPublicationError("remote publication command timed out or disconnected") from exc
        except subprocess.CalledProcessError as exc:
            if remote and self._retryable_remote_error(exc.stderr):
                raise TransientPublicationError("remote publication service temporarily unavailable") from exc
            raise
        if (
            remote
            and getattr(response, "returncode", 0)
            and self._retryable_remote_error(getattr(response, "stderr", None))
        ):
            raise TransientPublicationError("remote publication service temporarily unavailable")
        return response

    @staticmethod
    def _retryable_remote_error(error):
        text = str(error or "").casefold()
        return bool(
            re.search(r"\b(?:http|returned error:)\s*(?:429|50[0234])\b", text)
            or any(
                term in text
                for term in (
                    "connection reset",
                    "connection timed out",
                    "i/o timeout",
                    "temporarily unavailable",
                    "tls handshake timeout",
                    "unexpected eof",
                    "remote end hung up unexpectedly",
                    "could not resolve host",
                )
            )
        )

    def _entry(self, conn):
        entry = store.session(conn, round_id=self.round_id)
        if (
            not entry
            or entry["task_id"] != self.task_id
            or not enabled(entry["request"]["plan"])
            or not attempt_is_active(conn, self.task_id, self.attempt_id)
        ):
            raise StageStopped("review publication owner changed")
        return entry

    def _update(self, **changes):
        updated = {**(self.current or {}), **changes}
        self.check_active()
        with store.connect(self.db_path) as conn:
            transactions.begin(conn, immediate=True)
            entry = self._entry(conn)
            request = entry["request"]
            if request.get("review_publication") != self.current:
                raise StageStopped("review publication checkpoint changed")
            store.update(
                conn,
                self.round_id,
                request_json=json.dumps(store.review_publication_request(request, updated), sort_keys=True),
            )
        self.current = updated

    @contextmanager
    def _locked(self):
        with task_publication_lock(self.db_path, self.task_id) as locked:
            if not locked:
                raise StageStopped("review publication lock is busy")
            self._load_active()
            yield

    def _load_active(self):
        self.check_active()
        with store.connect(self.db_path) as conn:
            entry = self._entry(conn)
        self.current = entry["request"].get("review_publication")

    def _authorize(self, *, expected=None):
        self.check_active()
        config = validate_config(self.config_path, skip_external=True)
        if not config.get("ok") or Path(config["db_path"]).resolve() != Path(self.db_path).resolve():
            raise StageStopped("current review publication configuration is unavailable")
        self.config = config
        with store.connect(self.db_path) as conn:
            entry = self._entry(conn)
            settings, rejected = validation.context(conn, self.task_id, self.attempt_id, allow_completed=False)
            if rejected or not settings["action_scope"] or entry["request"].get("review_publication") != self.current:
                raise StageStopped("review publication authority changed")
            scope = settings["action_scope"]
            repo = next((repo for repo in config["repos"] if repo["full_name"] == scope["repo"]), None)
            if (
                not repo
                or not repo.get("enabled", True)
                or not config.get("daemon", {}).get("enabled", True)
                or repo["github_account"].casefold() != scope["github_account"].casefold()
                or not ordinary_dispatch_allowed(conn, self.task_id, repo, config, publishing=True)
            ):
                raise StageStopped("current source request does not authorize review publication")
            ticket = tapd_origin(conn, scope["workstream_id"])
        if expected and any(scope.get(key) != value for key, value in expected.items()):
            raise StageStopped("review publication target changed")
        return scope, settings, ticket

    def _validate(self, normalized, head, base_sha, *, expected=None):
        scope, settings, ticket = self._authorize(expected=expected)
        actual = clean_head(scope["worktree_path"], scope["branch_name"])
        if actual != head:
            raise StageStopped("review publication requires the pinned clean commit")
        scope = {**scope, "result_head_sha": head, "worktree_status": "clean", "observed_base_sha": base_sha}
        checked = audit.audit_result({**normalized, "submission_head_sha": head}, **{**settings, "action_scope": scope})
        if not checked["ok"]:
            raise StageStopped("review publication failed the submission audit")
        if ticket and tapd_publication.check(
            normalized["planned_github_actions"], scope, ticket, self.config.get("tapd"), run_command=self._run
        ):
            raise StageStopped("review publication blocked by TAPD public content policy")
        return scope

    def _bind_remote(self, scope):
        remote = git(scope["worktree_path"], "remote", "get-url", "--push", "origin", runner=self._run)
        head_repo = github_repository(remote)
        if not head_repo or head_repo.split("/")[1] != scope["repo"].casefold().split("/")[1]:
            raise StageStopped("review publication push destination does not match the repository")
        resolved = {**scope, "head_owner": head_repo.split("/")[0], "output_type": "new_pr"}
        target = guards.push_target(resolved, self._run)
        if not target or not target["ok"]:
            raise StageStopped("review publication push destination could not be verified")
        return head_repo, target["read_remote"]

    def _observe_branch(self, scope, remote):
        branch = scope["branch_name"]
        reference = f"refs/heads/{branch}"
        lines = git(scope["worktree_path"], "ls-remote", "--heads", remote, reference, runner=self._run).splitlines()
        if not lines:
            return None
        fields = lines[0].split()
        if len(lines) != 1 or len(fields) != 2 or fields[1] != reference or not _OID.fullmatch(fields[0]):
            raise StageStopped("review publication remote branch response is ambiguous")
        return fields[0]

    def _check_base(self, scope, proof):
        if proof is not None and not reviewed_pr.base_is_current({**scope, "pre_pr_review": proof}, self._run):
            raise StageStopped("review publication base changed")

    def _authorize_comment(self, stage):
        scope, settings, ticket = self._authorize(expected=self.current["authority"])
        violation = audit.managed_review_comment_violation(
            stage, self.current["pr"], scope, settings["allowed_github_actions"]
        )
        if violation:
            raise StageStopped(violation)
        return scope, ticket

    def _before_effect(self, effect, scope, *, base_proof=None, comment=False):
        self._authorize(expected=self.current["authority"])
        actor = client.read_json(["gh", "api", "user"], runner=self._run)
        if (
            not isinstance(actor, dict)
            or str(actor.get("login") or "").casefold() != scope["github_account"].casefold()
        ):
            raise StageStopped("review publication GitHub account does not match the registered publisher")
        self._check_base(scope, base_proof)
        attempts = self.current.get("attempts", {})
        count = attempts.get(effect, 0)
        overrides = self.current.get("retry_attempts", {})
        if count >= _MAX_EFFECT_ATTEMPTS:
            with store.connect(self.db_path) as conn:
                request = self._entry(conn)["request"]
            if request.get("retry_attempt_id") != self.attempt_id or overrides.get(effect) == self.attempt_id:
                raise StageStopped(f"review publication retry budget exhausted for {effect}")
            overrides = {**overrides, effect: self.attempt_id}
        # Reserve before the command. Every retry first observes the effect.
        self._update(attempts={**attempts, effect: count + 1}, retry_attempts=overrides)
        if comment:
            self._authorize_comment(effect)
        else:
            self._authorize(expected=self.current["authority"])

    def _push(self, effect, scope, head, previous, base_proof):
        head_repo, remote = self._bind_remote(scope)
        if head_repo != self.current["head_repo"]:
            raise StageStopped("review publication fork changed")
        observed = self._observe_branch(scope, remote)
        if observed != head:
            if observed != previous:
                raise StageStopped("review publication remote head changed")
            reference = f"refs/heads/{scope['branch_name']}"
            if previous:
                git(scope["worktree_path"], "merge-base", "--is-ancestor", previous, head, runner=self._run)
            self._before_effect(effect, scope, base_proof=base_proof)
            git(
                scope["worktree_path"],
                "push",
                f"--force-with-lease={reference}:{previous or ''}",
                "origin",
                f"{head}:{reference}",
                runner=self._run,
            )
            if self._observe_branch(scope, remote) != head:
                raise StageStopped("review publication push could not be confirmed")
        if not self.current.get(effect):
            self._update(**{effect: {"head_sha": head, "branch": scope["branch_name"], **_observed_times()}})

    def _pull(self, scope, number, *, expected_heads, require_marker=True):
        repo = scope["repo"]
        value = client.read_json(["gh", "api", f"repos/{repo}/pulls/{number}"], runner=self._run)
        if not isinstance(value, dict):
            raise StageStopped("review pull request response is malformed")
        head, base = value.get("head") or {}, value.get("base") or {}
        body = value.get("body")
        if (
            value.get("number") != number
            or value.get("state") != "open"
            or value.get("merged")
            or value.get("html_url") != f"https://github.com/{repo}/pull/{number}"
            or str((value.get("user") or {}).get("login") or "").casefold() != scope["github_account"].casefold()
            or str((head.get("repo") or {}).get("full_name") or "").casefold() != self.current["head_repo"]
            or str((base.get("repo") or {}).get("full_name") or "").casefold() != repo.casefold()
            or head.get("ref") != scope["branch_name"]
            or base.get("ref") != scope["base_branch"]
            or head.get("sha") not in expected_heads
            or (body is not None and not isinstance(body, str))
            or (require_marker and self.current["marker"] not in (body or ""))
        ):
            raise StageStopped("registered review pull request identity or head changed")
        return {**value, "body": body or ""}

    def _find_pull(self, scope):
        repo, branch = scope["repo"], scope["branch_name"]
        rows = client.read_json(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                repo,
                "--state",
                "all",
                "--head",
                branch,
                "--limit",
                "100",
                "--json",
                "number",
            ],
            runner=self._run,
        )
        if (
            not isinstance(rows, list)
            or len(rows) > 1
            or any(not isinstance(row, dict) or type(row.get("number")) is not int for row in rows)
        ):
            raise StageStopped("could not uniquely reconcile the candidate pull request")
        return rows[0]["number"] if rows else None

    def preliminary(self, normalized, snapshot_id, head, base_sha, *, candidate_evidence):
        with self._locked():
            if self.current is None or not self.current.get("pr"):
                scope = self._validate(normalized, head, base_sha, expected=(self.current or {}).get("authority"))
            else:
                scope, _, _ = self._authorize(expected=self.current["authority"])
            if self.current is None:
                action = next(action for action in normalized["planned_github_actions"] if action["type"] == "open_pr")
                head_repo, _ = self._bind_remote(scope)
                marker = f"<!-- robert-review-round: {self.round_id} {head} -->"
                self._update(
                    authority={
                        key: scope[key]
                        for key in ("repo", "github_account", "branch_name", "base_branch", "workstream_id")
                    },
                    authorization={"task_id": self.task_id, "attempt_id": self.attempt_id},
                    reserved_at=store.now(),
                    verification=normalized["verification"],
                    candidate_evidence=candidate_evidence,
                    snapshot_id=snapshot_id,
                    candidate_sha=head,
                    base_sha=base_sha,
                    head_repo=head_repo,
                    title=action["title"],
                    body=action["body"] + "\n\n" + marker,
                    marker=marker,
                )
            if (
                self.current["candidate_sha"] != head
                or self.current["base_sha"] != base_sha
                or self.current["snapshot_id"] != snapshot_id
                or self.current["candidate_evidence"] != candidate_evidence
                or self.current["verification"] != normalized["verification"]
            ):
                raise StageStopped("initial review publication evidence changed after reservation")
            existing = self.current.get("pr")
            if existing:
                finished = self.current.get("final_intent", {})
                self._pull(
                    scope, existing["number"], expected_heads=(head, finished.get("head_sha")), require_marker=False
                )
                return existing
            git(scope["worktree_path"], "merge-base", "--is-ancestor", base_sha, head, runner=self._run)
            base_proof = {"base_sha": base_sha, "head_sha": head}
            self._push("initial_push", scope, head, None, base_proof)
            number = self._find_pull(scope)
            if number is None:
                self._before_effect("create_pr", scope, base_proof=base_proof)
                head_repo, remote = self._bind_remote(scope)
                if head_repo != self.current["head_repo"] or self._observe_branch(scope, remote) != head:
                    raise StageStopped("candidate remote branch changed before pull request creation")
                args = [
                    "gh",
                    "pr",
                    "create",
                    "--repo",
                    scope["repo"],
                    "--head",
                    effects.head_for_pr_create(
                        scope["repo"], scope["branch_name"], self.current["head_repo"].split("/")[0]
                    ),
                    "--base",
                    scope["base_branch"],
                    "--title",
                    self.current["title"],
                    "--body",
                    self.current["body"],
                    "--draft",
                ]
                self._run(args, capture_output=True, text=True, check=True)
                number = self._find_pull(scope)
                if number is None:
                    raise StageStopped("draft pull request outcome could not be confirmed")
            pr = self._pull(scope, number, expected_heads=(head,))
            if not pr.get("draft") or _sha(pr["body"]) != _sha(self.current["body"]):
                raise StageStopped("candidate draft pull request body does not match the initial receipt")
            receipt = {
                "repo": scope["repo"],
                "number": number,
                "url": pr["html_url"],
                "branch": scope["branch_name"],
                "initial_head": head,
                "publisher": scope["github_account"],
                "workstream_id": scope["workstream_id"],
                **_observed_times(pr),
            }
            self._update(pr=receipt)
            return receipt

    def _comment(self, stage, head, body, *, base_proof=None):
        scope, ticket = self._authorize_comment(stage)
        # Bind round, stage, and the observed revision. A rebind does not
        # generate a new marker for the same public comment.
        marker = f"<!-- robert-review-comment: {self.round_id} {stage} {head} -->"
        redacted = redact_report(body)
        if not redacted["ok"] or "robert-review-comment:" in redacted["text"]:
            raise StageStopped("review comment did not pass public content checks")
        body = redacted["text"] + "\n\n" + marker
        stored = self.current.get(stage)
        digest = _sha(body)
        if stored:
            if stored["body_sha256"] != digest or stored["head_sha"] != head:
                raise StageStopped("registered review comment content changed")
            return stored
        if ticket and tapd_publication.check(
            [{"type": "comment", "body": body}], scope, ticket, self.config.get("tapd"), run_command=self._run
        ):
            raise StageStopped("review comment blocked by TAPD public content policy")
        bound = {"head_sha": head, "body_sha256": digest}
        intent_name = f"{stage}_intent"
        if self.current.get(intent_name) != bound:
            if self.current.get(intent_name):
                raise StageStopped("review comment differs from its registered intent")
            self._update(**{intent_name: bound})
        pr = self.current["pr"]
        endpoint = effects.issue_comment_endpoint(pr["url"])
        existing = effects.find_existing_comment(
            endpoint, marker, self._run, expected_body=body, publisher=scope["github_account"]
        )
        if not existing["ok"]:
            raise StageStopped("review comment outcome could not be reconciled")
        if existing["status"] == "published":
            external_id = existing["external_id"]
            response = existing["response"]["existing_comment"]
        else:
            self._before_effect(stage, scope, base_proof=base_proof, comment=True)
            self._pull(scope, pr["number"], expected_heads=(head,), require_marker=False)
            response = client.read_json(["gh", "api", endpoint, "-f", f"body={body}"], runner=self._run)
            if (
                not isinstance(response, dict)
                or response.get("body") != body
                or str((response.get("user") or {}).get("login") or "").casefold() != scope["github_account"].casefold()
                or not response.get("id")
            ):
                raise StageStopped("review comment write could not be confirmed")
            external_id = str(response["id"])
        if not external_id:
            raise StageStopped("review comment receipt has no external id")
        receipt = {**bound, "id": external_id, "target_url": pr["url"], **_observed_times(response)}
        self._update(**{stage: receipt})
        return receipt

    def report(self, reviewed_head, report_sha, report):
        with self._locked():
            if not self.current or "pr" not in self.current or self.current["candidate_sha"] != reviewed_head:
                raise StageStopped("review report has no matching initial publication")
            if _sha(report) != report_sha:
                raise StageStopped("review report changed before publication")
            scope, _, _ = self._authorize(expected=self.current["authority"])
            head = self.current.get("final_intent", {}).get("head_sha") if "report" in self.current else None
            self._pull(scope, self.current["pr"]["number"], expected_heads=(reviewed_head, head), require_marker=False)
            body = f"## 第一轮评审\n\n送审提交 `{reviewed_head}`\n\n{report}"
            return self._comment("report", reviewed_head, body)

    def waiting(self, reviewed_head, summary):
        with self._locked():
            scope, _, _ = self._authorize(expected=self.current["authority"])
            self._pull(scope, self.current["pr"]["number"], expected_heads=(reviewed_head,), require_marker=False)
            return self._comment("waiting", reviewed_head, summary)

    def blocked(self, reviewed_head, summary):
        with self._locked():
            scope, _, _ = self._authorize(expected=self.current["authority"])
            self._pull(scope, self.current["pr"]["number"], expected_heads=(reviewed_head,), require_marker=False)
            return self._comment("blocked", reviewed_head, summary)

    def finish_locked(self, normalized, head, base_proof, *, report_sha, dispositions_sha, summary):
        """Complete GitHub effects while the coordinator owns the publication lock."""
        self._load_active()
        if not self.current or "report" not in self.current:
            raise StageStopped("final review publication requires the first-round report receipt")
        scope = self._validate(normalized, head, base_proof["base_sha"], expected=self.current["authority"])
        self._check_base(scope, base_proof)
        draft = next(action for action in normalized["planned_github_actions"] if action["type"] == "open_pr")
        intent = {
            "head_sha": head,
            "report_sha256": report_sha,
            "dispositions_sha256": dispositions_sha,
            "body_sha256": _sha(draft["body"]),
        }
        if self.current.get("final_intent") != intent:
            if self.current.get("final_intent"):
                raise StageStopped("final review publication evidence changed")
            self._update(final_intent=intent)
        pr = self.current["pr"]
        self._pull(scope, pr["number"], expected_heads=(pr["initial_head"], head), require_marker=False)
        self._push("final_push", scope, head, pr["initial_head"], base_proof)
        self._description(scope, pr["number"], head, draft["body"], base_proof)
        self._comment("completion", head, summary, base_proof=base_proof)
        self._pull(scope, pr["number"], expected_heads=(head,), require_marker=False)
        self._check_base(scope, base_proof)
        final = {
            "initial": self.current["pr"],
            **intent,
            "push": self.current["final_push"],
            "report": self.current["report"],
            "completion": self.current["completion"],
            "description": self.current["description"],
        }
        if self.current.get("complete") != final:
            self._update(complete=final)
        return final

    def _description(self, scope, number, head, body, base_proof):
        if self.current.get("description"):
            return
        pr = self._pull(scope, number, expected_heads=(head,), require_marker=False)
        old = pr["body"]
        final_body = body + "\n\n" + self.current["marker"]
        # Respect edits by maintainers, including their changes to ordinary
        # metadata. The final report and dispositions still go to comments.
        outcome = "maintainer_edited"
        if old == final_body:
            outcome = "updated"
        elif old == self.current["body"]:
            self._before_effect("description", scope, base_proof=base_proof)
            pr = self._pull(scope, number, expected_heads=(head,), require_marker=False)
            if pr["body"] != old:
                raise StageStopped("pull request description changed before update")
            self._run(
                ["gh", "api", f"repos/{scope['repo']}/pulls/{number}", "--method", "PATCH", "-f", f"body={final_body}"],
                capture_output=True,
                text=True,
                check=True,
            )
            pr = self._pull(scope, number, expected_heads=(head,), require_marker=False)
            if pr["body"] != final_body:
                raise StageStopped("final pull request description could not be confirmed")
            outcome = "updated"
        self._update(description={"status": outcome, "body_sha256": _sha(pr["body"]), "confirmed_at": store.now()})
