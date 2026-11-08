"""Complete a dispatch only from its final result's confirmed publication receipts."""

import json
from urllib.parse import urlsplit

from robert_agent.delivery import results
from robert_agent.security.redaction import redact_and_truncate
from robert_agent.foundation.contracts.work import DispatchRequestStatus as Status
from robert_agent.persistence.queries import dispatch_requests as queries
from robert_agent.persistence.stores import dispatch_requests as stores


def complete(conn, repo_ids, now):
    for request in queries.delivery_candidates(conn, repo_ids):
        issue_url = json.loads(request["issue_json"])["html_url"]
        groups = {}
        for action in queries.published_results(conn, request["request_id"]):
            groups.setdefault(action["result_id"], []).append(action)
        for result_id, actions in groups.items():
            output = actions[0]["output_type"]
            if output not in {"comment_analysis", "new_pr"} or not results.result_actions_all_published(
                conn, result_id
            ):
                continue
            confirmed = []
            comment_receipts = {}
            for action in actions:
                receipt = json.loads(action["metadata_json"]).get("publish", {}).get("receipt", {})
                if (
                    receipt.get("schema_version") == 1
                    and receipt.get("result_id") == result_id
                    and receipt.get("confirmed_at")
                    and all(
                        receipt.get(key) == action[key] for key in ("action_id", "task_id", "action_type", "target_url")
                    )
                ):
                    confirmed.append(action)
                    comment_receipts[action["action_id"]] = receipt
            if len(confirmed) != len(actions):
                continue
            comments = [
                item
                for item in confirmed
                if item["action_type"] == "comment"
                and urlsplit(item["target_url"] or "")[:3] == urlsplit(issue_url)[:3]
                and comment_receipts[item["action_id"]].get("external_id")
                and urlsplit(item["target_url"]).fragment
                == "issuecomment-" + str(comment_receipts[item["action_id"]]["external_id"])
            ]
            prs = [item for item in confirmed if item["action_type"] == "open_pr" and item["target_url"]]
            if not comments or (output == "new_pr" and not prs):
                continue
            safe = redact_and_truncate(actions[0]["handoff"], 2000)
            summary = safe["text"] if safe["ok"] else "Work delivered; inspect GitHub for the result."
            payload = {
                "issue_url": issue_url,
                "result_url": comments[0]["target_url"],
                "summary": summary or "Requested work delivered to GitHub.",
                "pr_url": prs[0]["target_url"] if prs else None,
                "output_type": output,
            }
            if stores.transition(
                conn,
                request["request_id"],
                Status.ADMITTED.value,
                Status.DELIVERED.value,
                now,
                result_json=json.dumps(payload, ensure_ascii=False),
            ):
                stores.notification(
                    conn,
                    request["request_id"],
                    "completed",
                    now,
                    json.dumps(
                        {
                            **payload,
                            "request_id": request["request_id"],
                            "repo_id": request["repo_id"],
                            "repository_scoped": True,
                            "dispatch_identity": json.loads(request["identity_json"]),
                        },
                        ensure_ascii=False,
                    ),
                    actions[0]["task_id"],
                )
            break
