"""Human-approved runtime knowledge helpers for DD GitHub agent."""

import json
import re
from uuid import uuid4


VALID_SCOPE_TYPES = {"global", "route", "path", "symbol", "workstream"}
TOKEN_RE = re.compile(r"[A-Za-z0-9_./#!-]+")


def _id(prefix):
    return f"{prefix}-{uuid4().hex[:12]}"


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _json_list(value):
    return value if isinstance(value, list) else []


def _json_object(value):
    return value if isinstance(value, dict) else {}


def _decode_json(value, default):
    if not value:
        return default
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return default
    if isinstance(default, list) and isinstance(parsed, list):
        return parsed
    if isinstance(default, dict) and isinstance(parsed, dict):
        return parsed
    return default


def _terms_from_text(value):
    terms = set()
    for token in TOKEN_RE.findall(value or ""):
        normalized = token.strip(".,:;()[]{}<>\"'`").lower()
        if not normalized:
            continue
        terms.add(normalized)
        if "-" in normalized:
            terms.update(part for part in normalized.split("-") if part)
        if "/" in normalized:
            terms.update(part for part in normalized.split("/") if part)
    return terms


def _candidate_type(kind):
    if kind == "anti_pattern":
        return "anti_pattern"
    if kind in {"verification", "test"}:
        return "verification_hint"
    return "rule"


def _candidate_prompt_text(row, metadata):
    paths = metadata.get("paths") or []
    symbols = metadata.get("symbols") or []
    suffix = []
    if paths:
        suffix.append("Paths: " + ", ".join(paths[:3]))
    if symbols:
        suffix.append("Symbols: " + ", ".join(symbols[:3]))
    prompt = f"{row['title']}: {row['short_summary']}"
    if suffix:
        prompt += " (" + "; ".join(suffix) + ")"
    return prompt


def _candidate_retrieval_boost(metadata):
    return {
        "keywords": metadata.get("keywords") or [],
        "paths": metadata.get("paths") or [],
        "symbols": metadata.get("symbols") or [],
    }


def propose_candidates(conn, repo_id, run_now):
    rows = conn.execute(
        """
        SELECT memory_id, kind, title, short_summary, confidence, metadata_json
        FROM project_memory_entries
        WHERE repo_id = ?
        ORDER BY updated_at DESC, memory_id
        """,
        (repo_id,),
    ).fetchall()
    proposed = []
    for row in rows:
        row = {
            "memory_id": row[0],
            "kind": row[1],
            "title": row[2],
            "short_summary": row[3],
            "confidence": row[4],
            "metadata_json": row[5],
        }
        source_memory_ids = [row["memory_id"]]
        source_json = json.dumps(source_memory_ids, sort_keys=True)
        exists = conn.execute(
            """
            SELECT candidate_id
            FROM knowledge_candidates
            WHERE repo_id = ?
              AND title = ?
              AND source_memory_ids_json = ?
            """,
            (repo_id, row["title"], source_json),
        ).fetchone()
        if exists:
            continue
        metadata = _decode_json(row["metadata_json"], {})
        candidate_id = _id("kc")
        retrieval_boost = _candidate_retrieval_boost(metadata)
        conn.execute(
            """
            INSERT INTO knowledge_candidates(
              candidate_id, repo_id, title, summary, prompt_text, candidate_type,
              source_memory_ids_json, evidence_json, confidence, status,
              created_at, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                candidate_id,
                repo_id,
                row["title"],
                row["short_summary"],
                _candidate_prompt_text(row, metadata),
                _candidate_type(row["kind"]),
                source_json,
                json.dumps(metadata.get("evidence") or [], ensure_ascii=False, sort_keys=True),
                row["confidence"],
                run_now,
                json.dumps(
                    {"retrieval_boost": retrieval_boost},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            ),
        )
        proposed.append(candidate_id)
    return {"ok": True, "status": "proposed", "candidate_count": len(proposed), "candidate_ids": proposed}


def approve_candidate(conn, candidate_id, scope_type, scope_value, approved_by, run_now):
    scope_type = _text(scope_type)
    if scope_type not in VALID_SCOPE_TYPES:
        return {"ok": False, "status": "failed_validation", "safe_error": f"invalid scope_type: {scope_type}"}
    if scope_type == "global":
        scope_value = ""
    else:
        scope_value = _text(scope_value)
        if not scope_value:
            return {"ok": False, "status": "failed_validation", "safe_error": "scope_value is required"}
    approved_by = _text(approved_by)
    if not approved_by:
        return {"ok": False, "status": "failed_validation", "safe_error": "approved_by is required"}
    row = conn.execute(
        """
        SELECT repo_id, title, prompt_text, metadata_json, status
        FROM knowledge_candidates
        WHERE candidate_id = ?
        """,
        (candidate_id,),
    ).fetchone()
    if not row:
        return {"ok": False, "status": "not_found", "safe_error": f"candidate not found: {candidate_id}"}
    repo_id, title, prompt_text, metadata_json, status = row
    if status != "pending":
        return {"ok": False, "status": "not_pending", "safe_error": f"candidate is {status}"}
    metadata = _decode_json(metadata_json, {})
    knowledge_id = _id("rk")
    conn.execute(
        """
        INSERT INTO runtime_knowledge(
          knowledge_id, candidate_id, repo_id, scope_type, scope_value,
          title, prompt_text, retrieval_boost_json, active,
          approved_by, approved_at, created_at, updated_at, metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
        """,
        (
            knowledge_id,
            candidate_id,
            repo_id,
            scope_type,
            scope_value,
            title,
            prompt_text,
            json.dumps(
                _json_object(metadata.get("retrieval_boost")),
                ensure_ascii=False,
                sort_keys=True,
            ),
            approved_by,
            run_now,
            run_now,
            run_now,
            "{}",
        ),
    )
    conn.execute(
        """
        UPDATE knowledge_candidates
        SET status = 'approved',
            reviewed_at = ?,
            reviewer = ?
        WHERE candidate_id = ?
        """,
        (run_now, approved_by, candidate_id),
    )
    return {
        "ok": True,
        "status": "approved",
        "candidate_id": candidate_id,
        "knowledge_id": knowledge_id,
    }


def reject_candidate(conn, candidate_id, reviewer, review_note, run_now):
    reviewer = _text(reviewer)
    if not reviewer:
        return {"ok": False, "status": "failed_validation", "safe_error": "reviewer is required"}
    row = conn.execute(
        "SELECT status FROM knowledge_candidates WHERE candidate_id = ?",
        (candidate_id,),
    ).fetchone()
    if not row:
        return {"ok": False, "status": "not_found", "safe_error": f"candidate not found: {candidate_id}"}
    if row[0] != "pending":
        return {"ok": False, "status": "not_pending", "safe_error": f"candidate is {row[0]}"}
    conn.execute(
        """
        UPDATE knowledge_candidates
        SET status = 'rejected',
            reviewed_at = ?,
            reviewer = ?,
            review_note = ?
        WHERE candidate_id = ?
        """,
        (run_now, reviewer, _text(review_note), candidate_id),
    )
    return {"ok": True, "status": "rejected", "candidate_id": candidate_id}


def _event_terms(events):
    terms = set()
    for event in events or []:
        if not isinstance(event, dict):
            continue
        for key in ["title", "body", "intent", "source_key", "source_type"]:
            terms.update(_terms_from_text(_text(event.get(key))))
        metadata = event.get("metadata")
        if isinstance(metadata, dict):
            for value in metadata.values():
                if isinstance(value, str):
                    terms.update(_terms_from_text(value))
    return terms


def _knowledge_score(row, route_result, event_terms, workstream_id):
    scope_type = row["scope_type"]
    scope_value = row["scope_value"]
    if scope_type == "global":
        return 1
    if scope_type == "route":
        if scope_value in {
            _text(route_result.get("route_id")),
            _text(route_result.get("expected_output")),
        }:
            return 50
        return 0
    if scope_type == "workstream":
        return 60 if scope_value and scope_value == workstream_id else 0
    if scope_type in {"path", "symbol"}:
        return 35 if scope_value.lower() in event_terms else 0
    return 0


def load_runtime_knowledge(conn, repo_id, route_result, events, workstream_id=None, limit=5):
    rows = conn.execute(
        """
        SELECT knowledge_id, candidate_id, repo_id, scope_type, scope_value,
               title, prompt_text, retrieval_boost_json, approved_by, approved_at
        FROM runtime_knowledge
        WHERE repo_id = ?
          AND active = 1
        ORDER BY approved_at DESC, knowledge_id
        """,
        (repo_id,),
    ).fetchall()
    terms = _event_terms(events)
    matched = []
    for row in rows:
        item = {
            "knowledge_id": row[0],
            "candidate_id": row[1],
            "repo_id": row[2],
            "scope_type": row[3],
            "scope_value": row[4],
            "title": row[5],
            "prompt_text": row[6],
            "retrieval_boost": _decode_json(row[7], {}),
            "approved_by": row[8],
            "approved_at": row[9],
        }
        score = _knowledge_score(item, route_result or {}, terms, workstream_id)
        if score <= 0:
            continue
        item["score"] = score
        matched.append(item)
    matched.sort(key=lambda item: (-item["score"], item["approved_at"], item["knowledge_id"]))
    return matched[:limit]


def list_candidates(conn, repo_id=None, status=None):
    clauses = []
    params = []
    if repo_id:
        clauses.append("repo_id = ?")
        params.append(repo_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    rows = conn.execute(
        f"""
        SELECT candidate_id, repo_id, title, summary, candidate_type,
               confidence, status, created_at, reviewed_at, reviewer
        FROM knowledge_candidates
        {where}
        ORDER BY created_at DESC, candidate_id
        """,
        params,
    ).fetchall()
    return [
        {
            "candidate_id": row[0],
            "repo_id": row[1],
            "title": row[2],
            "summary": row[3],
            "candidate_type": row[4],
            "confidence": row[5],
            "status": row[6],
            "created_at": row[7],
            "reviewed_at": row[8],
            "reviewer": row[9],
        }
        for row in rows
    ]


def show_candidate(conn, candidate_id):
    row = conn.execute(
        """
        SELECT candidate_id, repo_id, title, summary, prompt_text,
               candidate_type, source_memory_ids_json, evidence_json,
               confidence, status, created_at, reviewed_at, reviewer,
               review_note, metadata_json
        FROM knowledge_candidates
        WHERE candidate_id = ?
        """,
        (candidate_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "candidate_id": row[0],
        "repo_id": row[1],
        "title": row[2],
        "summary": row[3],
        "prompt_text": row[4],
        "candidate_type": row[5],
        "source_memory_ids": _decode_json(row[6], []),
        "evidence": _decode_json(row[7], []),
        "confidence": row[8],
        "status": row[9],
        "created_at": row[10],
        "reviewed_at": row[11],
        "reviewer": row[12],
        "review_note": row[13],
        "metadata": _decode_json(row[14], {}),
    }
