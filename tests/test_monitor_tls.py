"""Boundaries for the WebTerm monitor's client certificate."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.request import Request

import pytest

from session_control import connection_monitor as monitor
from session_control.web import _webterm_monitor_ssl_context


def test_client_context_is_used_only_for_configured_webterm(monkeypatch):
    webterm = "https://webterm.example.test/status"
    other = "https://other.example.test/status"
    identity = object()
    seen = []

    def fake_probe(target, *, ssl_context=None):
        seen.append((target.host, ssl_context))
        return monitor.ProbeResult(0.0, datetime.now(timezone.utc), True, 1.0)

    monkeypatch.setattr(monitor, "_probe", fake_probe)
    runner = monitor.ConnectionMonitor(
        [
            monitor.TargetConfig("webterm", "http", webterm),
            monitor.TargetConfig("other", "http", other),
        ],
        webterm_url=webterm,
        webterm_ssl_context=identity,
    )
    runner._probe_all()
    assert seen == [(webterm, identity), (other, None)]


def test_client_identity_redirects_only_within_same_https_origin():
    handler = monitor._SameOriginRedirectHandler(("https", "webterm.example.test", 443))
    request = Request("https://webterm.example.test/status")
    assert (
        handler.redirect_request(
            request, None, 302, "Found", {}, "https://webterm.example.test/login"
        )
        is not None
    )
    for destination in (
        "https://other.example.test/login",
        "https://webterm.example.test:444/login",
        "http://webterm.example.test/login",
    ):
        assert handler.redirect_request(request, None, 302, "Found", {}, destination) is None


def test_client_cert_and_key_must_be_paired_and_https():
    config = SimpleNamespace(
        monitor_webterm_client_cert="cert.pem",
        monitor_webterm_client_key=None,
        monitor_webterm_ca_cert=None,
        webterm_url="https://webterm.example.test",
    )
    with pytest.raises(ValueError, match="must be set together"):
        _webterm_monitor_ssl_context(config)
    config.monitor_webterm_client_key = "key.pem"
    config.webterm_url = "http://webterm.example.test"
    with pytest.raises(ValueError, match="require an HTTPS"):
        _webterm_monitor_ssl_context(config)
