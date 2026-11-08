"""Executable gh fixture contracts without Docker."""

from concurrent.futures import ThreadPoolExecutor
from http.client import RemoteDisconnected
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import time
import unittest
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from tests.simulations.compose.fixture_server import FixtureHTTPServer
from tests.simulations.compose.fixture_store import FixtureStore


_REPO = "example/repo"
_COMMENTS = f"repos/{_REPO}/issues/1/comments"
_SHIM = Path(__file__).resolve().parents[1] / "simulations" / "compose" / "fake_gh.py"


class ComposeProtocolTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        server = FixtureHTTPServer(("127.0.0.1", 0), FixtureStore(self.root / "world.sqlite3"), self.root / "git")
        self.addCleanup(server.server_close)
        self.base = f"http://127.0.0.1:{server.server_port}"
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        self.seed = {
            "account": "robot",
            "repositories": {
                _REPO: {
                    "issues": {
                        "1": {
                            "title": "Fixture task",
                            "body": "@robot analyze",
                            "user": {"login": "maintainer"},
                            "assignees": [{"login": "robot"}],
                        },
                    },
                    "permissions": {"maintainer": "write"},
                }
            },
            "notifications": [{"id": "a"}, {"id": "b"}],
            "notification_page_size": 1,
        }
        self.scenario = {"run_id": "test", "case_id": "fixture", "seed": self.seed}
        self.scenario_id = self.request("/scenario", self.scenario)["scenario_id"]

    def request(self, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = Request(self.base + path, data, headers={"Content-Type": "application/json"})
        with build_opener(ProxyHandler({})).open(req, timeout=4) as response:
            return json.load(response)

    def gh(self, *args):
        return self.request("/gh", {"argv": list(args)})

    def arm(self, operation, phase, effect=None, entity=None, seconds=3):
        return self.request(
            "/faults",
            {
                "scenario_id": self.scenario_id,
                "operation": operation,
                "phase": phase,
                "entity": entity or {},
                "effect": effect or {"kind": "hold"},
                "expires_after_seconds": seconds,
            },
        )["rule_id"]

    def checkpoint(self, rule):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            for entry in self.request("/checkpoints?after_seq=0")["checkpoints"]:
                if entry["rule_id"] == rule:
                    return entry
            time.sleep(0.01)
        self.fail(f"fixture rule never matched: {rule}")

    def release(self, checkpoint, decision):
        self.request(f"/barriers/{checkpoint['barrier_id']}/release", {"decision": decision})

    def test_cli_rejects_unhandled_shape_and_records_failure(self):
        completed = subprocess.run(
            [sys.executable, str(_SHIM), "api", _COMMENTS, "--jq", ".[0]"],
            env={**os.environ, "ROBERT_FIXTURE_URL": self.base},
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        self.assertEqual(completed.returncode, 64)
        self.assertIn("--jq", completed.stderr)
        self.assertEqual(self.request("/state")["failures"][0]["reason"], "unsupported_gh")

    def test_http_pages_slurp_and_conditional_not_modified(self):
        first = self.gh("api", "notifications?per_page=50", "--include", "--method", "GET")
        headers, data = first["stdout"].split("\r\n\r\n", 1)
        self.assertEqual(json.loads(data), [{"id": "a"}])
        link = re.search(r"Link: <([^>]+)>", headers)[1]
        marker = re.search(r"Last-Modified: ([^\r]+)", headers)[1]
        second = self.gh("api", link, "--include", "--method", "GET")
        self.assertEqual(json.loads(second["stdout"].split("\r\n\r\n", 1)[1]), [{"id": "b"}])
        self.assertEqual(
            json.loads(self.gh("api", "notifications", "--paginate", "--slurp")["stdout"]),
            [[{"id": "a"}], [{"id": "b"}]],
        )
        conditional = self.gh("api", "notifications", "--include", "-H", f"If-Modified-Since: {marker}")
        self.assertEqual(conditional["exit_code"], 1)
        self.assertTrue(conditional["stdout"].startswith("HTTP/1.1 304"))
        self.request("/seed-events", {"scenario_id": self.scenario_id, "patch": {"notifications": [{"id": "c"}]}})
        self.assertEqual(
            self.gh("api", "notifications", "--include", "-H", f"If-Modified-Since: {marker}")["exit_code"], 0
        )

    def test_search_and_permission_respect_seeded_identity(self):
        for option in (("--assignee", "@me"), ("--mentions", "robot")):
            with self.subTest(option=option):
                result = self.gh(
                    "search",
                    "issues",
                    "--repo",
                    _REPO,
                    *option,
                    "--state",
                    "open",
                    "--include-prs",
                    "--json",
                    "number,author,body",
                    "--limit",
                    "1",
                )
                self.assertEqual(
                    json.loads(result["stdout"]),
                    [{"number": 1, "author": {"login": "maintainer"}, "body": "@robot analyze"}],
                )
        permission = self.gh("api", f"repos/{_REPO}/collaborators/maintainer/permission")
        self.assertEqual(json.loads(permission["stdout"])["permission"], "write")

    def test_persisted_effect_is_visible_while_response_is_held(self):
        rule = self.arm("comment", "after_effect_commit", entity={"repo": _REPO, "number": 1})
        with ThreadPoolExecutor(2) as pool:
            pending = pool.submit(self.gh, "api", _COMMENTS, "-f", "body=first")
            point = self.checkpoint(rule)
            self.assertFalse(pending.done())
            state = self.request("/state")
            self.assertEqual(state["effects"][0]["effect_id"], point["effect_id"])
            self.assertEqual(state["effects"][0]["effect"]["body"], "first")
            self.assertEqual(state["faults"][0]["hits"], 1)
            self.release(point, "abort")
            self.assertEqual(pending.result(timeout=2)["exit_code"], 1)
        readback = self.gh("api", _COMMENTS, "--paginate", "--slurp")
        self.assertEqual([c["body"] for c in json.loads(readback["stdout"])[0]], ["first"])

    def test_concurrent_one_shot_before_effect_never_writes_after_abort(self):
        rule = self.arm("comment", "before_effect", entity={"repo": _REPO})
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.gh, "api", _COMMENTS, "-f", "body=one")
            second = pool.submit(self.gh, "api", _COMMENTS, "-f", "body=two")
            point = self.checkpoint(rule)
            self.release(point, "abort")
            results = [first.result(timeout=2), second.result(timeout=2)]
            self.assertEqual(sorted(r["exit_code"] for r in results), [0, 1])
        with self.assertRaises(HTTPError) as error:
            self.release(point, "continue")
        with error.exception as response:
            self.assertEqual(response.code, 400)
        state = self.request("/state")
        self.assertEqual(len(state["effects"]), 1)
        self.assertEqual(state["faults"][0]["hits"], 1)
        self.assertEqual(len(state["calls"]), 2)

    def test_disconnect_after_commit_does_not_roll_back(self):
        self.arm("comment", "after_effect_commit", {"kind": "disconnect"}, {"repo": _REPO})
        with self.assertRaises(RemoteDisconnected):
            self.gh("api", _COMMENTS, "-f", "body=committed")
        state = self.request("/state")
        self.assertEqual(state["calls"][0]["outcome"], {"disconnected": True})
        self.assertEqual(state["effects"][0]["effect"]["body"], "committed")

    def test_expired_hold_records_harness_failure(self):
        rule = self.arm("comment", "before_effect", seconds=0.2)
        result = self.gh("api", _COMMENTS, "-f", "body=blocked")
        self.assertEqual(result["exit_code"], 1)
        point = self.checkpoint(rule)
        state = self.request("/state")
        self.assertFalse(state["effects"])
        self.assertEqual(state["failures"], [{"reason": "barrier_expired", "barrier_id": point["barrier_id"]}])

    def test_pr_create_projects_real_git_head_and_allows_duplicate_attempt_evidence(self):
        bare = self.root / "git" / f"{_REPO}.git"
        bare.parent.mkdir(parents=True)

        def git(*args, input=None):
            return subprocess.check_output(["git", *args], input=input, text=True, timeout=5).strip()

        git("init", "--quiet", "--bare", str(bare))
        tree = git("--git-dir", str(bare), "mktree", input="")
        sha = git(
            "-c",
            "user.name=Robot",
            "-c",
            "user.email=robot@example.invalid",
            "--git-dir",
            str(bare),
            "commit-tree",
            tree,
            "-m",
            "fixture",
        )
        git("--git-dir", str(bare), "update-ref", "refs/heads/main", sha)
        git("--git-dir", str(bare), "update-ref", "refs/heads/feature", sha)
        create = [
            "pr",
            "create",
            "--repo",
            _REPO,
            "--head",
            "feature",
            "--base",
            "main",
            "--title",
            "Proposed change",
            "--body",
            "Effect body",
        ]
        first, second = self.gh(*create), self.gh(*create)
        self.assertEqual(first["stdout"].strip(), f"https://github.com/{_REPO}/pull/2")
        self.assertEqual(second["stdout"].strip(), f"https://github.com/{_REPO}/pull/3")
        response = self.gh("api", f"repos/{_REPO}/pulls/2")
        self.assertEqual(json.loads(response["stdout"])["head"]["sha"], sha)
        listed = self.gh("pr", "list", "--repo", _REPO, "--head", "feature", "--state", "open", "--json", "number")
        self.assertEqual(json.loads(listed["stdout"]), [{"number": 2}, {"number": 3}])
        viewed = self.gh("pr", "view", first["stdout"].strip(), "--repo", _REPO, "--json", "body")
        self.assertEqual(json.loads(viewed["stdout"]), {"body": "Effect body"})
