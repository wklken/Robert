"""Project-memory helpers for DD GitHub agent tasks."""

import json
import re
import sqlite3
from uuid import uuid4


CONFIDENCE_SCORES = {"high": 6, "medium": 3, "low": 1}
TERM_WEIGHTS = {
    "memory_thread_key": 50,
    "workstream_id": 40,
    "route_id": 16,
    "expected_output": 16,
    "path": 12,
    "symbol": 12,
    "keyword": 10,
    "kind": 4,
}
TOKEN_RE = re.compile(r"[A-Za-z0-9_./#!-]+")


def _id(prefix):
    return f"{prefix}-{uuid4().hex[:12]}"


def _json_list(value):
    return value if isinstance(value, list) else []


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _lower_list(value):
    terms = []
    for item in _json_list(value):
        if isinstance(item, str) and item.strip():
            terms.append(item.strip().lower())
    return terms


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


def _entry_terms(entry, payload, workstream_id, route_result=None):
    route_result = route_result or {}
    terms = {
        "memory_thread_key": [entry["memory_thread_key"]],
        "workstream_id": [workstream_id],
        "kind": [entry["kind"]],
        "expected_output": [
            _text(route_result.get("expected_output")) or _text(payload.get("output_type"))
        ],
        "route_id": [_text(route_result.get("route_id"))],
        "path": _lower_list(entry.get("paths")),
        "symbol": _lower_list(entry.get("symbols")),
        "keyword": _lower_list(entry.get("keywords")),
    }
    return {
        term_type: [term for term in values if term]
        for term_type, values in terms.items()
        if any(values)
    }


def _query_terms(route_result, events):
    route_result = route_result or {}
    terms = set()
    for key in ["route_id", "expected_output"]:
        value = _text(route_result.get(key))
        if value:
            terms.add(value.lower())
            terms.update(_terms_from_text(value))
    for event in events or []:
        for key in ["title", "body", "intent", "source_key", "source_type"]:
            terms.update(_terms_from_text(_text(event.get(key))))
        metadata = event.get("metadata") if isinstance(event, dict) else {}
        if isinstance(metadata, dict):
            for value in metadata.values():
                if isinstance(value, str):
                    terms.update(_terms_from_text(value))
                elif isinstance(value, dict):
                    for nested in value.values():
                        if isinstance(nested, str):
                            terms.update(_terms_from_text(nested))
    return terms


def _runtime_boost_terms(runtime_knowledge):
    terms = set()
    for knowledge in runtime_knowledge or []:
        if not isinstance(knowledge, dict):
            continue
        boost = knowledge.get("retrieval_boost") or {}
        if not isinstance(boost, dict):
            continue
        for key in ["keywords", "paths", "symbols", "terms"]:
            for value in _json_list(boost.get(key)):
                if isinstance(value, str) and value.strip():
                    terms.add(value.strip().lower())
                    terms.update(_terms_from_text(value))
    return terms


def _thread_key(payload, workstream_id):
    explicit = _text(payload.get("memory_thread_key"))
    if explicit:
        return explicit
    for action in payload.get("planned_github_actions") or []:
        if not isinstance(action, dict) or action.get("type") != "open_pr":
            continue
        url = _text(action.get("url")) or _text(action.get("target_url"))
        match = re.search(r"/pull/(\d+)/?$", url)
        if not match:
            continue
        if workstream_id.startswith("github:") and "#" in workstream_id:
            repo_name = workstream_id[len("github:") :].split("#", 1)[0]
            return f"github:{repo_name}!{match.group(1)}"
    return workstream_id


def _normalize_entry(raw_entry, payload, workstream_id):
    if not isinstance(raw_entry, dict):
        return None, "entry must be an object"
    operation = _text(raw_entry.get("operation")) or "upsert"
    if operation != "upsert":
        return None, f"unsupported memory operation: {operation}"
    title = _text(raw_entry.get("title"))
    short_summary = _text(raw_entry.get("short_summary"))
    if not title or not short_summary:
        return None, "memory entry requires title and short_summary"
    confidence = _text(raw_entry.get("confidence")) or "medium"
    if confidence not in CONFIDENCE_SCORES:
        confidence = "medium"
    entry = {
        "operation": operation,
        "kind": _text(raw_entry.get("kind")) or "context",
        "title": title,
        "short_summary": short_summary,
        "long_summary": _text(raw_entry.get("long_summary")),
        "paths": _lower_list(raw_entry.get("paths")),
        "symbols": _lower_list(raw_entry.get("symbols")),
        "keywords": _lower_list(raw_entry.get("keywords")),
        "evidence": _json_list(raw_entry.get("evidence")),
        "confidence": confidence,
        "memory_thread_key": _text(raw_entry.get("memory_thread_key"))
        or _thread_key(payload, workstream_id),
    }
    return entry, None


def _fts_exists(conn):
    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE name = 'project_memory_fts'
          AND type = 'table'
        LIMIT 1
        """
    ).fetchone()
    return bool(row)


def _sync_fts(conn, memory_id, repo_id, entry):
    if not _fts_exists(conn):
        return
    try:
        conn.execute("DELETE FROM project_memory_fts WHERE memory_id = ?", (memory_id,))
        conn.execute(
            """
            INSERT INTO project_memory_fts(
              memory_id, repo_id, title, short_summary, long_summary, paths, symbols, keywords
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                memory_id,
                repo_id,
                entry["title"],
                entry["short_summary"],
                entry["long_summary"],
                " ".join(entry["paths"]),
                " ".join(entry["symbols"]),
                " ".join(entry["keywords"]),
            ),
        )
    except sqlite3.OperationalError:
        return


def _replace_terms(conn, memory_id, repo_id, terms_by_type, run_now):
    conn.execute("DELETE FROM project_memory_terms WHERE memory_id = ?", (memory_id,))
    for term_type, terms in terms_by_type.items():
        for term in sorted(set(terms)):
            conn.execute(
                """
                INSERT OR IGNORE INTO project_memory_terms(
                  memory_id, repo_id, term_type, term_value, created_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (memory_id, repo_id, term_type, term, run_now),
            )


def _upsert_entry(conn, payload, repo_id, workstream_id, raw_entry, run_now):
    entry, error = _normalize_entry(raw_entry, payload, workstream_id)
    if error:
        return None, error
    row = conn.execute(
        """
        SELECT memory_id, revision_count
        FROM project_memory_entries
        WHERE repo_id = ?
          AND memory_thread_key = ?
          AND kind = ?
          AND title = ?
        """,
        (repo_id, entry["memory_thread_key"], entry["kind"], entry["title"]),
    ).fetchone()
    if row:
        memory_id, revision_count = row
    else:
        memory_id = _id("pmem")
        revision_count = 0
        conn.execute(
            """
            INSERT INTO project_memory_entries(
              memory_id, repo_id, memory_thread_key, kind, title, short_summary,
              long_summary, confidence, source_task_id, source_result_id,
              revision_count, created_at, updated_at, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                memory_id,
                repo_id,
                entry["memory_thread_key"],
                entry["kind"],
                entry["title"],
                entry["short_summary"],
                entry["long_summary"],
                entry["confidence"],
                payload.get("task_id"),
                payload.get("result_id"),
                run_now,
                run_now,
                json.dumps(
                    {
                        "paths": entry["paths"],
                        "symbols": entry["symbols"],
                        "keywords": entry["keywords"],
                        "evidence": entry["evidence"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            ),
        )
    revision_id = _id("pmemrev")
    revision_payload = dict(entry)
    revision_payload["result_id"] = payload.get("result_id")
    revision_payload["task_id"] = payload.get("task_id")
    revision_payload["attempt_id"] = payload.get("attempt_id")
    conn.execute(
        """
        INSERT INTO project_memory_revisions(
          revision_id, memory_id, repo_id, result_id, task_id, attempt_id,
          operation, title, short_summary, long_summary, payload_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            revision_id,
            memory_id,
            repo_id,
            payload.get("result_id"),
            payload.get("task_id"),
            payload.get("attempt_id"),
            entry["operation"],
            entry["title"],
            entry["short_summary"],
            entry["long_summary"],
            json.dumps(revision_payload, ensure_ascii=False, sort_keys=True),
            run_now,
        ),
    )
    conn.execute(
        """
        UPDATE project_memory_entries
        SET short_summary = ?,
            long_summary = ?,
            confidence = ?,
            source_task_id = ?,
            source_result_id = ?,
            current_revision_id = ?,
            revision_count = ?,
            updated_at = ?,
            metadata_json = ?
        WHERE memory_id = ?
        """,
        (
            entry["short_summary"],
            entry["long_summary"],
            entry["confidence"],
            payload.get("task_id"),
            payload.get("result_id"),
            revision_id,
            revision_count + 1,
            run_now,
            json.dumps(
                {
                    "paths": entry["paths"],
                    "symbols": entry["symbols"],
                    "keywords": entry["keywords"],
                    "evidence": entry["evidence"],
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            memory_id,
        ),
    )
    _replace_terms(
        conn,
        memory_id,
        repo_id,
        _entry_terms(entry, payload, workstream_id),
        run_now,
    )
    _sync_fts(conn, memory_id, repo_id, entry)
    return memory_id, None


def record_memory_delta(conn, result_payload, workstream_id, repo_id, run_now):
    delta = result_payload.get("memory_delta")
    if not isinstance(delta, dict):
        return {"status": "skipped", "recorded_count": 0, "reason": "no_memory_delta"}
    status = _text(delta.get("status")) or "none"
    if status != "has_memory":
        return {
            "status": "skipped",
            "recorded_count": 0,
            "reason": _text(delta.get("reason")) or status,
        }
    entries = delta.get("entries")
    if not isinstance(entries, list):
        return {
            "status": "skipped",
            "recorded_count": 0,
            "safe_error": "memory_delta.entries must be a list",
        }
    recorded = []
    skipped = []
    for raw_entry in entries:
        memory_id, error = _upsert_entry(
            conn,
            result_payload,
            repo_id,
            workstream_id,
            raw_entry,
            run_now,
        )
        if memory_id:
            recorded.append(memory_id)
        else:
            skipped.append(error)
    if not recorded:
        return {
            "status": "skipped",
            "recorded_count": 0,
            "safe_error": "; ".join(error for error in skipped if error) or "no valid memory entries",
        }
    return {
        "status": "recorded",
        "recorded_count": len(recorded),
        "memory_ids": recorded,
        "skipped_count": len(skipped),
    }


def _candidate_ids_from_terms(conn, repo_id, query_terms):
    if not query_terms:
        return {}
    placeholders = ",".join("?" for _ in query_terms)
    params = [repo_id, *sorted(query_terms)]
    scores = {}
    for memory_id, term_type in conn.execute(
        f"""
        SELECT memory_id, term_type
        FROM project_memory_terms
        WHERE repo_id = ?
          AND term_value IN ({placeholders})
        """,
        params,
    ):
        scores[memory_id] = scores.get(memory_id, 0) + TERM_WEIGHTS.get(term_type, 1)
    return scores


def _candidate_ids_from_threads(conn, repo_id, thread_keys):
    scores = {}
    for thread_key in sorted(key for key in thread_keys if key):
        for (memory_id,) in conn.execute(
            """
            SELECT memory_id
            FROM project_memory_entries
            WHERE repo_id = ?
              AND memory_thread_key = ?
            """,
            (repo_id, thread_key),
        ):
            scores[memory_id] = scores.get(memory_id, 0) + TERM_WEIGHTS["memory_thread_key"]
    return scores


def _thread_keys(workstream_id, events):
    keys = {workstream_id}
    for event in events or []:
        for key in ["workstream_id", "origin_workstream_id"]:
            value = _text(event.get(key)) if isinstance(event, dict) else ""
            if value:
                keys.add(value)
        metadata = event.get("metadata") if isinstance(event, dict) else {}
        if isinstance(metadata, dict):
            dd_workstream = metadata.get("dd_workstream")
            if isinstance(dd_workstream, dict):
                for key in ["workstream_id", "origin_workstream_id"]:
                    value = _text(dd_workstream.get(key))
                    if value:
                        keys.add(value)
    return keys


def retrieve_memories(conn, repo_id, workstream_id, route_result, events, limit=5, runtime_knowledge=None):
    query_terms = _query_terms(route_result, events)
    boost_terms = _runtime_boost_terms(runtime_knowledge)
    thread_scores = _candidate_ids_from_threads(conn, repo_id, _thread_keys(workstream_id, events))
    term_scores = _candidate_ids_from_terms(conn, repo_id, query_terms)
    boost_scores = _candidate_ids_from_terms(conn, repo_id, boost_terms)
    scores = dict(term_scores)
    for memory_id, score in boost_scores.items():
        scores[memory_id] = scores.get(memory_id, 0) + (score * 3)
    for memory_id, score in thread_scores.items():
        scores[memory_id] = scores.get(memory_id, 0) + score
    if not scores:
        return []
    placeholders = ",".join("?" for _ in scores)
    rows = conn.execute(
        f"""
        SELECT memory_id, memory_thread_key, kind, title, short_summary,
               long_summary, confidence, updated_at, metadata_json
        FROM project_memory_entries
        WHERE memory_id IN ({placeholders})
        """,
        list(scores),
    ).fetchall()
    memories = []
    for row in rows:
        (
            memory_id,
            memory_thread_key,
            kind,
            title,
            short_summary,
            long_summary,
            confidence,
            updated_at,
            metadata_json,
        ) = row
        metadata = json.loads(metadata_json or "{}")
        score = scores[memory_id] + CONFIDENCE_SCORES.get(confidence, 0)
        memories.append(
            {
                "memory_id": memory_id,
                "memory_thread_key": memory_thread_key,
                "kind": kind,
                "title": title,
                "short_summary": short_summary,
                "long_summary": long_summary,
                "confidence": confidence,
                "paths": metadata.get("paths", []),
                "symbols": metadata.get("symbols", []),
                "keywords": metadata.get("keywords", []),
                "score": score,
                "updated_at": updated_at,
            }
        )
    memories.sort(key=lambda item: (-item["score"], item["updated_at"], item["memory_id"]))
    return memories[:limit]
