#!/usr/bin/env python3
import argparse
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

from robert_agent.common import emit, run_command


SOURCE_CONTEXT_VERSION = 3


def _body_context(raw, *, legacy_empty_unknown=False):
    # Older normalized payloads defaulted missing bodies to an unmarked "".
    default_observed = bool(raw.get("body")) if legacy_empty_unknown else True
    observed = "body" in raw and raw.get("body_observed", default_observed) is True
    updated_at = raw.get("body_updated_at")
    if observed and not updated_at:
        updated_at = (raw.get("source_updated_at") if raw.get("event_type") in {"assigned", "notification"}
                      else raw.get("event_at"))
    return {"body": raw.get("body") or "", "body_observed": observed,
            "body_updated_at": updated_at if observed else None}


def _body_time(context):
    try:
        value = datetime.fromisoformat(context["body_updated_at"].replace("Z", "+00:00"))
        return value if value.tzinfo is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def merge_event_body(preferred, duplicate):
    """Merge body observations without changing the preferred event's authority."""
    if (preferred.get("source_key") != duplicate.get("source_key")
            or preferred.get("event_fingerprint") != duplicate.get("event_fingerprint")
            or preferred.get("authorization_status") != duplicate.get("authorization_status")):
        return preferred
    current = _body_context(preferred, legacy_empty_unknown=True)
    candidate = _body_context(duplicate, legacy_empty_unknown=True)
    current_time, candidate_time = _body_time(current), _body_time(candidate)
    use_candidate = candidate["body_observed"] and (
        not current["body_observed"]
        or (candidate_time is not None and (current_time is None or candidate_time > current_time))
        or (current_time is None and candidate_time is None and not current["body"] and bool(candidate["body"]))
    )
    return {**preferred, **(candidate if use_candidate else current)}


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
                "mentions_dd": (_mentions_dd(body, github_account) if "body" in raw else bool(raw.get("mentions_dd"))),
                **_body_context(raw),
                "metadata": metadata,
                "has_open_dd_pr": bool(has_open_dd_pr),
            }
        )

    return normalized


def _run_json(args, runner=subprocess.run):
    completed = run_command(args, runner=runner, text=True, capture_output=True, check=True)
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
    return bool(bot_requests(body, github_account))


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


def bot_requests(body, github_account):
    """Extract unquoted instruction segments addressed to this exact configured account."""
    if not github_account:
        return []
    lines = []
    fence = None
    for line in (body or "").splitlines():
        stripped = line.lstrip()
        marker = (re.match(r"(`{3,}|~{3,})", stripped)
                  if len(line) - len(stripped) <= 3 and not line.startswith("\t") else None)
        if fence:
            if (marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence)
                    and not stripped[marker.end():].strip()):
                fence = None
            continue
        if marker:
            fence = marker[1]
            continue
        if stripped.startswith(">") or line.startswith(("    ", "\t")):
            continue
        lines.append(line)
    text = "\n".join(lines)
    text = re.sub(r"(`+)[\s\S]*?\1", "", text)
    mention = rf"(?<![\w@])@{re.escape(github_account)}(?=$|[\s,:：，])"
    return [re.split(r"(?<!\w)@[A-Za-z0-9][A-Za-z0-9-]*", text[match.end():], maxsplit=1)[0].strip(' \t\n,:：，')
            for match in re.finditer(mention, text, re.I)]


def is_vote_request(body, github_account=None):
    """Recognize conservative voting commands, leaving ambiguous wording to classification."""
    chinese_prefix = r"(?:请(?:你|帮我)?|帮我|麻烦(?:你)?)?"
    chinese_actors = r"(?:(?:组织|让|调用)(?:多个模型|各模型|多个\s*Agent|多\s*Agent))?"
    chinese_vote = r"(?:投票|表决)(?=$|[\s:：，,。]|选择|选|决定)"
    english_prefix = r"(?:(?:please(?:\s+help)?|(?:can|could|would)\s+you(?:\s+please)?)\s+)?"
    english_vote = r"vote(?=\s*(?:$|[:：]|for\b|on\b|between\b|[A-Z](?:\b|/)))"
    commands = (
        chinese_prefix + chinese_actors + r"(?:进行|发起|组织)?" + chinese_vote,
        english_prefix + r"(?:(?:organize|run|start|hold)\s+(?:a\s+)?|(?:let|have)\s+(?:multiple\s+)?(?:models|agents)\s+)?" + english_vote,
        chinese_prefix + r"(?:比较|对比)[^\n;。！？]{0,160}(?:并|然后|再)(?:请)?" + chinese_vote,
        english_prefix + r"compare\b[^\n;.!?]{0,160}\b(?:and|then)\s+" + english_vote,
    )
    for request in bot_requests(body, github_account):
        if re.search(r"(?:不要|不需要|先不|别|无需).{0,8}(?:投票|表决)|(?:do not|don't|no need to)\s+vote", request, re.I):
            continue
        if any(re.match(pattern, request, re.I) for pattern in commands):
            return True
    return False


def infer_intent(title, body, event_type, github_account=None):
    if event_type == "review_request":
        return "review_request"
    if is_vote_request(body, github_account):
        return "vote"
    text = f"{title or ''}\n{body or ''}".lower()
    if re.search(r"@\S+\s+(?:please\s+)?review\b", body or "", re.I):
        return "review_request"
    clean_body = re.sub(r"```[\s\S]*?```", "", body or "")
    clean_body = '\n'.join(line for line in clean_body.splitlines() if not line.lstrip().startswith('>'))
    requests = bot_requests(body, github_account)
    instruction = (requests[0] if requests else re.sub(r'^@\S+\s+', '', clean_body.strip())).lower()
    prefix = r'^(?:(?:please|can you|could you)\s+|请(?:帮我)?|帮我)?'
    if re.match(prefix + r'(?:分析|讨论|解释|为什么|为何|analy[sz]e\b|analysis\b|explain\b|discuss\b)', instruction):
        return 'analysis'
    if re.match(prefix + r'(?:fix\b|修复)', instruction):
        return 'bug_fix'
    if re.match(prefix + r'(?:add\s+(?:unit\s+)?tests?\b|write\s+tests?\b|补充测试|增加测试)', instruction):
        return 'add_tests'
    if re.match(prefix + r'(?:implement\b|add\b|build\b|create\b|support\b|实现|支持|增加|添加)', instruction):
        return 'small_change'
    if (not re.search(r"(?:不要|无需|别).{0,8}修复|\b(?:do not|don't)\s+fix\b", instruction)
            and re.search(r'\b(?:bug|fix|fixes|fixed)\b|修复|报错|失败', text)):
        return 'bug_fix'
    if any(
        word in text
        for word in ["分析", "analysis", "analyze", "explain", "discuss", "讨论", "方案"]
    ):
        return "analysis"
    return "unclear"


_infer_intent = infer_intent


def _issue_search_to_raw(item, event_type, github_account=None):
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
        "body_observed": "body" in item,
        "body_updated_at": item.get("updatedAt"),
        "url": item.get("url"),
        "event_at": item.get("updatedAt"),
        "intent": _infer_intent(item.get("title", ""), body, event_type, github_account),
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
    head_sha = head.get("sha") or raw.get("headRefOid") or raw.get("head_sha")

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
    if head_sha:
        enriched["existing_pr_head_sha"] = head_sha
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
        "notification_reason": notification.get("reason"),
        "actor_login": "github",
        "trusted_trigger_found": False,
        "authorization_lookup_complete": False,
        "event_at": notification.get("updated_at"),
    }


def collect_account_notifications(repo_configs, runner=subprocess.run, read_session=None):
    if read_session is not None:
        return read_session.collect_notifications(repo_configs)
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
            **(_body_context({"body": payload["body"], "body_updated_at": payload.get("updated_at")})
               if raw.get("event_type") in {"notification", "assigned"} and "body" in payload
               else _body_context(raw)),
            "state": payload.get("state") or raw.get("state", ""),
            "state_reason": payload.get("state_reason"),
            "source_updated_at": payload.get("updated_at"),
            "closed_at": payload.get("closed_at"),
            "url": payload.get("html_url") or raw.get("url"),
            "author_login": user.get("login") or raw.get("author_login"),
        },
        payload.get("state"),
    )


def _timeline_sort_key(item):
    return (item.get("created_at") or "", str(item.get("id") or ""))


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
    matches = [
        item
        for item in timeline
        if item.get("event") == "assigned"
        and _login(item, "assignee") == github_account
    ]
    if matches:
        item = max(matches, key=_timeline_sort_key)
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
    matches = []
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
        matches.append(
            (
                item,
                requested_reviewer,
                requested_team_slug or requested_team_name,
            )
        )
    if matches:
        item, requested_reviewer, requested_team = max(
            matches,
            key=lambda match: _timeline_sort_key(match[0]),
        )
        event_id = item.get("id") or raw.get("id")
        enriched = {
            **raw,
            "id": event_id,
            "event_type": "review_request",
            "requester_login": _login(item, "actor"),
            "requested_reviewer": requested_reviewer,
            "requested_team": requested_team,
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
        "body_observed": "body" in comment,
        "body_updated_at": comment.get("updated_at") or comment.get("created_at") or comment.get("submitted_at"),
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


def _discussion_events(raw, repo_config, runner, predicate):
    items = _discussion_items(raw, repo_config, runner)
    if items is None:
        return [], False

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
        enriched["intent"] = _infer_intent(raw.get("title"), enriched.get("body"), event_type, repo_config["github_account"])
        matched.append(enriched)

    return sorted(matched, key=_event_sort_key), True


def _enrich_mentions(raw, repo_config, runner):
    github_account = repo_config["github_account"]
    trusted = set(repo_config.get("trusted_actors", []))
    body_mentions_dd = _mentions_dd(raw.get("body"), github_account)
    matches = []
    if body_mentions_dd and raw.get("actor_login") in trusted:
        matches.append(
            {
                **raw,
                "authorization_lookup_complete": True,
            }
        )

    discussion_matches, lookup_complete = _discussion_events(
        raw,
        repo_config,
        runner,
        lambda item, _event_type: _mentions_dd(item.get("body"), github_account)
        and (item.get("user") or {}).get("login") in trusted,
    )
    matches.extend(
        {
            **event,
            "authorization_lookup_complete": True,
        }
        for event in discussion_matches
    )
    if matches:
        return matches
    if not lookup_complete:
        return [
            {
                **raw,
                "authorization_lookup_complete": False,
            }
        ]
    return [
        {
            **raw,
            "authorization_lookup_complete": not body_mentions_dd,
        }
    ]


def _enrich_mention(raw, repo_config, runner):
    return _enrich_mentions(raw, repo_config, runner)[-1]


def _enrich_known_workstream_contexts(raw, repo_config, runner):
    github_account = repo_config["github_account"]
    matches, lookup_complete = _discussion_events(
        raw,
        repo_config,
        runner,
        lambda item, _event_type: (item.get("user") or {}).get("login") != github_account
        and _mentions_dd(item.get("body"), github_account)
        and bool((item.get("body") or "").strip()),
    )
    if matches:
        return [
            {
                **event,
                "authorization_lookup_complete": True,
            }
            for event in matches
        ]
    if not lookup_complete:
        return [
            {
                **raw,
                "authorization_lookup_complete": False,
            }
        ]
    return [
        {
            **raw,
            "authorization_lookup_complete": True,
        }
    ]


def _enrich_known_workstream_context(raw, repo_config, runner):
    return _enrich_known_workstream_contexts(raw, repo_config, runner)[-1]


def _trusted_triggers_from_source(
    raw,
    repo_config,
    runner,
    known_workstreams=None,
    review_author_allowlist=None,
):
    trusted = set(repo_config.get("trusted_actors", []))
    github_account = repo_config["github_account"]
    raw = _enrich_pull_request_metadata(raw, repo_config, runner)
    known_workstreams = set(known_workstreams or [])
    raw_workstream_id = _raw_workstream_id(raw, repo_config["full_name"])

    if (raw.get('source_type') == 'pull_request' and raw.get('notification_reason') == 'review_requested'
            and (raw.get('pr_author_login') or '').casefold() in (review_author_allowlist or ())):
        # This notification requests a new native review round. An older
        # assignment is not its trigger, even if that assignment was trusted.
        requested = _timeline_review_request(raw, repo_config, runner)
        return [{**requested,
                'actor_login': requested.get('requester_login') or requested.get('actor_login'),
                'trusted_trigger_found': requested.get('requester_login') in trusted}]

    # A known source may carry new requests after its assignment was consumed.
    # Keep every discussion event's identity; authorization uses current trust.
    # Review-request notifications retain their dedicated timeline semantics.
    if raw_workstream_id in known_workstreams and raw.get('notification_reason') != 'review_requested':
        contexts = _enrich_known_workstream_contexts(raw, repo_config, runner)
        if any(event.get('event_type') != 'notification'
               or event.get('authorization_lookup_complete') is False for event in contexts):
            return [{**event, 'trusted_trigger_found': False} for event in reversed(contexts)]

    def _discussion_result():
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
        return [{
            **raw,
            "trusted_trigger_found": False,
            "authorization_lookup_complete": False,
        }]
    if assigned.get("assignment_actor_login") in trusted:
        return [{
            **assigned,
            "trusted_trigger_found": True,
            "authorization_lookup_complete": True,
        }]

    review_request = _timeline_review_request(raw, repo_config, runner)
    if review_request.get("authorization_lookup_complete") is False:
        return [{
            **raw,
            "trusted_trigger_found": False,
            "authorization_lookup_complete": False,
        }]
    if review_request.get("requester_login") in trusted:
        return [{
            **review_request,
            "trusted_trigger_found": True,
            "authorization_lookup_complete": True,
        }]

    discussion_result = _discussion_result()
    if discussion_result is not None:
        return [discussion_result]

    return [{
        **raw,
        "trusted_trigger_found": False,
        "authorization_lookup_complete": True,
    }]


def collect_live_events(
    repo_config,
    runner=subprocess.run,
    limit=30,
    known_workstreams=None,
    notification_hints=None,
    include_notifications=True,
    review_author_allowlist=None,
    read_session=None,
):
    repo = repo_config["full_name"]
    github_account = repo_config["github_account"]
    fields = "number,title,body,author,authorAssociation,isPullRequest,updatedAt,url"
    raw_events = []
    known_workstreams = set(known_workstreams or [])

    if read_session is not None:
        runner = read_session
    notification_raws = [raw for raw in (notification_hints or [])
                         if raw.get("repo_full_name") == repo]
    if include_notifications and notification_hints is None:
        notification_buckets = collect_account_notifications(
            [repo_config], runner=runner, read_session=read_session,
        )
        notification_raws = notification_buckets.get(repo, [])

    def search(args):
        if read_session is not None:
            return read_session.search(args, notification_hints=notification_raws)
        return _run_json(args, runner=runner)

    def source_events(raw, load):
        if read_session is None:
            return load()
        hints = [hint for hint in notification_raws if hint['number'] == raw['number']
                 and hint['source_type'] == raw['source_type']]
        return read_session.source_events(
            repo_config, raw, load, known_workstreams=known_workstreams,
            review_author_allowlist=review_author_allowlist, notification_hints=hints,
        )

    assigned = search(
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
    )
    for item in assigned:
        raw = _issue_search_to_raw(item, "assigned", github_account)
        def load_assigned(raw=raw):
            enriched = _enrich_pull_request_metadata(raw, repo_config, runner)
            return [_timeline_assignment(enriched, repo_config, runner)]
        raw_events.extend(source_events(raw, load_assigned))

    mentions = search(
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
    )
    for item in mentions:
        raw = _issue_search_to_raw(item, "mention", github_account)
        def load_mentions(raw=raw):
            enriched = dict(raw)
            if enriched.get("author_association") in {None, "", "UNKNOWN"}:
                enriched["actor_permission"] = _actor_permission(repo_config, enriched.get("actor_login"), runner)
            enriched = _enrich_pull_request_metadata(enriched, repo_config, runner)
            if _raw_workstream_id(enriched, repo) in known_workstreams:
                return _enrich_known_workstream_contexts(enriched, repo_config, runner)
            return _enrich_mentions(enriched, repo_config, runner)
        raw_events.extend(source_events(raw, load_mentions))

    for raw in notification_raws:
        if raw.get("repo_full_name") != repo:
            continue
        def load_notification(raw=raw):
            enriched, source_state = _source_metadata(raw, repo_config, runner)
            if source_state and source_state.lower() != "open":
                return []
            return _trusted_triggers_from_source(
                enriched, repo_config, runner, known_workstreams=known_workstreams,
                review_author_allowlist=review_author_allowlist,
            )
        raw_events.extend(source_events(raw, load_notification))

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
