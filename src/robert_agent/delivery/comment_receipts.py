"""Match comment effects by publisher and exact accepted content."""

import json


def comment_records(stdout):
    """Flatten a paginated comment listing, or return None when its shape proves nothing about absence."""
    try:
        listing = json.loads(stdout)
    except (TypeError, ValueError, RecursionError):
        return None
    if not isinstance(listing, list):
        return None
    records = []
    for page in listing:
        for item in page if isinstance(page, list) else [page]:
            if not isinstance(item, dict) or not isinstance(item.get("body"), str):
                return None
            comment_id = item.get("id")
            if type(comment_id) is not int or comment_id <= 0:
                return None
            user = item.get("user")
            if user is not None:
                login = user.get("login") if isinstance(user, dict) else None
                if not isinstance(login, str) or not login or any(char.isspace() for char in login):
                    return None
            records.append(item)
    return records


def match_comment(comments, expected_body, publisher, marker):
    matches = []
    for comment in comments:
        body = comment.get("body")
        if not isinstance(body, str) or not marker or marker not in body:
            continue
        actor = (comment.get("user") or {}).get("login")
        if not publisher or not actor:
            return {"status": "unknown", "reason_code": "comment_author_unavailable"}
        if actor.casefold() != publisher.casefold():
            continue
        if body != expected_body:
            return {"status": "unknown", "reason_code": "comment_content_conflict"}
        matches.append(comment)
    if len(matches) > 1:
        return {"status": "unknown", "reason_code": "comment_marker_conflict"}
    if matches:
        return {"status": "confirmed_effect", "reason_code": "comment_content_and_author_match", "comment": matches[0]}
    return {"status": "confirmed_absent", "reason_code": "comment_lookup_complete_absent"}
