import json
import subprocess
from pathlib import Path

from robert_agent.persistence.rounds import pre_pr_review as store
from tests.support import database
from tests.support.pre_pr_review import PrePRReviewFixture


class ReviewRemote:
    def __init__(self, driver, base):
        self.scope = driver.scope
        self.base = base
        self.head = None
        self.pr = None
        self.comments = []
        self.events = []
        self.lose = None
        self.repo = self.scope["repo"]
        self.url = f"https://github.com/{self.repo}/pull/72"
        self.push_url = f"https://github.com/{self.repo}.git"

    def completed(self, args, value, *, effect=None):
        if effect:
            self.events.append(effect)
            if self.lose == effect:
                self.lose = None
                raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(value), stderr="")

    def __call__(self, args, **kwargs):
        if args[0] == "git":
            if args[1:3] == ["remote", "get-url"]:
                return subprocess.CompletedProcess(args, 0, stdout=self.push_url + "\n", stderr="")
            if args[1] == "ls-remote":
                if args[2] == "--get-url":
                    text = self.push_url + "\n"
                else:
                    ref = args[-1].removeprefix("refs/heads/")
                    head = self.base if ref == self.scope["base_branch"] else self.head
                    text = f"{head}\trefs/heads/{ref}\n" if head else ""
                return subprocess.CompletedProcess(args, 0, stdout=text, stderr="")
            if args[1] == "push":
                self.head = args[-1].split(":", 1)[0]
                if self.pr:
                    self.pr["head"]["sha"] = self.head
                return self.completed(args, {}, effect="push")
            return subprocess.run(args, **kwargs)
        if args[1:3] == ["pr", "list"]:
            return self.completed(args, [{"number": 72}] if self.pr else [])
        if args[1:3] == ["pr", "create"]:
            branch = self.scope["branch_name"]
            self.pr = {
                "head": {"ref": branch, "sha": self.head, "repo": {"full_name": self.repo}},
                "base": {"ref": self.scope["base_branch"], "sha": self.base, "repo": {"full_name": self.repo}},
                "user": {"login": self.scope["github_account"]},
                "body": args[args.index("--body") + 1],
                "title": args[args.index("--title") + 1],
                "draft": "--draft" in args,
                "number": 72,
                "html_url": self.url,
                "state": "open",
            }
            return self.completed(args, self.url, effect="open")
        if args[1:3] == ["api", "user"]:
            return self.completed(args, {"login": self.scope["github_account"]})
        if args[1:3] == ["api", f"repos/{self.repo}/pulls/72"]:
            if "PATCH" in args:
                self.pr["body"] = args[-1].removeprefix("body=")
                return self.completed(args, self.pr, effect="description")
            return self.completed(args, self.pr)
        if args[1:3] == ["api", f"repos/{self.repo}/issues/72/comments?per_page=100"]:
            return self.completed(args, [self.comments])
        if args[1:3] == ["api", f"repos/{self.repo}/issues/72/comments"]:
            body = args[-1].removeprefix("body=")
            comment = {"body": body, "id": len(self.comments) + 1, "user": self.pr["user"]}
            self.comments.append(comment)
            kind = "report" if " 第一轮评审" in body else "completion"
            return self.completed(args, comment, effect=kind)
        raise AssertionError(args[:3])


class PrePRPublicationFixture(PrePRReviewFixture):
    def setup_publication(self, workers=("claude",), *, local=False):
        driver = self.start(list(workers), legacy=False, legacy_report=False, pr_first=True, local=local)
        remote = ReviewRemote(driver, self.git_fixture.base)
        driver.publication_runner = remote
        driver.publication_config = self.config_path
        launch = driver.launcher

        def observe(**kwargs):
            name = Path(kwargs["prompt_path"]).name
            if name == "reviewer-prompt.md":
                self.assertTrue(remote.pr["draft"])
                self.assertIn("等待第一轮评审", remote.pr["body"])
                with database.connect(self.db) as conn:
                    round = store.session(conn, round_id=driver.round_id)
                    self.assertFalse(round["proof"])
                    self.assertEqual(round["request"]["review_publication"]["pr"]["number"], 72)
                remote.events.append("review")
            elif name.startswith("address-"):
                self.assertTrue(remote.comments)
                remote.events.append("repair")
            return launch(**kwargs)

        driver.launcher = observe
        return driver, remote
