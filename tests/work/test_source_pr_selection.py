"""Verify current PR facts and exact GitHub read scope independently of scheduling."""

from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from robert_agent.adapters.github import pull_requests
from robert_agent.work import source_pr_selection


def lineage(number):
    return SimpleNamespace(
        action_id=f"action-{number}",
        publication_id=f"publication-{number}",
        request_fingerprint=f"fingerprint-{number}",
        workstream_id=f"stream-{number}",
        number=number,
        url=f"https://github.com/org/alpha/pull/{number}",
        author="bot",
        repository="org/alpha",
        head_repository="bot/alpha",
        head_branch=f"topic/{number}",
        base_branch="main",
        creation_state="open",
    )


def snapshot(link):
    return {
        "number": link.number,
        "html_url": link.url,
        "state": "open",
        "merged": False,
        "merged_at": None,
        "user": {"login": "bot"},
        "head": {"repo": {"full_name": "bot/alpha"}, "ref": link.head_branch, "sha": "a" * 40},
        "base": {"repo": {"full_name": "org/alpha"}, "ref": "main", "sha": "b" * 40},
    }


class SourcePRSelectionTests(unittest.TestCase):
    def test_multiple_open_or_incomplete_observations_hold(self):
        history = (lineage(11), lineage(17))
        snaps = [snapshot(link) for link in history]
        both = source_pr_selection.normalize_observations(history, snaps, "bot")
        self.assertEqual(
            source_pr_selection.select_target(history, both, "main")[:2], ("held", "source_pr_multiple_open")
        )
        for changes in (
            {"number": 111},
            {"html_url": "https://hostile.example/org/alpha/pull/17"},
            {"user": {"login": "other"}},
            {"head": {"ref": "topic/17", "sha": "a" * 40, "repo": {"full_name": "foreign/alpha"}}},
            {"base": {"ref": "main", "sha": None, "repo": {"full_name": "org/alpha"}}},
            {"merged": None},
        ):
            with self.subTest(changes=changes):
                partial = source_pr_selection.normalize_observations(
                    history, [snaps[0], {**snaps[1], **changes}], "bot"
                )
                self.assertEqual(
                    source_pr_selection.select_target(history, partial, "main")[:2],
                    ("held", "source_pr_observation_unavailable"),
                )
        snaps[0]["base"]["ref"] = "release"
        retarget = source_pr_selection.normalize_observations(history, snaps, "bot")
        self.assertEqual(
            source_pr_selection.select_target(history, retarget, "main")[:2], ("held", "source_pr_target_retargeted")
        )

    def test_native_host_and_timeout_are_pinned_while_default_adapter_keeps_legacy_transport(self):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return SimpleNamespace(stdout='{"number":17}', stderr="", returncode=0)

        with patch.dict("os.environ", {"GH_HOST": "ghe.example", "GH_REPO": "foreign/alpha"}):
            pull_requests.read_pr("org/alpha", 17, runner=runner)
            pull_requests.read_native_prs("org/alpha", [17], runner=runner)
        ordinary, native = calls
        self.assertEqual(ordinary[0], ["gh", "api", "repos/org/alpha/pulls/17"])
        self.assertEqual(ordinary[1]["timeout"], 60)
        self.assertNotIn("env", ordinary[1])
        self.assertEqual(native[0], ["gh", "api", "--hostname", "github.com", "repos/org/alpha/pulls/17"])
        self.assertLessEqual(native[1]["timeout"], 10)
        self.assertEqual(native[1]["env"]["GH_HOST"], "github.com")
        self.assertNotIn("GH_REPO", native[1]["env"])
        stop = Event()
        stop.set()
        self.assertEqual(pull_requests.read_native_prs("org/alpha", [17], runner=runner, stop=stop), [])
        self.assertEqual(len(calls), 2)
