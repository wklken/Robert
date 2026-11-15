"""Exact, uncached GitHub discussion reads and shared issue-comment attribution."""

from dataclasses import dataclass
import json
import os
import re
import subprocess
import time
from urllib.parse import urlsplit

from robert_agent.adapters.processes.bounded import BoundedCommandError, run_bounded
from robert_agent.foundation.timestamps import utc_timestamp

_PATHS = {
    "comment": ("issues/comments/{item}", "issues/{number}/comments", "issuecomment-", "issue_url", "issues"),
    "review": (
        "pulls/{number}/reviews/{item}",
        "pulls/{number}/reviews",
        "pullrequestreview-",
        "pull_request_url",
        "pulls",
    ),
    "review_comment": ("pulls/comments/{item}", "pulls/{number}/comments", "discussion_r", "pull_request_url", "pulls"),
}
_MAX_OUTPUT = 1024 * 1024


def issue_comment_fields(node_id):
    query = (
        "query($id:ID!){node(id:$id){... on IssueComment{id body author{login} editor{login} lastEditedAt updatedAt}}}"
    )
    return ["-f", f"query={query}", "-f", f"id={node_id}"]


def _login(value):
    return value.get("login") if isinstance(value, dict) else None


def issue_comment_body(item, response, *, lookup):
    """Combine REST identity and the existing GraphQL editor/body contract."""
    created, updated = item.get("created_at"), item.get("updated_at")
    actor = _login(item.get("user"))
    if not lookup:
        return item, actor, {"body_authorization_complete": not (updated and created and updated != created)}
    data = response.get("data") if isinstance(response, dict) else None
    node = data.get("node") if isinstance(data, dict) else None
    if not isinstance(node, dict) or node.get("id") != item.get("node_id") or not isinstance(node.get("body"), str):
        return item, actor, {"body_authorization_complete": False}
    author, editor = _login(node.get("author")), _login(node.get("editor"))
    actor = editor if node.get("lastEditedAt") else author
    return (
        {**item, "body": node["body"], "updated_at": node.get("updatedAt")},
        actor,
        {
            "body_authorization_complete": bool(actor),
            "body_author_login": author,
            "body_editor_login": editor,
            "body_edited_at": node.get("lastEditedAt"),
        },
    )


@dataclass(frozen=True, slots=True)
class ItemRead:
    status: str
    reason: str
    body: str | None = None
    actor: str | None = None
    author: str | None = None
    author_type: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    edited_at: str | None = None


def _same_url(actual, expected):
    if not isinstance(actual, str):
        return False
    try:
        result, intended = urlsplit(actual), urlsplit(expected)
    except ValueError:
        return False
    return (
        result.scheme == "https"
        and result.netloc == intended.netloc
        and not result.query
        and result.path.casefold() == intended.path.casefold()
        and result.fragment == intended.fragment
    )


class DiscussionReader:
    """A single bounded operation over uncached gh commands."""

    def __init__(self, *, runner=None, stop=None):
        self.runner = runner or run_bounded
        self.stop = stop
        self.env = {key: value for key, value in os.environ.items() if key not in {"GH_HOST", "GH_REPO", "GH_DEBUG"}}
        self.env.update(GH_HOST="github.com", GH_PROMPT_DISABLED="1")
        self.deadline = min(
            time.monotonic() + 30,
            getattr(stop, "deadline", float("inf")),
            float(os.environ.get("_ROBERT_CONTROL_DEADLINE", "inf")),
        )

    def _request(self, endpoint, *, arguments=(), method="GET"):
        timeout = min(10, self.deadline - time.monotonic())
        if timeout <= 0 or (self.stop is not None and self.stop.is_set()):
            return None, None
        command = ["gh", "api", "--hostname", "github.com", "--include", "--method", method, endpoint, *arguments]
        try:
            result = self.runner(
                command,
                cwd=None,
                env=self.env,
                input=None,
                timeout=timeout,
                max_output_bytes=_MAX_OUTPUT,
                stop=self.stop,
            )
            if time.monotonic() >= self.deadline or (self.stop is not None and self.stop.is_set()):
                return None, None
            payload = result.stdout
            if not isinstance(payload, bytes) or len(payload) > _MAX_OUTPUT:
                return None, None
            head, *rest = re.split(rb"\r?\n\r?\n", payload, maxsplit=1)
            status = re.fullmatch(rb"HTTP/(?:1\.1|2(?:\.0)?) ([0-9]{3})(?: [^\r\n]*)?", head.splitlines()[0])
            if not status or not rest:
                return None, None
            code = int(status[1])
            if code == 200 and result.returncode == 0 or code == 404 and result.returncode == 1:
                return code, json.loads(rest[0])
        except (IndexError, TypeError, ValueError, OSError, BoundedCommandError, subprocess.SubprocessError):
            pass
        return None, None

    def parent(self, repository, number):
        if not _coordinates(repository, number):
            return None
        status, value = self._request(f"repos/{repository}/pulls/{number}")
        return value if status == 200 and isinstance(value, dict) else None

    def item(self, *, repository, pr_number, event_type, item_id, event_url):
        """Read the exact item after the caller positively verified the parent."""
        if (
            not _coordinates(repository, pr_number)
            or event_type not in _PATHS
            or not isinstance(item_id, str)
            or not re.fullmatch(r"[1-9][0-9]{0,19}", item_id)
        ):
            return ItemRead("unavailable", "github_item_coordinates_invalid")
        path, listing, anchor, parent_field, parent_kind = _PATHS[event_type]
        expected_url = f"https://github.com/{repository}/pull/{pr_number}#{anchor}{item_id}"
        if not _same_url(event_url, expected_url):
            return ItemRead("unavailable", "github_item_coordinates_invalid")
        status, value = self._request(f"repos/{repository}/" + path.format(number=pr_number, item=item_id))
        if status == 404 and isinstance(value, dict):
            # GitHub conceals inaccessible items as 404. An exact, readable
            # parent collection with the same permission kind is also necessary.
            scope, sample = self._request(f"repos/{repository}/" + listing.format(number=pr_number) + "?per_page=1")
            if (
                scope == 200
                and isinstance(sample, list)
                and all(
                    isinstance(entry, dict) and type(entry.get("id")) is int and str(entry["id"]) != item_id
                    for entry in sample
                )
            ):
                return ItemRead("absent", "github_item_absence_confirmed")
        if status != 200 or not isinstance(value, dict):
            return ItemRead("unavailable", "github_item_read_unavailable")
        expected_parent = f"https://api.github.com/repos/{repository}/{parent_kind}/{pr_number}"
        if (
            type(value.get("id")) is not int
            or str(value["id"]) != item_id
            or not _same_url(value.get("html_url"), expected_url)
            or not _same_url(value.get(parent_field), expected_parent)
        ):
            return ItemRead("unavailable", "github_item_identity_changed")
        return self._body(event_type, value)

    def _body(self, event_type, value):
        user = value.get("user")
        author = _login(user)
        actor, edited_at = author, None
        if event_type == "comment":
            node_id = value.get("node_id")
            if not isinstance(node_id, str) or not re.fullmatch(r"[A-Za-z0-9_+/=:-]{1,200}", node_id):
                return ItemRead("unavailable", "github_item_body_authority_unavailable")
            rest_body = value.get("body")
            rest_update = utc_timestamp(value.get("updated_at"))
            status, result = self._request("graphql", arguments=issue_comment_fields(node_id), method="POST")
            value, actor, evidence = issue_comment_body(value, result, lookup=True)
            graph_update = utc_timestamp(value.get("updated_at"))
            edited_at = evidence.get("body_edited_at")
            if (
                status != 200
                or not isinstance(result, dict)
                or result.get("errors")
                or not evidence["body_authorization_complete"]
                or evidence.get("body_author_login") != author
                or rest_update is None
                or graph_update is None
                or graph_update < rest_update
                or (graph_update == rest_update and value["body"] != rest_body)
            ):
                return ItemRead("unavailable", "github_item_body_authority_unavailable")
        created = value.get("created_at") or value.get("submitted_at")
        updated = value.get("updated_at") or value.get("submitted_at")
        body = value.get("body")
        created_time, updated_time, edited_time = (
            utc_timestamp(timestamp) for timestamp in (created, updated, edited_at)
        )
        if (
            any(not isinstance(login, str) or not login for login in (author, actor))
            or not isinstance(body, str)
            or created_time is None
            or updated_time is None
            or updated_time < created_time
            or (edited_at is not None and (edited_time is None or not created_time <= edited_time <= updated_time))
        ):
            return ItemRead("unavailable", "github_item_body_authority_unavailable")
        return ItemRead(
            "present", "github_item_observed", body, actor, author, user.get("type"), created, updated, edited_at
        )


def _coordinates(repository, number):
    return (
        isinstance(repository, str)
        and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is not None
        and type(number) is int
        and number > 0
    )
