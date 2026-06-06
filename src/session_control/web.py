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
from session_control.claude_status import ClaudeStatusPoller
from session_control.config import AppConfig
from session_control.models import SessionRecord
from session_control.scanner import PROVIDERS, SessionScanner

CLAUDE_TOKEN_FIELDS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
)


def create_app(
    config: AppConfig | None = None,
    scanner: SessionScanner | None = None,
    actions: SessionActionService | None = None,
    claude_status_poller: ClaudeStatusPoller | None = None,
) -> Flask:
    app_config = config or AppConfig.from_env()
    app_scanner = scanner or SessionScanner(app_config)
    app_actions = actions or SessionActionService(app_config, app_scanner)
    app_claude_status = claude_status_poller or _claude_status_poller(app_config)
    if app_claude_status:
        app_claude_status.start()
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
            codex_usage=_codex_usage_summary(report.sessions),
            claude_usage=_claude_usage_summary(report.sessions),
            claude_status=app_claude_status.snapshot() if app_claude_status else None,
            csrf_token=_csrf_token(),
            webterm_url=app_config.webterm_url,
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
            webterm_url=app_config.webterm_url,
        )

    @app.get("/api/sessions")
    def api_sessions():
        report = app_scanner.scan()
        return jsonify(report.to_dict())

    @app.get("/api/claude/status")
    def api_claude_status():
        if not app_claude_status:
            return jsonify(
                {
                    "enabled": False,
                    "message": "Claude status polling is disabled.",
                }
            )
        return jsonify(
            {
                "enabled": True,
                "status": app_claude_status.snapshot(),
            }
        )

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True})

    @app.post("/sessions/<public_id>/open")
    def open_session(public_id: str):
        if not app_config.webterm_url:
            flash("Web terminal URL is not configured (SESSION_CONTROL_WEBTERM_URL).", "error")
            return redirect(request.referrer or url_for("index"))
        try:
            app_actions.open_in_webterm(public_id)
        except SessionActionError as exc:
            flash(str(exc), "error")
            return redirect(request.referrer or url_for("index"))
        return redirect(app_config.webterm_url)

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

    @app.template_filter("integer")
    def _integer_filter(value: int | None) -> str:
        if value is None:
            return "unknown"
        return f"{int(value):,}"

    @app.template_filter("percent")
    def _percent_filter(value: float | None) -> str:
        if value is None:
            return "unknown"
        return f"{float(value):.0f}%"

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


def _claude_status_poller(config: AppConfig) -> ClaudeStatusPoller | None:
    if not config.claude_status_poll_enabled:
        return None
    return ClaudeStatusPoller(
        command=config.claude_status_command,
        interval_seconds=config.claude_status_poll_interval_seconds,
        timeout_seconds=config.claude_status_timeout_seconds,
    )


def _codex_usage_summary(sessions: tuple[SessionRecord, ...]) -> dict | None:
    latest: tuple[datetime, dict] | None = None
    for session_record in sessions:
        if session_record.provider != "codex":
            continue
        usage = session_record.metadata.get("token_usage")
        if not isinstance(usage, dict):
            continue
        parsed = _parse_datetime(str(usage.get("updated_at") or session_record.updated_at))
        if not parsed:
            continue
        rate_limits = session_record.metadata.get("rate_limits")
        summary = {
            "updated_at": usage.get("updated_at") or session_record.updated_at,
            "last_total_tokens": _nested_int(usage, "last", "total_tokens"),
            "session_total_tokens": _nested_int(usage, "total", "total_tokens"),
            "context_window": usage.get("model_context_window"),
            "plan_type": rate_limits.get("plan_type") if isinstance(rate_limits, dict) else "",
            "primary_used_percent": _nested_value(rate_limits, "primary", "used_percent"),
            "primary_resets_at": _nested_value(rate_limits, "primary", "resets_at"),
            "secondary_used_percent": _nested_value(rate_limits, "secondary", "used_percent"),
            "secondary_resets_at": _nested_value(rate_limits, "secondary", "resets_at"),
        }
        if latest is None or parsed > latest[0]:
            latest = (parsed, summary)
    return latest[1] if latest else None


def _claude_usage_summary(sessions: tuple[SessionRecord, ...]) -> dict | None:
    latest: tuple[datetime, dict] | None = None
    for session_record in sessions:
        if session_record.provider != "claude":
            continue
        usage = session_record.metadata.get("token_usage")
        if not isinstance(usage, dict):
            continue
        parsed = _parse_datetime(str(usage.get("updated_at") or session_record.updated_at))
        if not parsed:
            continue
        summary = {
            "updated_at": usage.get("updated_at") or session_record.updated_at,
            "last_total_tokens": _token_total(usage.get("last")),
            "session_total_tokens": _token_total(usage.get("total")),
            "input_tokens": _nested_int(usage, "total", "input_tokens"),
            "cache_creation_input_tokens": _nested_int(
                usage, "total", "cache_creation_input_tokens"
            ),
            "cache_read_input_tokens": _nested_int(usage, "total", "cache_read_input_tokens"),
            "output_tokens": _nested_int(usage, "total", "output_tokens"),
            "model": usage.get("model") or session_record.metadata.get("model") or "",
            "service_tier": usage.get("service_tier") or "",
            "speed": usage.get("speed") or "",
            "limits_note": usage.get("limits_note") or "Claude limits are not recorded locally.",
        }
        if latest is None or parsed > latest[0]:
            latest = (parsed, summary)
    return latest[1] if latest else None


def _token_total(value: object) -> int | None:
    if not isinstance(value, dict):
        return None
    total = 0
    found = False
    for field in CLAUDE_TOKEN_FIELDS:
        amount = value.get(field)
        if amount is None:
            continue
        try:
            total += int(amount)
        except (TypeError, ValueError):
            continue
        found = True
    return total if found else None


def _nested_int(value: object, first: str, second: str) -> int | None:
    nested = _nested_value(value, first, second)
    if nested is None:
        return None
    try:
        return int(nested)
    except (TypeError, ValueError):
        return None


def _nested_value(value: object, first: str, second: str) -> object | None:
    if not isinstance(value, dict):
        return None
    nested = value.get(first)
    if not isinstance(nested, dict):
        return None
    return nested.get(second)


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
