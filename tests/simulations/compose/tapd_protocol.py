"""TAPD CLI commands used by the scanner and publisher."""

from datetime import datetime, timedelta, timezone
import json
import re


def timestamp():
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def parse(argv):
    match = re.fullmatch(r"--workspace-id ([1-9][0-9]*) --json (.*)", " ".join(argv[:3]) + " " + "\0".join(argv[3:]))
    if not match:
        raise ValueError("expected an explicit TAPD workspace and JSON output")
    workspace, command = match.groups()
    entity = {"workspace": workspace}
    if ticket := re.fullmatch(r"(story|bug)\x00show\x00([1-9][0-9]*)\x00--no-comments", command):
        return "tapd_ticket", {**entity, "kind": ticket[1], "ticket": ticket[2]}, False
    if terminal := re.fullmatch(
        r"workflow\x00last-steps\x00--system=(story|bug)(?:\x00--workitem-type-id=[1-9][0-9]*)?", command
    ):
        return "tapd_terminal", {**entity, "kind": terminal[1]}, False
    if listing := re.fullmatch(
        r"comment\x00list\x00(?:(?:--author=([^\x00]+))|(?:--entry-id=([1-9][0-9]*)))"
        r"\x00--entry-type=(stories|bug)(\x00--order=created desc)?\x00--page\x00([1-9][0-9]*)\x00--limit\x00([1-9][0-9]*)",
        command,
    ):
        author, ticket, entry_type, order, page, size = listing.groups()
        if bool(author) != bool(order):
            raise ValueError("unexpected TAPD ordering")
        return (
            "tapd_comments",
            {
                **entity,
                "author": author,
                "ticket": ticket,
                "entry_type": entry_type,
                "page": int(page),
                "size": int(size),
            },
            False,
        )
    if addition := re.fullmatch(r"comment\x00add\x00--entry-type=(stories|bug)\x00--entry-id=([1-9][0-9]*)", command):
        return "tapd_comment", {**entity, "entry_type": addition[1], "ticket": addition[2]}, True
    raise ValueError(f"unsupported TAPD command: {argv[3:]!r}")


def _response(value=None, *, message=None):
    return {
        "stdout": json.dumps(value) if message is None else "",
        "stderr": json.dumps({"message": message}) if message else "",
        "exit_code": int(message is not None),
    }


def execute(call, world, body):
    entity = call.entity
    tapd = world["tapd"][entity["workspace"]]
    if call.operation == "tapd_terminal":
        return _response({"closed": "Closed"}), None
    if call.operation == "tapd_ticket":
        kind, ticket = entity["kind"], entity["ticket"]
        value = tapd["tickets"].get(kind, {}).get(ticket)
        if value is None:
            return _response(message=f"{kind} {ticket} not found"), None
        return _response(value), None
    comments = tapd["comments"]
    if call.operation == "tapd_comments":
        rows = [
            row
            for row in comments
            if row["entry_type"] == entity["entry_type"]
            and (entity["ticket"] is None or row["entry_id"] == entity["ticket"])
            and (entity["author"] is None or row["author"] == entity["author"])
        ]
        if entity["author"]:
            rows.sort(key=lambda row: (row["created"], int(row["id"])), reverse=True)
        page, size = entity["page"], entity["size"]
        return _response({"page": page, "items": rows[(page - 1) * size : page * size]}), None
    kind = "story" if entity["entry_type"] == "stories" else "bug"
    if entity["ticket"] not in tapd["tickets"].get(kind, {}) or not isinstance(body, str) or not body.strip():
        raise ValueError("TAPD write requires a seeded ticket and a body")
    world["_next_id"] += 1
    row = {
        "id": str(world["_next_id"]),
        "workspace_id": entity["workspace"],
        "entry_type": entity["entry_type"],
        "entry_id": entity["ticket"],
        "author": tapd["writer"],
        "description": body,
        "created": timestamp(),
    }
    comments.append(row)
    return _response({"success": True, "id": row["id"]}), row
