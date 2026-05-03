"""Flask web UI for session-control."""

from __future__ import annotations

import ipaddress
import secrets
from collections import Counter
from datetime import datetime, timezone
from http import HTTPStatus
from urllib.parse import urlparse

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from session_control.actions import SessionActionError, SessionActionService
from session_control.config import AppConfig
from session_control.models import SessionRecord
from session_control.scanner import PROVIDERS, SessionScanner


def create_app(
    config: AppConfig | None = None,
    scanner: SessionScanner | None = None,
    actions: SessionActionService | None = None,
) -> Flask:
    app_config = config or AppConfig.from_env()
    app_scanner = scanner or SessionScanner(app_config)
    app_actions = actions or SessionActionService(app_config, app_scanner)
    app = Flask(__name__)
    app.secret_key = app_config.secret_key

    @app.before_request
    def _protect_state_changing_requests() -> None:
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return
        source = _request_source(request)
        if source and not _same_origin(source, app_config, request):
            abort(HTTPStatus.FORBIDDEN, description="Cross-origin request blocked.")
        token = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
        if not token or token != session.get("csrf_token"):
            abort(HTTPStatus.FORBIDDEN, description="Missing or invalid CSRF token.")

    @app.get("/")
    def index():
        selected_provider = request.args.get("provider", "").strip()
        query = request.args.get("q", "").strip()
        providers = (selected_provider,) if selected_provider in PROVIDERS else None
        report = app_scanner.scan(providers=providers)
        sessions = _filter_sessions(report.sessions, query)
        counts = Counter(session.provider for session in report.sessions)
        return render_template(
            "index.html",
            sessions=sessions,
            errors=report.errors,
            providers=PROVIDERS,
            counts=counts,
            selected_provider=selected_provider,
            query=query,
            csrf_token=_csrf_token(),
        )

    @app.get("/sessions/<public_id>")
    def detail(public_id: str):
        report = app_scanner.scan()
        session_record = _find_session(report.sessions, public_id)
        if not session_record:
            abort(HTTPStatus.NOT_FOUND)
        return render_template(
            "detail.html",
            session=session_record,
            csrf_token=_csrf_token(),
        )

    @app.get("/api/sessions")
    def api_sessions():
        report = app_scanner.scan()
        return jsonify(report.to_dict())

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True})

    @app.post("/sessions/<public_id>/delete")
    def delete_session(public_id: str):
        try:
            result = app_actions.delete(public_id)
        except SessionActionError as exc:
            flash(str(exc), "error")
            return redirect(request.referrer or url_for("index"))
        flash(
            f"Deleted {result.moved_count} item(s) for {result.session.provider} session "
            f"{result.session.session_id}.",
            "success",
        )
        return redirect(url_for("index"))

    @app.template_filter("datetime")
    def _datetime_filter(value: str) -> str:
        if not value:
            return "unknown"
        parsed = _parse_datetime(value)
        if not parsed:
            return value
        return parsed.astimezone().strftime("%Y-%m-%d %H:%M")

    @app.template_filter("age")
    def _age_filter(value: str) -> str:
        parsed = _parse_datetime(value)
        if not parsed:
            return "unknown"
        delta = datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)
        days = delta.days
        if days >= 365:
            return f"{days // 365}y"
        if days >= 30:
            return f"{days // 30}mo"
        if days >= 1:
            return f"{days}d"
        hours = max(0, delta.seconds // 3600)
        if hours:
            return f"{hours}h"
        return "now"

    @app.template_filter("bytes")
    def _bytes_filter(value: int) -> str:
        amount = float(value or 0)
        for unit in ("B", "KB", "MB", "GB"):
            if amount < 1024 or unit == "GB":
                return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
            amount /= 1024
        return f"{amount:.1f} GB"

    return app


def _filter_sessions(sessions: tuple[SessionRecord, ...], query: str) -> list[SessionRecord]:
    if not query:
        return list(sessions)
    needle = query.lower()
    return [
        item
        for item in sessions
        if needle in item.title.lower()
        or needle in item.workspace.lower()
        or needle in item.preview.lower()
        or needle in item.session_id.lower()
    ]


def _find_session(sessions: tuple[SessionRecord, ...], public_id: str) -> SessionRecord | None:
    return next((item for item in sessions if item.public_id == public_id), None)


def _csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return str(token)


def _same_origin(origin_or_referrer: str, config: AppConfig, flask_request) -> bool:
    source = _normalize_origin(origin_or_referrer)
    if not source:
        return False
    allowed = {
        origin
        for origin in (
            _normalize_origin(flask_request.host_url),
            _normalize_origin(config.public_origin or ""),
            *(_normalize_origin(value) for value in config.allowed_origins),
        )
        if origin
    }
    forwarded_host = flask_request.headers.get("X-Forwarded-Host", "").split(",", 1)[0].strip()
    forwarded_proto = flask_request.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip()
    if forwarded_host:
        forwarded_scheme = forwarded_proto or flask_request.scheme
        allowed.add(_normalize_origin(f"{forwarded_scheme}://{forwarded_host}"))
    allowed.update(_loopback_alias_origins(flask_request))
    return source in allowed


def _request_source(flask_request) -> str:
    origin = str(flask_request.headers.get("Origin") or "").strip()
    if origin and origin.lower() != "null":
        return origin
    return str(flask_request.headers.get("Referer") or "").strip()


def _loopback_alias_origins(flask_request) -> set[str]:
    parsed = urlparse(flask_request.host_url)
    scheme = parsed.scheme.lower()
    host = parsed.hostname or ""
    port = parsed.port
    if not _is_loopback_host(host):
        return set()
    suffix = f":{port}" if port else ""
    return {
        _normalize_origin(f"{scheme}://127.0.0.1{suffix}"),
        _normalize_origin(f"{scheme}://localhost{suffix}"),
        _normalize_origin(f"{scheme}://[::1]{suffix}"),
    }


def _is_loopback_host(host: str) -> bool:
    candidate = host.strip().strip("[]")
    if candidate.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def _normalize_origin(value: str) -> str:
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        return ""
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return ""
    port = parsed.port
    default_port = 443 if scheme == "https" else 80 if scheme == "http" else None
    if port and port != default_port:
        return f"{scheme}://{hostname}:{port}"
    return f"{scheme}://{hostname}"


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
