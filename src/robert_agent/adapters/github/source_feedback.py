"""Bounded authenticated GitHub feedback write and independent comment readback."""

import json
import os
import re
import subprocess
import time
from hashlib import sha256
from urllib.parse import parse_qsl, urlsplit

from robert_agent.adapters.github.discussion import DiscussionReader
from robert_agent.adapters.processes.bounded import BoundedCommandError, run_bounded
from robert_agent.foundation.timestamps import utc_timestamp

_OUTPUT_BYTES = 1024 * 1024
_MAX_PAGES = 10
_PAGE_SIZE = 100


class FeedbackTransport:
    def __init__(self, *, runner=None, stop=None):
        self.runner = runner or run_bounded
        self.stop = stop
        self.env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"GH_HOST", "GH_REPO", "GH_DEBUG", "GH_FORCE_TTY"}
        }
        self.env.update(GH_HOST="github.com", GH_PROMPT_DISABLED="1")
        self.deadline = min(
            time.monotonic() + 45,
            getattr(stop, "deadline", float("inf")),
            float(os.environ.get("_ROBERT_CONTROL_DEADLINE", "inf")),
        )
        self.discussion = DiscussionReader(runner=self.runner, stop=self)

    def is_set(self):
        return time.monotonic() >= self.deadline or (self.stop is not None and self.stop.is_set())

    def _request(self, endpoint, *, payload=None):
        if self.is_set():
            return None
        method = "GET" if payload is None else "POST"
        command = ["gh", "api", "--hostname", "github.com", "--include", "--method", method, endpoint]
        if payload is not None:
            command.extend(("--input", "-"))
        try:
            result = self.runner(
                command,
                cwd=None,
                env=self.env,
                input=payload,
                timeout=min(10, self.deadline - time.monotonic()),
                max_output_bytes=_OUTPUT_BYTES,
                stop=self,
            )
            if (
                self.is_set()
                or result.returncode != 0
                or not isinstance(result.stdout, bytes)
                or len(result.stdout) > _OUTPUT_BYTES
            ):
                return None
            head, body = re.split(rb"\r?\n\r?\n", result.stdout, maxsplit=1)
            status, *headers = head.splitlines()
            code = b"200" if payload is None else b"201"
            if re.fullmatch(rb"HTTP/(?:1\.1|2(?:\.0)?) " + code + rb"(?: [^\r\n]*)?", status) is None:
                return None
            return headers, json.loads(body)
        except (OSError, BoundedCommandError, subprocess.SubprocessError, ValueError, TypeError):
            return None

    def publisher_matches(self, login):
        response = self._request("user")
        user = response[1] if response else None
        observed = user.get("login") if isinstance(user, dict) else None
        return isinstance(observed, str) and observed.casefold() == login

    def post(self, packet):
        target = packet["target"]
        endpoint = f"repos/{target['repository']}/issues/{target['pr_number']}/comments"
        self._request(endpoint, payload=json.dumps({"body": packet["body"]}).encode())

    def readback(self, packet, *, repository_id=None):
        """Require a complete bounded list plus an independent exact unedited item read."""
        target = packet["target"]
        repository, number = target["repository"], target["pr_number"]
        endpoint = f"repos/{repository}/issues/{number}/comments"
        found = self._find_comment(endpoint, packet["recipe"]["marker"], repository_id=repository_id)
        if found is None or found["body"] != packet["body"]:
            return None
        external_id = str(found["id"])
        url = f"{target['pr_url']}#issuecomment-{external_id}"
        author = found.get("user")
        login = author.get("login") if isinstance(author, dict) else None
        if (
            not _same_target(
                found.get("html_url"), "github.com", repository, f"/pull/{number}", f"issuecomment-{external_id}"
            )
            or not _same_target(found.get("issue_url"), "api.github.com", repository, f"/issues/{number}")
            or not isinstance(login, str)
            or login.casefold() != packet["publisher"]
        ):
            return None
        item = self.discussion.item(
            repository=repository,
            pr_number=number,
            event_type="comment",
            item_id=external_id,
            event_url=url,
        )
        if (
            self.is_set()
            or item.status != "present"
            or item.body != packet["body"]
            or item.author.casefold() != packet["publisher"]
            or item.actor.casefold() != packet["publisher"]
            or item.edited_at is not None
            or item.created_at != item.updated_at
            or utc_timestamp(item.created_at) is None
            or item.created_at != found.get("created_at")
            or item.updated_at != found.get("updated_at")
        ):
            return None
        return {
            "external_id": external_id,
            "url": url,
            "creator": item.author.casefold(),
            "body_sha256": sha256(item.body.encode()).hexdigest(),
            "created_at": item.created_at,
        }

    def _find_comment(self, endpoint, marker, *, repository_id):
        matched = None
        last_id = 0
        for page in range(1, _MAX_PAGES + 1):
            query = f"?per_page={_PAGE_SIZE}&page={page}"
            response = self._request(endpoint + query)
            if response is None:
                return None
            headers, comments = response
            if not isinstance(comments, list) or len(comments) > _PAGE_SIZE:
                return None
            for item in comments:
                if (
                    not isinstance(item, dict)
                    or type(item.get("id")) is not int
                    or not isinstance(item.get("body"), str)
                ):
                    return None
                identifier = item["id"]
                if not last_id < identifier < 10**20:
                    return None
                last_id = identifier
                if marker in item["body"]:
                    if matched is not None:
                        return None
                    matched = item
            link = _next_page(headers, endpoint, page, repository_id)
            if link is None:
                return None
            if not link:
                return matched if len(comments) < _PAGE_SIZE else None
        return None


def _same_target(value, host, repository, suffix, fragment=""):
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    prefix = ("/repos/" if host == "api.github.com" else "/") + repository
    return (
        parsed.scheme == "https"
        and parsed.netloc == host
        and not parsed.query
        and parsed.fragment == fragment
        and parsed.path.endswith(suffix)
        and parsed.path[: -len(suffix)].casefold() == prefix.casefold()
    )


def _link_page(value, endpoint, repository_id):
    try:
        url = urlsplit(value.decode("ascii"))
        params = parse_qsl(url.query, strict_parsing=True)
    except (ValueError, UnicodeError):
        return None
    if url.scheme != "https" or url.netloc != "api.github.com" or url.fragment:
        return None
    # GitHub can express the same API resource using its numeric repository ID.
    _, _, suffix = endpoint.rpartition("/issues/")
    paths = {"/" + endpoint.casefold()}
    if type(repository_id) is int and repository_id > 0:
        paths.add(f"/repositories/{repository_id}/issues/{suffix}")
    if url.path.casefold() not in paths or len(params) != 2:
        return None
    query = dict(params)
    page = query.get("page", "")
    if query.get("per_page") != str(_PAGE_SIZE) or not re.fullmatch(r"[1-9][0-9]{0,7}", page):
        return None
    return int(page)


def _next_page(headers, endpoint, page, repository_id):
    links = [header[5:].strip() for header in headers if header[:5].lower() == b"link:"]
    if not links:
        return False
    if len(links) != 1:
        return None
    relations = {}
    for link in links[0].split(b","):
        parsed = re.fullmatch(rb'\s*<([^<>]+)>;\s*rel="(next|last|first|prev)"\s*', link)
        if parsed is None or parsed[2] in relations:
            return None
        target_page = _link_page(parsed[1], endpoint, repository_id)
        if target_page is None:
            return None
        relations[parsed[2]] = target_page
    next_page, last = relations.get(b"next"), relations.get(b"last")
    if (
        (next_page is not None and next_page != page + 1)
        or (last is not None and (last < page or (last > page) != (next_page is not None)))
        or (b"prev" in relations and (page == 1 or relations[b"prev"] != page - 1))
        or (b"first" in relations and relations[b"first"] != 1)
    ):
        return None
    return next_page is not None
