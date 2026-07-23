#!/usr/bin/env python3
import argparse
import json
import re
import subprocess
from pathlib import Path

from robert_agent.common import emit


def source_key(repo_full_name, source_type, number):
    marker = "!" if source_type == "pull_request" else "#"
    return f"github:{repo_full_name}{marker}{number}"


def event_fingerprint(event_type, event_id):
    return f"{event_type}:{event_id}"


def normalize_events(raw_events, repo_config):
    repo = repo_config["full_name"]
    github_account = repo_config["github_account"]
    normalized = []

    for raw in raw_events:
        source_type = raw.get("source_type", "issue")
        event_type = raw["event_type"]
        number = int(raw["number"])
        body = raw.get("body") or ""
        event_id = raw.get("id") or raw.get("event_id")
        normalized_event_fingerprint = raw.get("event_fingerprint")
        if not event_id and not normalized_event_fingerprint:
            raise ValueError("event id or event_fingerprint is required")
        normalized_source_key = source_key(repo, source_type, number)
        metadata = dict(raw.get("metadata") or {})
        dd_workstream = metadata.get("dd_workstream") or parse_dd_workstream_metadata(body)
        workstream_id = raw.get("workstream_id") or normalized_source_key
        origin_workstream_id = raw.get("origin_workstream_id")
        has_open_dd_pr = raw.get("has_open_dd_pr")
        if source_type == "pull_request" and dd_workstream:
            metadata["dd_workstream"] = dd_workstream
            origin_workstream_id = (
                dd_workstream.get("origin_workstream_id")
                or dd_workstream.get("workstream_id")
                or origin_workstream_id
            )
            workstream_id = normalized_source_key
            has_open_dd_pr = True
        normalized.append(
            {
                **raw,
                "repo": repo,
                "source_type": source_type,
                "number": number,
                "source_key": normalized_source_key,
                "workstream_id": workstream_id,
                "origin_workstream_id": origin_workstream_id,
                "event_fingerprint": normalized_event_fingerprint
                or event_fingerprint(event_type, event_id),
                "mentions_dd": bool(raw.get("mentions_dd") or f"@{github_account}" in body),
                "body": body,
                "metadata": metadata,
                "has_open_dd_pr": bool(has_open_dd_pr),
            }
        )

    return normalized


def _run_json(args, runner=subprocess.run):
    completed = runner(args, text=True, capture_output=True, check=True)
    stdout = completed.stdout.strip()
    if not stdout:
        return []
    return json.loads(stdout)


def _try_run_json(args, runner=subprocess.run):
    try:
        return _run_json(args, runner=runner)
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None


def _flatten_items(payload):
    if not payload:
        return []
    if isinstance(payload, dict):
        return [payload]
    items = []
    for item in payload:
        if isinstance(item, list):
            items.extend(item)
        else:
            items.append(item)
    return items


def _api_items(repo, api_path, runner):
    payload = _try_run_json(
        ["gh", "api", f"repos/{repo}/{api_path}", "--paginate", "--slurp"],
        runner=runner,
    )
    if payload is None:
        return None
    return _flatten_items(payload)


def _login(payload, key):
    value = payload.get(key)
    if isinstance(value, dict):
        return value.get("login")
    return value


def _mentions_dd(body, github_account):
    return f"@{github_account}" in (body or "")


ROBERT_WORKSTREAM_BLOCK = re.compile(r"<!--\s*robert-workstream(?P<body>.*?)-->", re.I | re.S)
LINKED_ISSUE_REFERENCE = re.compile(
    r"(?im)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|refs?|addresses?)\s+(?:(?P<repo>[\w.-]+/[\w.-]+))?#(?P<number>\d+)\b"
)


def parse_dd_workstream_metadata(body):
    match = ROBERT_WORKSTREAM_BLOCK.search(body or "")
    if not match:
        return {}
    metadata = {}
    for line in match.group("body").splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if key and value:
            metadata[key] = value
    return metadata


def infer_dd_workstream_from_pr_body(body, repo_full_name):
    for match in LINKED_ISSUE_REFERENCE.finditer(body or ""):
        referenced_repo = match.group("repo") or repo_full_name
        if referenced_repo != repo_full_name:
            continue
        issue_number = int(match.group("number"))
        return {
            "workstream_id": source_key(repo_full_name, "issue", issue_number),
            "source_issue": str(issue_number),
            "inferred_from": "pr_body_issue_reference",
        }
    return {}


def infer_intent(title, body, event_type):
    text = f"{title or ''}\n{body or ''}".lower()
    if event_type == "review_request":
        return "review_request"
    if any(word in text for word in ["bug", "fix", "修复", "报错", "失败"]):
        return "bug_fix"
    if any(
        word in text
        for word in ["分析", "analysis", "analyze", "explain", "discuss", "讨论", "方案"]
    ):
        return "analysis"
    return "unclear"


_infer_intent = infer_intent


def _issue_search_to_raw(item, event_type):
    source_type = "pull_request" if item.get("isPullRequest") else "issue"
    author = item.get("author") or {}
    body = item.get("body") or ""
    raw = {
        "number": item["number"],
        "source_type": source_type,
        "event_type": event_type,
        "actor_login": author.get("login"),
        "author_association": item.get("authorAssociation"),
        "title": item.get("title", ""),
        "body": body,
        "url": item.get("url"),
        "event_at": item.get("updatedAt"),
        "intent": _infer_intent(item.get("title", ""), body, event_type),
    }
    if item.get("id"):
        raw["id"] = item.get("id")
    else:
        raw["event_fingerprint"] = event_fingerprint(
            event_type,
            f"{item.get('url') or item.get('number')}:{item.get('updatedAt') or ''}",
        )
    return raw


def _enrich_pull_request_metadata(raw, repo_config, runner):
    if raw.get("source_type") != "pull_request":
        return raw
    repo = repo_config["full_name"]
    github_account = repo_config["github_account"]
    details = _try_run_json(["gh", "api", f"repos/{repo}/pulls/{raw['number']}"], runner=runner)
    details = details if isinstance(details, dict) else {}
    body = details.get("body") or raw.get("body") or ""
    dd_workstream = parse_dd_workstream_metadata(body)
    pr_author = _login(details, "user") or raw.get("pr_author_login")
    is_dd_authored_pr = pr_author == github_account
    if not dd_workstream and is_dd_authored_pr:
        dd_workstream = infer_dd_workstream_from_pr_body(body, repo)
    base = details.get("base") if isinstance(details.get("base"), dict) else {}
    base_ref = base.get("ref") or raw.get("baseRefName") or raw.get("base_branch")
    head = details.get("head") if isinstance(details.get("head"), dict) else {}
    head_ref = head.get("ref") or raw.get("headRefName") or raw.get("head_ref")

    if not dd_workstream and not is_dd_authored_pr:
        enriched = {**raw}
        if base_ref:
            enriched["base_branch"] = base_ref
        if pr_author:
            enriched["pr_author_login"] = pr_author
        return enriched

    metadata = dict(raw.get("metadata") or {})
    if dd_workstream:
        metadata["dd_workstream"] = dd_workstream
    enriched = {
        **raw,
        "metadata": metadata,
        "has_open_dd_pr": True,
    }
    if details:
        enriched["state"] = details.get("state") or raw.get("state")
        enriched["merged"] = bool(details.get("merged") or details.get("merged_at"))
        enriched["merged_at"] = details.get("merged_at")
    if dd_workstream:
        enriched["origin_workstream_id"] = (
            dd_workstream.get("origin_workstream_id")
            or dd_workstream.get("workstream_id")
            or raw.get("origin_workstream_id")
        )
    if head_ref:
        enriched["existing_pr_head_branch"] = head_ref
    if base_ref:
        enriched["base_branch"] = base_ref
    if pr_author:
        enriched["pr_author_login"] = pr_author
    return enriched


def _actor_permission(repo_config, actor_login, runner):
    if not actor_login:
        return None
    repo = repo_config["full_name"]
    payload = _try_run_json(
        ["gh", "api", f"repos/{repo}/collaborators/{actor_login}/permission"],
        runner=runner,
    )
    if isinstance(payload, dict):
        return payload.get("permission")
    return None


def _notification_to_raw(notification):
    subject = notification.get("subject") or {}
    repository = notification.get("repository") or {}
    repo_full_name = repository.get("full_name")
    url = subject.get("url") or ""
    match = re.search(r"/repos/(?P<repo>[^/]+/[^/]+)/(?:issues|pulls)/(?P<number>\d+)$", url)
    repo_full_name = repo_full_name or (match.group("repo") if match else None)
    if not match or not repo_full_name:
        return None
    subject_type = (subject.get("type") or "").lower()
    source_type = "pull_request" if "pull" in subject_type or "/pulls/" in url else "issue"
    return {
        "id": notification.get("id"),
        "repo_full_name": repo_full_name,
        "number": int(match.group("number")),
        "source_type": source_type,
        "event_type": "notification",
        "actor_login": "github",
        "trusted_trigger_found": False,
        "authorization_lookup_complete": False,
        "event_at": notification.get("updated_at"),
    }


def collect_account_notifications(repo_configs, runner=subprocess.run):
    configured = {repo["full_name"] for repo in repo_configs}
    buckets = {repo["full_name"]: [] for repo in repo_configs}
    pages = _run_json(
        ["gh", "api", "notifications", "--paginate", "--slurp"],
        runner=runner,
    )
    for page in pages:
        notifications = page if isinstance(page, list) else [page]
        for notification in notifications:
            raw = _notification_to_raw(notification)
            if not raw:
                continue
            repo_full_name = raw.get("repo_full_name")
            if repo_full_name not in configured:
                continue
            buckets[repo_full_name].append(raw)
    return {repo: hints for repo, hints in buckets.items() if hints}


def _source_metadata(raw, repo_config, runner):
    repo = repo_config["full_name"]
    payload = _try_run_json(["gh", "api", f"repos/{repo}/issues/{raw['number']}"], runner=runner)
    if not isinstance(payload, dict):
        return raw, None
    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    return (
        {
            **raw,
            "title": payload.get("title") or raw.get("title", ""),
            "state": payload.get("state") or raw.get("state", ""),
            "state_reason": payload.get("state_reason"),
            "source_updated_at": payload.get("updated_at"),
            "closed_at": payload.get("closed_at"),
            "url": payload.get("html_url") or raw.get("url"),
            "author_login": user.get("login") or raw.get("author_login"),
        },
        payload.get("state"),
    )


def _timeline_assignment(raw, repo_config, runner):
    repo = repo_config["full_name"]
    github_account = repo_config["github_account"]
    timeline = _api_items(repo, f"issues/{raw['number']}/timeline", runner)
    if timeline is None:
        return {
            **raw,
            "assigned_to": github_account,
            "authorization_lookup_complete": False,
        }
    for item in timeline:
        if item.get("event") != "assigned":
            continue
        if _login(item, "assignee") != github_account:
            continue
        event_id = item.get("id") or raw.get("id")
        enriched = {
            **raw,
            "id": event_id,
            "assignment_actor_login": _login(item, "actor"),
            "assigned_to": github_account,
            "authorization_lookup_complete": True,
            "event_at": item.get("created_at") or raw.get("event_at"),
        }
        if item.get("id"):
            enriched["event_fingerprint"] = event_fingerprint(
                "assigned",
                item["id"],
            )
        return enriched
    return {
        **raw,
        "assigned_to": github_account,
        "authorization_lookup_complete": True,
    }


def _timeline_review_request(raw, repo_config, runner):
    if raw.get("source_type") != "pull_request":
        return {
            **raw,
            "authorization_lookup_complete": True,
        }
    repo = repo_config["full_name"]
    github_account = repo_config["github_account"]
    timeline = _api_items(repo, f"issues/{raw['number']}/timeline", runner)
    if timeline is None:
        return {
            **raw,
            "authorization_lookup_complete": False,
        }
    for item in timeline:
        if item.get("event") != "review_requested":
            continue
        requested_reviewer = _login(item, "requested_reviewer")
        requested_team = item.get("requested_team")
        requested_team_name = None
        requested_team_slug = None
        if isinstance(requested_team, dict):
            requested_team_name = requested_team.get("name")
            requested_team_slug = requested_team.get("slug")
        if github_account not in {requested_reviewer, requested_team_name, requested_team_slug}:
            continue
        event_id = item.get("id") or raw.get("id")
        enriched = {
            **raw,
            "id": event_id,
            "event_type": "review_request",
            "requester_login": _login(item, "actor"),
            "requested_reviewer": requested_reviewer,
            "requested_team": requested_team_slug or requested_team_name,
            "authorization_lookup_complete": True,
            "event_at": item.get("created_at") or raw.get("event_at"),
            "intent": "review_request",
        }
        if item.get("id"):
            enriched["event_fingerprint"] = event_fingerprint(
                "review_request",
                item["id"],
            )
        return enriched
    return {
        **raw,
        "authorization_lookup_complete": True,
    }


def _comment_to_raw(raw, comment, event_type="comment", repo_config=None, runner=None):
    user = comment.get("user") or {}
    actor_login = user.get("login")
    association = comment.get("author_association") or comment.get("authorAssociation")
    permission = comment.get("actor_permission")
    if (
        repo_config
        and runner
        and actor_login
        and association in {None, "", "UNKNOWN"}
        and not permission
    ):
        permission = _actor_permission(repo_config, actor_login, runner)
    event_id = comment.get("id") or raw.get("id")
    enriched = {
        **raw,
        "id": event_id,
        "event_type": event_type,
        "actor_login": actor_login,
        "author_association": association,
        "actor_permission": permission,
        "body": comment.get("body") or "",
        "event_at": comment.get("created_at") or comment.get("submitted_at") or raw.get("event_at"),
    }
    if comment.get("id"):
        enriched["event_fingerprint"] = event_fingerprint(event_type, comment["id"])
    return enriched


def _raw_workstream_id(raw, repo_full_name):
    return source_key(repo_full_name, raw.get("source_type", "issue"), raw["number"])


def _discussion_items(raw, repo_config, runner):
    repo = repo_config["full_name"]
    items = []

    comments = _api_items(repo, f"issues/{raw['number']}/comments", runner)
    if comments is None:
        return None
    items.extend(("comment", comment) for comment in comments)

    if raw.get("source_type") != "pull_request":
        return items

    reviews = _api_items(repo, f"pulls/{raw['number']}/reviews", runner)
    if reviews is None:
        return None
    items.extend(("review", review) for review in reviews)

    review_comments = _api_items(repo, f"pulls/{raw['number']}/comments", runner)
    if review_comments is None:
        return None
    items.extend(("review_comment", comment) for comment in review_comments)

    return items


def _event_sort_key(event):
    return (event.get("event_at") or "", str(event.get("id") or ""))


def _latest_discussion_event(raw, repo_config, runner, predicate):
    items = _discussion_items(raw, repo_config, runner)
    if items is None:
        return None, False

    matched = []
    for event_type, item in items:
        if not predicate(item, event_type):
            continue
        enriched = _comment_to_raw(
            raw,
            item,
            event_type=event_type,
            repo_config=repo_config,
            runner=runner,
        )
        enriched["intent"] = _infer_intent(raw.get("title"), enriched.get("body"), event_type)
        matched.append(enriched)

    if not matched:
        return None, True
    return max(matched, key=_event_sort_key), True


def _enrich_mention(raw, repo_config, runner):
    github_account = repo_config["github_account"]
    trusted = set(repo_config.get("trusted_actors", []))
    body_mentions_dd = _mentions_dd(raw.get("body"), github_account)
    if body_mentions_dd and raw.get("actor_login") in trusted:
        return {
            **raw,
            "authorization_lookup_complete": True,
        }

    latest_mention, lookup_complete = _latest_discussion_event(
        raw,
        repo_config,
        runner,
        lambda item, _event_type: _mentions_dd(item.get("body"), github_account)
        and (item.get("user") or {}).get("login") in trusted,
    )
    if latest_mention:
        return {
            **latest_mention,
            "authorization_lookup_complete": True,
        }
    if not lookup_complete:
        return {
            **raw,
            "authorization_lookup_complete": False,
        }
    return {
        **raw,
        "authorization_lookup_complete": not body_mentions_dd,
    }


def _enrich_known_workstream_context(raw, repo_config, runner):
    github_account = repo_config["github_account"]
    latest_context, lookup_complete = _latest_discussion_event(
        raw,
        repo_config,
        runner,
        lambda item, _event_type: (item.get("user") or {}).get("login") != github_account
        and _mentions_dd(item.get("body"), github_account)
        and bool((item.get("body") or "").strip()),
    )
    if latest_context:
        return {
            **latest_context,
            "authorization_lookup_complete": True,
        }
    if not lookup_complete:
        return {
            **raw,
            "authorization_lookup_complete": False,
        }

    return {
        **raw,
        "authorization_lookup_complete": True,
    }


def _trusted_trigger_from_source(
    raw,
    repo_config,
    runner,
    known_workstreams=None,
):
    trusted = set(repo_config.get("trusted_actors", []))
    github_account = repo_config["github_account"]
    raw = _enrich_pull_request_metadata(raw, repo_config, runner)
    known_workstreams = set(known_workstreams or [])
    raw_workstream_id = _raw_workstream_id(raw, repo_config["full_name"])

    def _discussion_result():
        if raw_workstream_id in known_workstreams:
            context_event = _enrich_known_workstream_context(raw, repo_config, runner)
            if context_event.get("authorization_lookup_complete") is False:
                return {
                    **context_event,
                    "trusted_trigger_found": False,
                    "authorization_lookup_complete": False,
                }
            if context_event.get("id") != raw.get("id"):
                return {
                    **context_event,
                    "trusted_trigger_found": False,
                    "authorization_lookup_complete": True,
                }

        mention = _enrich_mention(raw, repo_config, runner)
        if mention.get("authorization_lookup_complete") is False:
            return {
                **mention,
                "trusted_trigger_found": False,
                "authorization_lookup_complete": False,
            }
        if _mentions_dd(mention.get("body"), github_account) and mention.get("actor_login") in trusted:
            return {
                **mention,
                "trusted_trigger_found": True,
                "authorization_lookup_complete": True,
            }

        return None

    assigned = _timeline_assignment(raw, repo_config, runner)
    if assigned.get("authorization_lookup_complete") is False:
        return {
            **raw,
            "trusted_trigger_found": False,
            "authorization_lookup_complete": False,
        }
    if assigned.get("assignment_actor_login") in trusted:
        return {
            **assigned,
            "trusted_trigger_found": True,
            "authorization_lookup_complete": True,
        }

    review_request = _timeline_review_request(raw, repo_config, runner)
    if review_request.get("authorization_lookup_complete") is False:
        return {
            **raw,
            "trusted_trigger_found": False,
            "authorization_lookup_complete": False,
        }
    if review_request.get("requester_login") in trusted:
        return {
            **review_request,
            "trusted_trigger_found": True,
            "authorization_lookup_complete": True,
        }

    discussion_result = _discussion_result()
    if discussion_result is not None:
        return discussion_result

    return {
        **raw,
        "trusted_trigger_found": False,
        "authorization_lookup_complete": True,
    }


def collect_live_events(
    repo_config,
    runner=subprocess.run,
    limit=30,
    known_workstreams=None,
    notification_hints=None,
    include_notifications=True,
):
    repo = repo_config["full_name"]
    github_account = repo_config["github_account"]
    fields = "number,title,body,author,authorAssociation,isPullRequest,updatedAt,url"
    raw_events = []
    known_workstreams = set(known_workstreams or [])

    assigned = _run_json(
        [
            "gh",
            "search",
            "issues",
            "--repo",
            repo,
            "--assignee",
            "@me",
            "--state",
            "open",
            "--include-prs",
            "--json",
            fields,
            "--limit",
            str(limit),
        ],
        runner=runner,
    )
    for item in assigned:
        raw = _issue_search_to_raw(item, "assigned")
        raw = _enrich_pull_request_metadata(raw, repo_config, runner)
        raw_events.append(_timeline_assignment(raw, repo_config, runner))

    mentions = _run_json(
        [
            "gh",
            "search",
            "issues",
            "--repo",
            repo,
            "--mentions",
            github_account,
            "--state",
            "open",
            "--include-prs",
            "--json",
            fields,
            "--limit",
            str(limit),
        ],
        runner=runner,
    )
    for item in mentions:
        raw = _issue_search_to_raw(item, "mention")
        if raw.get("author_association") in {None, "", "UNKNOWN"}:
            raw["actor_permission"] = _actor_permission(repo_config, raw.get("actor_login"), runner)
        raw = _enrich_pull_request_metadata(raw, repo_config, runner)
        if _raw_workstream_id(raw, repo) in known_workstreams:
            raw_events.append(_enrich_known_workstream_context(raw, repo_config, runner))
            continue
        raw_events.append(_enrich_mention(raw, repo_config, runner))

    notification_raws = list(notification_hints or [])
    if include_notifications and notification_hints is None:
        notification_buckets = collect_account_notifications([repo_config], runner=runner)
        notification_raws = notification_buckets.get(repo, [])

    for raw in notification_raws:
        if raw.get("repo_full_name") != repo:
            continue
        raw, source_state = _source_metadata(raw, repo_config, runner)
        if source_state and source_state.lower() != "open":
            continue
        raw_events.append(
            _trusted_trigger_from_source(
                raw,
                repo_config,
                runner,
                known_workstreams=known_workstreams,
            )
        )

    return raw_events


def build_repo_config(repo, github_account, trusted_actors=None):
    return {
        "full_name": repo,
        "github_account": github_account,
        "trusted_actors": list(trusted_actors or []),
    }


def load_fixture(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return payload
    return payload.get("events", [])


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--dd-account", required=True)
    parser.add_argument("--trusted-actor", action="append", default=[])
    parser.add_argument("--skip-external", action="store_true")
    args = parser.parse_args(argv)

    repo_config = build_repo_config(args.repo, args.github_account, args.trusted_actor)
    raw_events = []
    if args.fixture:
        raw_events = load_fixture(args.fixture)
    elif not args.skip_external:
        raw_events = collect_live_events(repo_config)

    events = normalize_events(
        raw_events,
        repo_config,
    )
    return emit({"ok": True, "status": "normalized", "events": events})


if __name__ == "__main__":
    raise SystemExit(main())
