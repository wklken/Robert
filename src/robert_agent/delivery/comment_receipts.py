"""Match comment effects by publisher and exact accepted content."""


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
