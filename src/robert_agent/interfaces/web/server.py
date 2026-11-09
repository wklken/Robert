from robert_agent.persistence import errors as db_errors
from robert_agent.interfaces.web.templates import render_dashboard_html
from robert_agent.context.application import knowledge_review
from robert_agent.work.application import web_setup
from robert_agent.readmodels import dashboard

#!/usr/bin/env python3
import argparse
from contextlib import closing
from pathlib import Path
from robert_agent.persistence import database as db_database
from robert_agent.persistence.queries import sqlite_master
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.resources import files
import ipaddress
import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from robert_agent.configuration import command as validate_config
from robert_agent.readmodels import board
from robert_agent.work.application import commands as work_items
from robert_agent.readmodels import workbench
from robert_agent.foundation.commands import emit


WEB_ASSET_ROOT = files("robert_agent").joinpath("web_assets")
WEB_ASSETS = {
    "/": ("board.html", "text/html; charset=utf-8"),
    "/work": ("index.html", "text/html; charset=utf-8"),
    "/board": ("board.html", "text/html; charset=utf-8"),
    "/favicon.ico": ("robert-favicon.svg", "image/svg+xml"),
    "/assets/robert-favicon.svg": ("robert-favicon.svg", "image/svg+xml"),
    "/assets/robert-logo.svg": ("robert-logo.svg", "image/svg+xml"),
    "/assets/github-shell.css": ("github-shell.css", "text/css; charset=utf-8"),
    "/assets/operations.css": ("operations.css", "text/css; charset=utf-8"),
    "/assets/board.css": ("board.css", "text/css; charset=utf-8"),
    "/assets/board-client.js": ("board-client.js", "text/javascript; charset=utf-8"),
    "/assets/board.js": ("board.js", "text/javascript; charset=utf-8"),
    "/assets/workbench.css": ("workbench.css", "text/css; charset=utf-8"),
    "/assets/workbench.js": (
        "workbench.js",
        "application/javascript; charset=utf-8",
    ),
}


def validate_server_options(host, writable, allow_remote):
    try:
        address = ipaddress.ip_address(host)
        loopback = address.is_loopback
    except ValueError:
        loopback = host == "localhost"
    if not loopback and not allow_remote:
        return {
            "ok": False,
            "status": "security_refusal",
            "safe_error": ("non-loopback binding requires --allow-remote and an authenticated reverse proxy"),
        }
    return {
        "ok": True,
        "status": "valid",
        "writable": bool(writable),
        "csrf_required": bool(writable),
        "remote_warning": not loopback,
    }


@dataclass(frozen=True)
class ControlContext:
    db_path: str
    operator_identity: str
    allowed_repo_ids: frozenset[str]
    allowed_workers: frozenset[str]
    allowed_origins: frozenset[str]
    csrf_token: str
    writes_enabled: bool
    write_error: str | None


def _query_limit(query, default=50):
    raw = (query.get("limit") or [str(default)])[0]
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise board.BoardQueryError("limit must be an integer") from exc


def build_http_response(path, db_path, control_context=None):
    parsed = urlsplit(path)
    route = parsed.path
    if route in WEB_ASSETS:
        asset_route = "/work" if route == "/" and "bucket" in parse_qs(parsed.query) else route
        filename, content_type = WEB_ASSETS[asset_route]
        try:
            payload = (WEB_ASSET_ROOT / filename).read_bytes()
        except OSError:
            return 404, {"content-type": "text/plain; charset=utf-8"}, b"not found\n"
        return 200, {"content-type": content_type}, payload
    if route == "/api/session":
        context = control_context
        payload = {
            "writes_enabled": bool(context and context.writes_enabled),
            "write_error": context.write_error if context else "Start with --config to enable writes.",
            "csrf_token": context.csrf_token if context else None,
            "operator_identity": context.operator_identity if context else None,
            "allowed_repo_ids": sorted(context.allowed_repo_ids) if context else [],
            "allowed_workers": sorted(context.allowed_workers) if context else [],
        }
        return _json_response(payload)
    if route == "/api/board":
        query = parse_qs(parsed.query)
        try:
            payload = board.list_board(
                db_path,
                repo=(query.get("repo") or [None])[0],
                column=(query.get("column") or [None])[0],
                agent=(query.get("agent") or [None])[0],
                priority=(query.get("priority") or [None])[0],
                attention=(query.get("attention") or [None])[0],
                query=(query.get("q") or [""])[0],
                limit=_query_limit(query),
                cursor=(query.get("cursor") or [None])[0],
                include_history=(query.get("include_history") or ["false"])[0].lower() in {"1", "true", "yes"},
            )
        except board.BoardQueryError as exc:
            return _json_response(
                {"ok": False, "status": "failed_validation", "safe_error": str(exc)},
                422,
            )
        return _json_response(payload)
    if route.startswith("/api/work-items/"):
        raw_suffix = route[len("/api/work-items/") :]
        events_route = raw_suffix.endswith("/events")
        raw_work_item_id = raw_suffix[: -len("/events")] if events_route else raw_suffix
        if not raw_work_item_id or "/" in raw_work_item_id:
            return _json_response({"ok": False, "status": "not_found"}, 404)
        work_item_id = unquote(raw_work_item_id)
        if events_route:
            if board.get_work_item_detail(db_path, work_item_id) is None:
                return _json_response({"ok": False, "status": "not_found"}, 404)
            query = parse_qs(parsed.query)
            try:
                payload = board.list_work_item_events(
                    db_path,
                    work_item_id,
                    limit=_query_limit(query),
                    cursor=(query.get("cursor") or [None])[0],
                )
            except board.BoardQueryError as exc:
                return _json_response(
                    {"ok": False, "status": "failed_validation", "safe_error": str(exc)},
                    422,
                )
            return _json_response(payload)
        try:
            payload = board.get_work_item_detail(db_path, work_item_id)
        except db_errors.OperationalError:
            payload = None
        if payload is not None:
            return _json_response(payload)
        try:
            payload = workbench.get_work_item(db_path, work_item_id)
        except db_errors.OperationalError:
            return _json_response(
                {
                    "ok": False,
                    "status": "unavailable",
                    "safe_error": "workbench data is unavailable for this database schema",
                },
                409,
            )
        if payload is None:
            return _json_response(
                {"ok": False, "status": "not_found", "safe_error": "work item not found"},
                404,
            )
        return _json_response(payload)
    if route == "/healthz":
        return 200, {"content-type": "text/plain; charset=utf-8"}, b"ok\n"
    if route == "/operations":
        payload = render_dashboard_html().encode("utf-8")
        return 200, {"content-type": "text/html; charset=utf-8"}, payload
    if route == "/api/work-items":
        query = parse_qs(parsed.query)
        try:
            raw_limit = (query.get("limit") or ["30"])[0]
            try:
                limit = int(raw_limit)
            except (TypeError, ValueError) as exc:
                raise workbench.WorkItemQueryError("limit must be between 1 and 100") from exc
            payload = workbench.list_work_items(
                db_path,
                bucket=(query.get("bucket") or [None])[0],
                repo=(query.get("repo") or [None])[0],
                actor=(query.get("actor") or [None])[0],
                query=(query.get("q") or [""])[0],
                sort=(query.get("sort") or ["priority"])[0],
                limit=limit,
                cursor=(query.get("cursor") or [None])[0],
            )
        except workbench.WorkItemQueryError as exc:
            return _json_response(
                {"ok": False, "status": "invalid_query", "safe_error": str(exc)},
                400,
            )
        except db_errors.OperationalError:
            return _json_response(
                {
                    "ok": False,
                    "status": "unavailable",
                    "safe_error": "workbench data is unavailable for this database schema",
                },
                409,
            )
        return _json_response(payload)
    if route in {"/data.json", "/history"}:
        payload = json.dumps(dashboard.build_dashboard_data(db_path), ensure_ascii=False).encode()
        return 200, {"content-type": "application/json; charset=utf-8"}, payload
    if route == "/artifact.txt":
        query = parse_qs(parsed.query)
        preview = dashboard.load_artifact_preview(
            db_path,
            (query.get("task_id") or [""])[0],
            (query.get("artifact_type") or [""])[0],
            artifact_id=(query.get("artifact_id") or [None])[0],
        )
        if not preview:
            return 404, {"content-type": "text/plain; charset=utf-8"}, b"artifact not found\n"
        if preview.get("missing"):
            return 404, {"content-type": "text/plain; charset=utf-8"}, dashboard.render_artifact_preview_text(preview)
        return 200, {"content-type": "text/plain; charset=utf-8"}, dashboard.render_artifact_preview_text(preview)
    return 404, {"content-type": "text/plain; charset=utf-8"}, b"not found\n"


def _json_response(payload, status=200):
    return (
        status,
        {"content-type": "application/json; charset=utf-8"},
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"),
    )


def _lower_headers(headers):
    if not headers:
        return {}
    return {str(key).lower(): str(value) for key, value in headers.items()}


def _control_request_error(control_context, headers, *, json_required):
    if not control_context:
        return _json_response(
            {"ok": False, "status": "unavailable", "safe_error": "writes are disabled"},
            503,
        )
    allowed_hosts = {urlsplit(origin).netloc for origin in control_context.allowed_origins}
    if headers.get("host") not in allowed_hosts:
        return _json_response(
            {"ok": False, "status": "forbidden", "safe_error": "invalid Host"},
            403,
        )
    if headers.get("origin") not in control_context.allowed_origins:
        return _json_response(
            {"ok": False, "status": "forbidden", "safe_error": "invalid Origin"},
            403,
        )
    if headers.get("x-robert-csrf-token") != control_context.csrf_token:
        return _json_response(
            {"ok": False, "status": "forbidden", "safe_error": "invalid CSRF token"},
            403,
        )
    content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    expected = "application/json" if json_required else "application/x-www-form-urlencoded"
    if content_type != expected:
        return _json_response(
            {"ok": False, "status": "unsupported_media_type", "safe_error": f"content-type must be {expected}"},
            415,
        )
    return None


def _work_item_context(control_context):
    return work_items.CommandContext(
        actor_kind="operator",
        actor_identity=control_context.operator_identity,
        allowed_repo_ids=control_context.allowed_repo_ids,
        allowed_workers=control_context.allowed_workers,
    )


def _handle_work_item_post(route, body, db_path, control_context, headers):
    request_error = _control_request_error(control_context, headers, json_required=True)
    if request_error:
        return request_error
    if not control_context.writes_enabled:
        return _json_response(
            {
                "ok": False,
                "status": "unavailable",
                "safe_error": control_context.write_error or "writes are disabled",
            },
            503,
        )
    idempotency_key = headers.get("x-idempotency-key")
    if not idempotency_key:
        return _json_response(
            {"ok": False, "status": "failed_validation", "safe_error": "X-Idempotency-Key is required"},
            422,
        )
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _json_response(
            {"ok": False, "status": "failed_validation", "safe_error": "invalid JSON body"},
            422,
        )
    if not isinstance(payload, dict):
        return _json_response(
            {"ok": False, "status": "failed_validation", "safe_error": "JSON body must be an object"},
            422,
        )
    try:
        if route == "/api/work-items":
            mode = payload.get("mode", "backlog")
            if mode not in {"backlog", "start"}:
                raise work_items.WorkItemValidationError("mode must be backlog or start")
            result = work_items.create_work_item(
                db_path,
                context=_work_item_context(control_context),
                repo_id=payload.get("repo_id"),
                title=payload.get("title", ""),
                description=payload.get("description", ""),
                priority=payload.get("priority", "P2"),
                routing_mode=payload.get("routing_mode", "auto"),
                requested_worker=payload.get("requested_worker"),
                start=mode == "start",
                idempotency_key=idempotency_key,
            )
            detail = board.get_work_item_detail(db_path, result["item"]["work_item_id"])
            return _json_response(detail, 201)
        prefix = "/api/work-items/"
        suffix = unquote(route[len(prefix) :])
        if not suffix.endswith("/commands"):
            return _json_response({"ok": False, "status": "not_found"}, 404)
        work_item_id = suffix[: -len("/commands")]
        result = work_items.execute_command(
            db_path,
            work_item_id,
            context=_work_item_context(control_context),
            command=payload.get("command"),
            expected_version=payload.get("expected_version"),
            expected_event_id=payload.get("expected_event_id"),
            idempotency_key=idempotency_key,
            body=payload.get("body", ""),
            routing_mode=payload.get("routing_mode"),
            requested_worker=payload.get("requested_worker"),
            title=payload.get("title"),
            description=payload.get("description"),
            priority=payload.get("priority"),
            abandon_review_run_ids=payload.get("abandon_review_run_ids"),
        )
        detail = board.get_work_item_detail(db_path, result["item"]["work_item_id"])
        return _json_response(detail)
    except work_items.WorkItemNotFoundError:
        return _json_response({"ok": False, "status": "not_found"}, 404)
    except work_items.WorkItemValidationError as exc:
        return _json_response(
            {"ok": False, "status": "failed_validation", "safe_error": str(exc)},
            422,
        )
    except work_items.WorkItemConflictError as exc:
        current = None
        if route.startswith("/api/work-items/"):
            work_item_id = unquote(route[len("/api/work-items/") :]).removesuffix("/commands")
            current = board.get_work_item_detail(db_path, work_item_id)
        return _json_response(
            {"ok": False, "status": "conflict", "safe_error": str(exc), "current": current},
            409,
        )


def _handle_task_retrigger_post(route, body, db_path, control_context, headers):
    request_error = _control_request_error(control_context, headers, json_required=True)
    if request_error:
        return request_error
    if not control_context.writes_enabled:
        return _json_response(
            {
                "ok": False,
                "status": "unavailable",
                "safe_error": control_context.write_error or "writes are disabled",
            },
            503,
        )
    idempotency_key = headers.get("x-idempotency-key")
    if not idempotency_key:
        return _json_response(
            {"ok": False, "status": "failed_validation", "safe_error": "X-Idempotency-Key is required"},
            422,
        )
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _json_response(
            {"ok": False, "status": "failed_validation", "safe_error": "invalid JSON body"},
            422,
        )
    if not isinstance(payload, dict):
        return _json_response(
            {"ok": False, "status": "failed_validation", "safe_error": "JSON body must be an object"},
            422,
        )
    task_id = unquote(route[len("/api/tasks/") : -len("/retrigger")])
    try:
        result = work_items.retrigger_failed_github_task(
            db_path,
            task_id,
            context=_work_item_context(control_context),
            idempotency_key=idempotency_key,
        )
        return _json_response(result, 202)
    except work_items.WorkItemNotFoundError:
        return _json_response({"ok": False, "status": "not_found"}, 404)
    except work_items.WorkItemValidationError as exc:
        return _json_response(
            {"ok": False, "status": "failed_validation", "safe_error": str(exc)},
            422,
        )
    except work_items.WorkItemConflictError as exc:
        return _json_response(
            {"ok": False, "status": "conflict", "safe_error": str(exc)},
            409,
        )


def handle_dashboard_post(path, body, db_path, control_context=None, request_headers=None):
    route = urlsplit(path).path
    if len(body) > 64 * 1024:
        return _json_response(
            {"ok": False, "status": "payload_too_large", "safe_error": "request body exceeds 64 KiB"},
            413,
        )
    headers = _lower_headers(request_headers)
    if route == "/api/work-items" or (route.startswith("/api/work-items/") and route.endswith("/commands")):
        return _handle_work_item_post(route, body, db_path, control_context, headers)
    if route.startswith("/api/tasks/") and route.endswith("/retrigger"):
        return _handle_task_retrigger_post(route, body, db_path, control_context, headers)
    if control_context and route.startswith("/knowledge/"):
        request_error = _control_request_error(control_context, headers, json_required=False)
        if request_error:
            return request_error
        if not control_context.writes_enabled:
            return _json_response(
                {
                    "ok": False,
                    "status": "unavailable",
                    "safe_error": control_context.write_error or "writes are disabled",
                },
                503,
            )
    try:
        form = parse_qs(body.decode("utf-8"), errors="strict")
    except UnicodeError:
        return _json_response(
            {"ok": False, "status": "failed_validation", "safe_error": "Form must be valid UTF-8."}, 422
        )

    def field(name):
        return (form.get(name) or [""])[0]

    run_now = datetime.now(timezone.utc).isoformat()
    if route.startswith("/knowledge/"):
        if not dashboard.knowledge_available(db_path):
            return _json_response(
                {
                    "ok": False,
                    "status": "unavailable",
                    "safe_error": "Knowledge tables are not available in this database yet.",
                },
                400,
            )
    if route == "/knowledge/propose":
        repo_id = field("repo_id")
        if not repo_id:
            return _json_response(
                {
                    "ok": False,
                    "status": "failed_validation",
                    "safe_error": "repo_id is required",
                },
                400,
            )
        result = knowledge_review.propose(
            db_path,
            repo_id=repo_id,
            run_now=run_now,
        )
        status = 200 if result.get("ok") else 400
        return _json_response(result, status)
    if route == "/knowledge/approve":
        result = knowledge_review.approve(
            db_path,
            candidate_id=field("candidate_id"),
            scope_type=field("scope_type"),
            scope_value=field("scope_value"),
            approved_by=field("approved_by"),
            run_now=run_now,
        )
        status = 200 if result.get("ok") else 400
        return _json_response(result, status)
    if route == "/knowledge/reject":
        result = knowledge_review.reject(
            db_path,
            candidate_id=field("candidate_id"),
            reviewer=field("reviewer"),
            review_note=field("review_note"),
            run_now=run_now,
        )
        status = 200 if result.get("ok") else 400
        return _json_response(result, status)
    return _json_response(
        {
            "ok": False,
            "status": "unsupported",
            "safe_error": "unsupported dashboard action",
        },
        404,
    )


def make_handler(db_path, control_context=None):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, headers, payload):
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            context = control_context or build_read_only_control_context(
                db_path, host=self.server.server_address[0], port=self.server.server_address[1]
            )
            if self.headers.get("Host") not in {urlsplit(origin).netloc for origin in context.allowed_origins}:
                self._send(*_json_response({"ok": False, "status": "forbidden", "safe_error": "invalid Host"}, 403))
                return
            try:
                response = build_http_response(
                    self.path,
                    db_path,
                    control_context=control_context,
                )
            except Exception:
                response = _json_response(
                    {
                        "ok": False,
                        "status": "internal_error",
                        "safe_error": "request failed",
                    },
                    500,
                )
            self._send(*response)

        def do_POST(self):
            try:
                length = int(self.headers.get("content-length") or "0")
            except ValueError:
                self._send(
                    *_json_response(
                        {
                            "ok": False,
                            "status": "failed_validation",
                            "safe_error": "invalid content-length",
                        },
                        400,
                    )
                )
                return
            if length < 0:
                self._send(
                    *_json_response(
                        {
                            "ok": False,
                            "status": "failed_validation",
                            "safe_error": "invalid content-length",
                        },
                        400,
                    )
                )
                return
            if length > 64 * 1024:
                self.close_connection = True
                self._send(
                    *_json_response(
                        {
                            "ok": False,
                            "status": "payload_too_large",
                            "safe_error": "request body exceeds 64 KiB",
                        },
                        413,
                    )
                )
                return
            try:
                body = self.rfile.read(length)
                response = handle_dashboard_post(
                    self.path,
                    body,
                    db_path,
                    control_context=control_context,
                    request_headers=dict(self.headers.items()),
                )
            except Exception:
                response = _json_response(
                    {
                        "ok": False,
                        "status": "internal_error",
                        "safe_error": "request failed",
                    },
                    500,
                )
            self._send(*response)

        def log_message(self, *_args):
            return

    return Handler


def _allowed_web_origins(host, port, public_origin=None):
    bind_host = f"[{host}]" if ":" in host else host
    origins = {f"http://{bind_host}" if port == 80 else f"http://{bind_host}:{port}"}
    if public_origin is not None:
        error = "--public-origin must be an HTTP(S) origin without credentials, path, query or fragment"
        try:
            parsed = urlsplit(public_origin)
            # urlsplit strips some control characters; reject them before parsing.
            if (
                not public_origin
                or any(character.isspace() or ord(character) < 32 for character in public_origin)
                or any(character in public_origin for character in ("\\", "%", "*", "?", "#"))
                or parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in {"", "/"}
            ):
                raise ValueError(error)
            public_port = parsed.port  # Validate the port, including its range.
        except ValueError as exc:
            raise ValueError(error) from exc
        public_host = parsed.hostname.lower()
        if ":" in public_host:
            public_host = f"[{public_host}]"
        default_port = 443 if parsed.scheme == "https" else 80
        authority = public_host if public_port in {None, default_port} else f"{public_host}:{public_port}"
        origins.add(f"{parsed.scheme}://{authority}")
    return frozenset(origins)


def build_control_context_from_config(
    config_path,
    *,
    host,
    port,
    operator_identity,
    csrf_token=None,
    public_origin=None,
):
    allowed_origins = _allowed_web_origins(host, port, public_origin)
    config_result = validate_config.validate_config(config_path, skip_external=True)
    if not config_result["ok"]:
        raise ValueError(config_result["safe_error"])
    db_path = config_result["db_path"]
    allowed_repo_ids = web_setup.prepare_repositories(config_result)
    return ControlContext(
        db_path=db_path,
        operator_identity=operator_identity,
        allowed_repo_ids=allowed_repo_ids,
        allowed_workers=frozenset(worker["name"] for worker in config_result["workers"]),
        allowed_origins=allowed_origins,
        csrf_token=csrf_token or secrets.token_urlsafe(32),
        writes_enabled=True,
        write_error=None,
    )


def build_read_only_control_context(db_path, *, host, port, public_origin=None):
    return ControlContext(
        db_path=str(db_path),
        operator_identity="local-operator",
        allowed_repo_ids=frozenset(),
        allowed_workers=frozenset(),
        allowed_origins=_allowed_web_origins(host, port, public_origin),
        csrf_token=secrets.token_urlsafe(32),
        writes_enabled=False,
        write_error="Start with --config to enable writes.",
    )


def serve(db_path, host="127.0.0.1", port=8765, control_context=None):
    if control_context is None:
        control_context = build_read_only_control_context(db_path, host=host, port=port)
    server = ThreadingHTTPServer(
        (host, port),
        make_handler(db_path, control_context),
    )
    server.serve_forever()


def main(argv=None):
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--db")
    source.add_argument("--config")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--public-origin",
        help="Browser-facing HTTP(S) origin for an authenticated reverse proxy.",
    )
    parser.add_argument("--operator", default="local-operator")
    parser.add_argument("--writable", action="store_true")
    parser.add_argument("--allow-remote", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    options = validate_server_options(
        args.host,
        args.writable,
        args.allow_remote,
    )
    if not options["ok"]:
        return emit(options, 4)
    if args.writable and not args.config:
        return emit(
            {
                "ok": False,
                "status": "failed_input",
                "safe_error": "--writable requires --config",
            },
            3,
        )
    try:
        _allowed_web_origins(args.host, args.port, args.public_origin)
    except ValueError as exc:
        return emit({"ok": False, "status": "failed_input", "safe_error": str(exc)}, 3)
    if args.config:
        config_result = validate_config.validate_config(
            args.config,
            skip_external=True,
        )
        if not config_result["ok"]:
            return emit(config_result, 3)
        db_path = config_result["db_path"]
        if args.writable:
            try:
                control_context = build_control_context_from_config(
                    args.config,
                    host=args.host,
                    port=args.port,
                    operator_identity=args.operator,
                    public_origin=args.public_origin,
                )
            except ValueError as exc:
                return emit(
                    {
                        "ok": False,
                        "status": "failed_config",
                        "safe_error": str(exc),
                    },
                    3,
                )
        else:
            control_context = build_read_only_control_context(
                db_path,
                host=args.host,
                port=args.port,
                public_origin=args.public_origin,
            )
    elif args.db:
        db_path = args.db
        control_context = build_read_only_control_context(
            db_path,
            host=args.host,
            port=args.port,
            public_origin=args.public_origin,
        )
    else:
        return emit(
            {
                "ok": False,
                "status": "failed_input",
                "safe_error": "--config or --db is required",
            },
            3,
        )
    if not args.writable:
        try:
            with closing(
                db_database.connect(Path(db_path).expanduser().resolve().as_uri() + "?mode=ro", uri=True)
            ) as conn:
                if workbench.REQUIRED_TABLES - sqlite_master.table_names(conn):
                    raise ValueError("Database schema is incomplete; initialize Robert first.")
        except (db_errors.Error, OSError, ValueError):
            return emit(
                {
                    "ok": False,
                    "status": "failed_input",
                    "safe_error": "Database is missing, unreadable, or incomplete; initialize Robert first.",
                },
                3,
            )
    if args.json:
        return emit({"ok": True, "status": "ready", "data": dashboard.build_dashboard_data(db_path)})
    serve(db_path, args.host, args.port, control_context=control_context)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
