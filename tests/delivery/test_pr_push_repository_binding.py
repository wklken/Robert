import json
import os
from pathlib import Path
import subprocess
from unittest.mock import patch

from robert_agent.delivery.application import effects, guards, reconcile, scope
from tests.support.temp_workspace import TemporaryWorkspace


BASE_REPO = "example/backend"
FORK_REPO = "contributor/backend"
BRANCH = "codex/shared"
PR_NUMBER = 42
PR_URL = f"https://github.com/{BASE_REPO}/pull/{PR_NUMBER}"


class PRPushRepositoryBindingTests(TemporaryWorkspace):
    def setUp(self):
        super().setUp()
        self.checkout = self.root / "worker"
        self.bare = {BASE_REPO: self.root / "base.git", FORK_REPO: self.root / "fork.git"}
        self.git_env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_ALLOW_PROTOCOL": "file",
        }
        self.git("init", "-b", "main", str(self.checkout), cwd=self.root)
        self.git("config", "user.name", "Robert Test")
        self.git("config", "user.email", "robert@example.invalid")
        self.base = self.commit("base")
        self.git("switch", "-c", BRANCH)
        self.head = self.commit("PR change")
        for remote in self.bare.values():
            self.git("init", "--bare", "-b", "main", str(remote), cwd=self.root)
            self.git("push", str(remote), "main", BRANCH)
        self.candidate = self.commit("proposed repair")
        self.push_count = 0
        self.action = {
            "action_id": "action-1",
            "task_id": "task-1",
            "result_id": "result-1",
            "action_type": "push_existing_pr",
            "target_url": PR_URL,
            "metadata_json": json.dumps({"worktree_path": str(self.checkout), "remote": "origin", "branch": BRANCH}),
        }
        self.audited_scope = {
            "repo": BASE_REPO,
            "github_account": "robert-bot",
            "base_branch": "main",
            "branch_name": BRANCH,
            "remote": "origin",
            "worktree_path": str(self.checkout),
            "existing_pr_head_sha": self.head,
            "result_head_sha": self.candidate,
            "sources": [{"source_type": "pull_request", "number": PR_NUMBER}],
        }

    def git(self, *args, cwd=None):
        return subprocess.run(
            ["git", *args],
            cwd=cwd or self.checkout,
            env=self.git_env,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()

    def commit(self, contents):
        (self.checkout / "feature.txt").write_text(contents + "\n", encoding="utf-8")
        self.git("add", "feature.txt")
        self.git("commit", "-m", contents)
        return self.git("rev-parse", "HEAD")

    def tip(self, repo, branch=BRANCH):
        return self.git("--git-dir", str(self.bare[repo]), "rev-parse", f"refs/heads/{branch}")

    def assert_tips(self, base_head, fork_head):
        self.assertEqual((self.tip(BASE_REPO), self.tip(FORK_REPO)), (base_head, fork_head))
        self.assertEqual((self.tip(BASE_REPO, "main"), self.tip(FORK_REPO, "main")), (self.base, self.base))

    def runner(self, command, **kwargs):
        # The GitHub API and remote identity are simulated. Git operates on the actual local origin.
        if command == ["gh", "api", f"repos/{BASE_REPO}/pulls/{PR_NUMBER}"]:
            payload = {
                "state": "closed" if getattr(self, "closed", False) else "open",
                "merged": getattr(self, "closed", False),
                "user": {"login": "robert-bot"},
                "base": {"sha": self.base, "ref": "main", "repo": {"full_name": BASE_REPO}},
                "head": {"sha": self.tip(FORK_REPO), "ref": BRANCH, "repo": {"full_name": FORK_REPO}},
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        if command == ["git", "remote", "get-url", "--push", "--all", "origin"]:
            self.assertEqual(Path(kwargs["cwd"]), self.checkout)
            paths = self.git(*command[1:]).splitlines()
            identities = {str(path): f"https://github.com/{repo}.git" for repo, path in self.bare.items()}
            return subprocess.CompletedProcess(command, 0, "\n".join(identities[path] for path in paths) + "\n", "")
        if command[:2] in (["git", "ls-remote"], ["git", "push"]):
            self.assertEqual(Path(kwargs["cwd"]), self.checkout)
            # Translate verified GitHub URLs to our owned bare repositories.
            # A named origin intentionally retains Git's fetch/pushurl split.
            destinations = {f"https://github.com/{repo}.git": str(path) for repo, path in self.bare.items()}
            command = [destinations.get(arg, arg) for arg in command]
            if command[1] == "push":
                self.push_count += 1
            result = subprocess.run(command, env=self.git_env, timeout=10, **kwargs)
            if command[:3] == ["git", "ls-remote", "--get-url"]:
                identities = {str(path): f"https://github.com/{repo}.git" for repo, path in self.bare.items()}
                result.stdout = identities.get(result.stdout.strip(), result.stdout.strip()) + "\n"
            return result
        raise AssertionError(f"unexpected publication command: {command}")

    def publish(self):
        with patch.object(scope, "trusted_action_scope", return_value=self.audited_scope):
            return effects.publish_push_existing_pr(self.action, self.runner)

    def test_wrong_repository_refuses_push_even_when_branch_and_head_match(self):
        self.git("remote", "add", "origin", str(self.bare[BASE_REPO]))
        self.assert_tips(self.head, self.head)

        outcome = self.publish()

        self.assertFalse(outcome["ok"], outcome)
        self.assertEqual(outcome["status"], "publish_failed")
        self.assertEqual(self.push_count, 0)
        self.assert_tips(self.head, self.head)

    def test_matching_candidate_on_wrong_repository_does_not_prove_publication(self):
        self.git("remote", "add", "origin", str(self.bare[BASE_REPO]))
        self.git("push", "origin", BRANCH)
        self.assert_tips(self.candidate, self.head)

        outcome = reconcile.classify_remote_state(self.action, self.audited_scope, self.runner)
        replay = self.publish()

        self.assertEqual(outcome["status"], "unknown", outcome)
        self.assertFalse(replay["ok"], replay)
        self.assertEqual(self.push_count, 0)
        self.assert_tips(self.candidate, self.head)

    def test_split_pushurl_never_uses_fetch_repository_as_publication_proof(self):
        self.git("remote", "add", "origin", str(self.bare[BASE_REPO]))
        self.git("push", "origin", BRANCH)
        self.git("config", "remote.origin.pushurl", str(self.bare[FORK_REPO]))
        self.assert_tips(self.candidate, self.head)
        observed = reconcile.classify_remote_state(self.action, self.audited_scope, self.runner)
        self.assertNotEqual(observed["status"], "confirmed_effect", observed)
        outcome = self.publish()
        self.assertTrue(outcome["ok"], outcome)
        self.assertFalse(outcome.get("deduplicated"), outcome)
        self.assertEqual(self.push_count, 1)
        self.assert_tips(self.candidate, self.candidate)
        self.assertEqual(
            reconcile.classify_remote_state(self.action, self.audited_scope, self.runner)["status"], "confirmed_effect"
        )

    def test_explicit_pushurl_does_not_reapply_push_instead_of(self):
        self.git("remote", "add", "origin", str(self.bare[BASE_REPO]))
        self.git("config", "remote.origin.pushurl", str(self.bare[FORK_REPO]))
        self.git("config", f"url.{self.bare[BASE_REPO]}.pushInsteadOf", str(self.bare[FORK_REPO]))
        outcome = self.publish()
        self.assertTrue(outcome["ok"], outcome)
        self.assert_tips(self.head, self.candidate)

    def test_closed_pr_allows_readonly_confirmation_but_refuses_new_write(self):
        self.git("remote", "add", "origin", str(self.bare[FORK_REPO]))
        self.assertTrue(self.publish()["ok"])
        self.closed = True
        self.assertEqual(
            reconcile.classify_remote_state(self.action, self.audited_scope, self.runner)["status"], "confirmed_effect"
        )
        self.assertFalse(self.publish()["ok"])
        self.assertEqual(self.push_count, 1)

    def test_correct_fork_is_updated_and_replay_deduplicates(self):
        self.git("remote", "add", "origin", str(self.bare[FORK_REPO]))
        self.assert_tips(self.head, self.head)

        outcome = self.publish()

        self.assertTrue(outcome["ok"], outcome)
        self.assertEqual(outcome["status"], "published")
        self.assertEqual(self.push_count, 1)
        self.assert_tips(self.head, self.candidate)

        observed = reconcile.classify_remote_state(self.action, self.audited_scope, self.runner)
        self.assertEqual(observed["status"], "confirmed_effect", observed)
        replay = self.publish()
        self.assertTrue(replay["ok"], replay)
        self.assertTrue(replay["deduplicated"], replay)
        self.assertEqual(self.push_count, 1)
        self.assert_tips(self.head, self.candidate)

    def test_credential_bearing_raw_or_expanded_destination_is_rejected(self):
        safe = f"https://github.com/{FORK_REPO}.git"
        secret = f"https://fixture-user:fixture-secret@github.com/{FORK_REPO}.git"
        for raw, expanded in ((secret, safe), (safe, secret)):
            with self.subTest(raw=raw, expanded=expanded):
                calls = []

                def runner(command, **kwargs):
                    calls.append(command)
                    if command[:2] == ["gh", "api"]:
                        return self.runner(command, **kwargs)
                    value = raw if command[:3] == ["git", "remote", "get-url"] else expanded
                    return subprocess.CompletedProcess(command, 0, value + "\n", "")

                result = guards.existing_pr_push_target(self.audited_scope, runner)
                self.assertFalse(result["ok"], result)
                self.assertNotIn("fixture-secret", json.dumps(result))
                self.assertFalse(any(call[:2] == ["git", "push"] for call in calls))

    def test_read_url_rewrite_to_another_repository_is_rejected(self):
        self.git("remote", "add", "origin", str(self.bare[BASE_REPO]))
        self.git("config", "remote.origin.pushurl", "owned-alias:backend")
        # First expansion resolves the push URL to the fork. Reading that raw
        # resolved URL would then apply the second rule and reach the base.
        self.git("config", f"url.{self.bare[FORK_REPO]}.insteadOf", "owned-alias:backend")
        self.git("config", f"url.{self.bare[BASE_REPO]}.insteadOf", str(self.bare[FORK_REPO]))
        self.assertEqual(self.git("remote", "get-url", "--push", "origin"), str(self.bare[FORK_REPO]))
        result = self.publish()
        self.assertFalse(result["ok"], result)
        self.assertEqual(self.push_count, 0)
        self.assert_tips(self.head, self.head)
