"""Daily proposal run facts and a scoped, durable operator digest."""

from contextlib import closing
from datetime import timedelta
import json

from robert_agent.adapters.notifications import wakeup
from robert_agent.delivery.messages import allowed_repo_ids
from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.persistence import database, transactions
from robert_agent.persistence.queries import proposals as queries
from robert_agent.persistence.stores import proposals as store, proposal_feedback
from robert_agent.security.redaction import redact_text


MAX_ATTEMPTS = 3
RETRY_SECONDS = 300


def analysis_slot(day):
    return "proposal-analysis:" + day


def claim_analysis(config, day, now):
    """Reserve a bounded daily attempt while the scheduler holds its process lock."""
    with closing(database.connect(config["db_path"])) as conn, conn:
        transactions.begin_immediate(conn)
        previous = queries.scan_result(conn, analysis_slot(day))
        result = json.loads(previous["result_json"] or "{}") if previous else {}
        if result.get("status") == "completed" or result.get("attempts", 0) >= MAX_ATTEMPTS:
            return False
        if previous:
            previous_at = utc_timestamp(previous["completed_at"])
            if previous_at and utc_timestamp(now) < previous_at + timedelta(seconds=RETRY_SECONDS):
                return False
        store.record_scan(
            conn, analysis_slot(day), now, json.dumps({"status": "running", "attempts": result.get("attempts", 0) + 1})
        )
    return True


def finish_analysis(config, day, now, result):
    with closing(database.connect(config["db_path"])) as conn, conn:
        transactions.begin_immediate(conn)
        previous = queries.scan_result(conn, analysis_slot(day))
        prior = json.loads(previous["result_json"] or "{}") if previous else {}
        store.record_scan(conn, analysis_slot(day), now, json.dumps({**result, "attempts": prior.get("attempts", 1)}))


def _summary(day, rows, analysis, running):
    lines = [f"Robert 今日推荐 · {day}"]
    status = analysis.get("status")
    if running or status == "running":
        lines.append("分析仍在进行，后续结果会更新到 Web。")
    elif status == "partial":
        lines.append("部分来源或分析尚未刷新，请在 Web 查看各项分析时间。")
    elif status != "completed":
        lines.append("本日分析尚未完成，当前推荐沿用已有结果。")
    if analysis.get("discovered") or analysis.get("expired"):
        lines.append(f"新增候选 {analysis.get('discovered', 0)} 条，延期到期 {analysis.get('expired', 0)} 条。")
    if not rows:
        lines.append("暂无推荐。")
    for number, row in enumerate(rows, 1):
        source = json.loads(row["source_json"])
        report = json.loads(row["report_json"])
        text = f"{number}. {source.get('title', '待查看提案')}\n{report.get('recommendation_reason') or report.get('summary', '')}"
        redacted = redact_text(text)
        lines.append(redacted["text"][:800] if redacted["ok"] else f"{number}. 请在 Web 查看此提案。")
    lines.append("打开 Web 查看推荐、调查或决定处理。")
    return "\n\n".join(lines)


def enqueue(config, day, now, *, running=False):
    """Create one local outbox row and its daily slot in the same transaction."""
    if not config.get("messaging", {}).get("enabled"):
        return False
    slot = "proposal-digest:" + day
    with closing(database.connect(config["db_path"])) as conn, conn:
        transactions.begin_immediate(conn)
        if queries.scan_result(conn, slot):
            return False
        allowed = allowed_repo_ids(conn, config)
        rows = queries.current(conn, status="pending", repo_ids=allowed, limit=5, ranked=True)
        analysis_row = queries.scan_result(conn, analysis_slot(day))
        analysis = json.loads(analysis_row["result_json"] or "{}") if analysis_row else {}
        metadata = {
            "summary": _summary(day, rows, analysis, running),
            "result_url": config["proposer"]["schedule"]["web_url"] + "/proposals",
            "proposal_digest_repo_ids": sorted(allowed),
        }
        proposal_feedback.record(
            conn, slot, "proposal_digest", now, json.dumps(metadata, ensure_ascii=False), operator=True
        )
        store.record_scan(conn, slot, now, json.dumps({"status": "queued", "count": len(rows)}))
    wakeup.notify(config["db_path"])
    return True
