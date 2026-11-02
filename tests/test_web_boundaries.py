"""The web layer's trust boundaries: CSRF and origin checks, federation auth, helpers.

Every state-changing request needs a same-origin source and a CSRF token, except the
federation ingest route, which instead needs the node's bearer token. The actions object
is replaced, so a request that passes these checks still cannot touch a session file or
tmux; what matters here is which requests are let through.
"""

from __future__ import annotations

import json
import ssl
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import pytest

from session_control import web
from session_control.actions import SessionActionError
from session_control.federation import build_inventory
from session_control.web import create_app
from tests.helpers import seed_claude, seed_codex, seed_continue


class FakeActions:
    def __init__(self):
        self.calls: list[tuple] = []
        self.fail: str | None = None
        self.errors: tuple[str, ...] = ()
        self.opened = 1
        self.digest_store = SimpleNamespace(coverage=lambda: {"categories": [], "repositories": []})

    def _maybe_fail(self):
        if self.fail:
            raise SessionActionError(self.fail)

    def open_in_webterm(self, public_id, *, codex_permission_preset=None):
        self.calls.append(("open", public_id, codex_permission_preset))
        self._maybe_fail()
        return SimpleNamespace(window_index=4)

    def open_many_in_webterm(self, public_ids, *, codex_permission_preset=None):
        self.calls.append(("open_many", public_ids, codex_permission_preset))
        items = tuple(SimpleNamespace(window_index=i) for i in range(self.opened))
        return SimpleNamespace(opened=items, errors=self.errors)

    def delete(self, public_id):
        self.calls.append(("delete", public_id))
        self._maybe_fail()
        session = SimpleNamespace(provider="codex", session_id="abc")
        return SimpleNamespace(moved_count=2, session=session)

    def delete_many(self, public_ids):
        self.calls.append(("delete_many", public_ids))
        deleted = tuple(SimpleNamespace(moved_count=1) for _ in public_ids if self.opened)
        return SimpleNamespace(deleted=deleted, errors=self.errors)


@pytest.fixture
def actions():
    return FakeActions()


@pytest.fixture
def client(app_config, actions):
    config = replace(app_config, webterm_url="https://term.example.test")
    app = create_app(config, actions=actions)
    client = app.test_client()
    client.get("/")
    return client


def token(client) -> str:
    with client.session_transaction() as session:
        return session["csrf_token"]


def post(client, path, data=None, headers=None, with_token=True):
    data = dict(data or {})
    if with_token:
        data["csrf_token"] = token(client)
    return client.post(path, data=data, headers=headers or {})


# --------------------------------------------------------------------------- CSRF and origin


def test_a_post_without_a_csrf_token_is_forbidden_and_does_nothing(client, actions):
    response = post(client, "/sessions/x/delete", with_token=False)
    assert response.status_code == 403
    assert actions.calls == []


def test_a_wrong_csrf_token_is_forbidden(client, actions):
    response = client.post("/sessions/x/delete", data={"csrf_token": "guess"})
    assert response.status_code == 403 and actions.calls == []


def test_the_csrf_token_may_come_in_a_header(client, actions):
    response = client.post("/sessions/x/delete", headers={"X-CSRF-Token": token(client)})
    assert response.status_code == 302
    assert actions.calls == [("delete", "x")]


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
def test_other_state_changing_methods_are_checked_too(client, method):
    # 403 from the guard, before the router can answer 405
    assert getattr(client, method)("/sessions/x/delete").status_code == 403


def test_a_cross_origin_post_is_blocked_even_with_a_valid_token(client, actions):
    for header in ({"Origin": "https://evil.example"}, {"Referer": "https://evil.example/page"}):
        response = post(client, "/sessions/x/delete", headers=header)
        assert response.status_code == 403
    assert actions.calls == []


def test_a_null_origin_falls_back_to_the_referer(client, actions):
    response = post(
        client, "/sessions/x/delete", headers={"Origin": "null", "Referer": "https://evil.example/"}
    )
    assert response.status_code == 403 and actions.calls == []


def test_the_same_origin_and_loopback_aliases_are_allowed(client, actions):
    for header in (
        {"Origin": "http://localhost"},
        {"Origin": "http://127.0.0.1"},
        {"Referer": "http://localhost/sessions/x"},
    ):
        assert post(client, "/sessions/x/delete", headers=header).status_code == 302
    assert len(actions.calls) == 3


def test_configured_public_and_allowed_origins_are_accepted(app_config, actions):
    config = replace(
        app_config,
        public_origin="https://sessions.example.test",
        allowed_origins=("https://alt.example.test:8443",),
    )
    client = create_app(config, actions=actions).test_client()
    client.get("/")
    for origin in ("https://sessions.example.test", "https://alt.example.test:8443"):
        assert post(client, "/sessions/x/delete", headers={"Origin": origin}).status_code == 302
    for origin in ("https://alt.example.test", "http://sessions.example.test"):
        assert post(client, "/sessions/x/delete", headers={"Origin": origin}).status_code == 403


def test_a_forwarded_host_is_trusted_with_its_forwarded_scheme(client, actions):
    headers = {
        "Origin": "https://front.example.test",
        "X-Forwarded-Host": "front.example.test, internal",
        "X-Forwarded-Proto": "https",
    }
    assert post(client, "/sessions/x/delete", headers=headers).status_code == 302
    headers["Origin"] = "http://front.example.test"
    assert post(client, "/sessions/x/delete", headers=headers).status_code == 403


def test_a_forwarded_host_without_a_forwarded_scheme_uses_the_requests_own(client, actions):
    # the request is plain http, so only an http origin for the forwarded host is accepted
    base = {"X-Forwarded-Host": "front.example.test"}
    assert (
        post(
            client, "/sessions/x/delete", headers={**base, "Origin": "http://front.example.test"}
        ).status_code
        == 302
    )
    assert (
        post(
            client, "/sessions/x/delete", headers={**base, "Origin": "https://front.example.test"}
        ).status_code
        == 403
    )


def test_a_get_needs_no_token(client):
    assert client.get("/healthz").get_json() == {"ok": True}


# --------------------------------------------------------------------------- actions


def test_open_without_a_webterm_url_flashes_and_opens_nothing(app_config, actions):
    client = create_app(app_config, actions=actions).test_client()
    client.get("/")
    response = post(client, "/sessions/x/open")
    assert response.status_code == 302 and actions.calls == []
    with client.session_transaction() as session:
        assert any("not configured" in m for _, m in session["_flashes"])


def test_open_redirects_to_the_terminal_window_with_the_chosen_preset(client, actions):
    response = post(client, "/sessions/x/open", {"codex_permission_preset": "read-only"})
    assert response.headers["Location"] == "https://term.example.test/term?window=4"
    assert actions.calls[0][0:2] == ("open", "x")


def test_open_uses_the_configured_preset_when_none_is_posted(client, actions):
    post(client, "/sessions/x/open")
    assert actions.calls[0][2] == "default"


def test_a_failed_open_flashes_the_reason_and_goes_back(client, actions):
    actions.fail = "tmux is not running"
    response = post(client, "/sessions/x/open", headers={"Referer": "http://localhost/sessions/x"})
    assert response.headers["Location"].endswith("/sessions/x")
    with client.session_transaction() as session:
        assert ("error", "tmux is not running") in session["_flashes"]


def test_bulk_open_needs_a_selection(client, actions):
    response = post(client, "/sessions/bulk/open")
    assert response.status_code == 302 and actions.calls == []
    with client.session_transaction() as session:
        assert any("Select at least one" in m for _, m in session["_flashes"])


def test_bulk_open_dedupes_ids_and_goes_to_the_last_window(client, actions):
    actions.opened = 2
    actions.errors = ("b: not found",)
    response = client.post(
        "/sessions/bulk/open",
        data={"csrf_token": token(client), "session_id": ["a", "a", "", "b"]},
    )
    assert actions.calls[0][1] == ("a", "b")
    assert response.headers["Location"] == "https://term.example.test/term?window=1"
    with client.session_transaction() as session:
        flashes = session["_flashes"]
    assert ("success", "Opened 2 selected session(s).") in flashes
    assert ("error", "b: not found") in flashes


def test_bulk_open_with_nothing_opened_goes_back(client, actions):
    actions.opened = 0
    actions.errors = ("x: failed",)
    response = client.post(
        "/sessions/bulk/open", data={"csrf_token": token(client), "session_id": "x"}
    )
    assert "term" not in response.headers["Location"]


def test_bulk_open_without_a_webterm_url_is_refused(app_config, actions):
    client = create_app(app_config, actions=actions).test_client()
    client.get("/")
    post(client, "/sessions/bulk/open", {"session_id": "x"})
    assert actions.calls == []


def test_delete_reports_what_moved_and_a_failure_goes_back(client, actions):
    response = post(client, "/sessions/x/delete")
    assert response.headers["Location"].endswith("/")
    with client.session_transaction() as session:
        assert any("Deleted 2 item(s) for codex session abc" in m for _, m in session["_flashes"])
    actions.fail = "Session was not found."
    post(client, "/sessions/x/delete", headers={"Referer": "http://localhost/sessions/x"})


def test_bulk_delete_needs_a_selection_and_reports_each_error(client, actions):
    post(client, "/sessions/bulk/delete")
    assert actions.calls == []
    actions.errors = ("b: gone",)
    client.post(
        "/sessions/bulk/delete", data={"csrf_token": token(client), "session_id": ["a", "b"]}
    )
    assert actions.calls == [("delete_many", ("a", "b"))]
    with client.session_transaction() as session:
        flashes = session["_flashes"]
    assert any("Deleted 2 selected session(s), moving 2 item(s) to trash." in m for _, m in flashes)
    assert ("error", "b: gone") in flashes


# --------------------------------------------------------------------------- pages and APIs


def test_an_unknown_session_detail_is_a_404(app_config):
    assert create_app(app_config).test_client().get("/sessions/nope").status_code == 404


def test_a_known_session_has_a_detail_page(app_config):
    seed_continue(app_config.continue_root)
    client = create_app(app_config).test_client()
    public_id = client.get("/api/sessions").get_json()["sessions"][0]["public_id"]
    page = client.get(f"/sessions/{public_id}")
    assert page.status_code == 200 and "Continue repo summary" in page.get_data(as_text=True)


def test_ecu_pages_show_the_document_or_an_error_status(app_config):
    ok = create_app(app_config, ecu_reader=lambda: ({"leases": []}, "")).test_client()
    assert ok.get("/api/ecu").get_json() == {"leases": []}
    assert ok.get("/ecu").status_code == 200
    down = create_app(app_config, ecu_reader=lambda: (None, "ecu not found")).test_client()
    response = down.get("/api/ecu")
    assert response.status_code == 503 and response.get_json() == {"error": "ecu not found"}
    assert down.get("/ecu").status_code == 200


def test_connections_pages_work_with_and_without_a_monitor(app_config):
    client = create_app(app_config).test_client()
    assert client.get("/api/connections").get_json()["enabled"] is False
    assert client.get("/connections").status_code == 200
    monitor = SimpleNamespace(
        start=lambda: None, snapshot=lambda: {"targets": [], "events": [], "interval_seconds": 5}
    )
    client = create_app(app_config, connection_monitor=monitor).test_client()
    assert client.get("/api/connections").get_json() == {
        "enabled": True, "targets": [], "events": [], "interval_seconds": 5,
    }  # fmt: skip


def test_claude_status_is_reported_only_when_polling_is_enabled(app_config):
    assert (
        create_app(app_config).test_client().get("/api/claude/status").get_json()["enabled"]
        is False
    )
    poller = SimpleNamespace(start=lambda: None, snapshot=lambda: {"ok": True})
    enabled = create_app(app_config, claude_status_poller=poller).test_client()
    assert enabled.get("/api/claude/status").get_json() == {"enabled": True, "status": {"ok": True}}


def test_coverage_and_federation_pages_render(app_config):
    client = create_app(app_config).test_client()
    assert client.get("/coverage").status_code == 200
    assert client.get("/federation").status_code == 200


def test_portfolio_assets_are_served_with_no_cache_and_unknown_ones_are_404(app_config):
    client = create_app(app_config).test_client()
    assert client.get("/_portfolio/does-not-exist.js").status_code == 404
    with mock.patch.object(web, "_portfolio_site") as site:
        site.return_value.asset.return_value = (b"body{}", "text/css")
        response = client.get("/_portfolio/theme.css")
    assert response.get_data() == b"body{}" and response.headers["Cache-Control"] == "no-cache"


# --------------------------------------------------------------------------- federation ingest


@pytest.fixture
def federated(app_config, tmp_path):
    seed_codex(app_config.codex_root)
    config = replace(
        app_config, federation_store_dir=tmp_path / "fed", federation_tokens={"air": "s3cret"}
    )
    client = create_app(config).test_client()
    payload = build_inventory(
        __import__("session_control.scanner", fromlist=["SessionScanner"])
        .SessionScanner(config)
        .scan(),
        node_id="air",
        node_label="Air",
    )
    return client, payload, config


def ingest(client, payload, node="air", auth="Bearer s3cret"):
    headers = {"Authorization": auth} if auth is not None else {}
    return client.post(f"/api/federation/v1/inventories/{node}", json=payload, headers=headers)


def test_ingest_needs_no_csrf_but_needs_the_right_bearer_token(federated):
    client, payload, config = federated
    assert ingest(client, payload).status_code == 201
    assert (config.federation_store_dir / "air.json").exists()


@pytest.mark.parametrize(
    "auth", [None, "", "s3cret", "Basic s3cret", "Bearer wrong", "Bearer ", "bearer s3cret"]
)
def test_ingest_refuses_a_missing_or_wrong_token(federated, auth):
    client, payload, config = federated
    assert ingest(client, payload, auth=auth).status_code == 401
    assert not config.federation_store_dir.exists()


def test_ingest_refuses_a_node_with_no_token_and_a_server_with_no_store(federated, app_config):
    client, payload, _ = federated
    assert ingest(client, payload, node="pro").status_code == 401
    unconfigured = create_app(
        replace(app_config, federation_tokens={"air": "s3cret"})
    ).test_client()
    assert ingest(unconfigured, payload).status_code == 404


def test_ingest_matches_the_node_in_the_path_case_insensitively(federated):
    client, payload, _ = federated
    assert ingest(client, payload, node="AIR").status_code == 201


def test_ingest_refuses_a_payload_for_another_node_or_a_malformed_one(federated):
    client, payload, config = federated
    other = json.loads(json.dumps(payload))
    other["node"]["id"] = "pro"
    assert ingest(client, other).status_code == 400
    assert ingest(client, {"schema_version": 1}).status_code == 400
    assert not (config.federation_store_dir / "pro.json").exists()


def test_ingest_reports_the_stored_session_count(federated):
    client, payload, _ = federated
    body = ingest(client, payload).get_json()
    assert body["ok"] is True and body["node"]["id"] == "air"
    assert body["session_count"] == len(payload["sessions"])


def test_the_export_endpoint_is_read_only_metadata(federated):
    client, _, _ = federated
    body = client.get("/api/federation/v1/inventory").get_json()
    assert "preview" not in json.dumps(body) and body["node"]["id"]


# --------------------------------------------------------------------------- helpers


def request_stub(host_url="http://localhost:5420/", headers=None, scheme="http"):
    return SimpleNamespace(host_url=host_url, headers=headers or {}, scheme=scheme)


@pytest.mark.parametrize(
    ("value", "origin"),
    [
        ("https://Host.Example/path?q=1", "https://host.example"),
        ("https://host.example:443/", "https://host.example"),
        ("http://host.example:80", "http://host.example"),
        ("http://host.example:8080", "http://host.example:8080"),
        ("https://host.example:8443", "https://host.example:8443"),
        ("ftp://host.example:21", "ftp://host.example:21"),
        ("host.example", ""),
        ("", ""),
        ("https://", ""),
    ],
)
def test_origins_are_normalised(value, origin):
    assert web._normalize_origin(value) == origin


@pytest.mark.parametrize(
    ("host", "loopback"),
    [("localhost", True), ("LOCALHOST", True), ("127.0.0.1", True), ("[::1]", True), ("::1", True),
     ("10.0.0.1", False), ("example.com", False), ("", False)],
)  # fmt: skip
def test_loopback_hosts(host, loopback):
    assert web._is_loopback_host(host) is loopback


def test_loopback_aliases_only_exist_for_a_loopback_request():
    assert "http://localhost:5420" in web._loopback_alias_origins(request_stub())
    assert web._loopback_alias_origins(request_stub("https://sessions.example.test/")) == set()


def test_the_request_source_prefers_origin_unless_it_is_null_or_absent():
    assert (
        web._request_source(request_stub(headers={"Origin": "https://a", "Referer": "https://b"}))
        == "https://a"
    )
    assert (
        web._request_source(request_stub(headers={"Origin": "null", "Referer": "https://b"}))
        == "https://b"
    )
    assert web._request_source(request_stub(headers={"Referer": " https://b "})) == "https://b"
    assert web._request_source(request_stub()) == ""


def test_an_empty_or_unparseable_source_is_never_the_same_origin(app_config):
    assert web._same_origin("", app_config, request_stub()) is False
    assert web._same_origin("not a url", app_config, request_stub()) is False


@pytest.mark.parametrize(
    ("base", "index", "url"),
    [("https://t.example/", None, "https://t.example/term"), ("https://t.example", 0, "https://t.example/term?window=0"),
     ("https://t.example///", 3, "https://t.example/term?window=3")],
)  # fmt: skip
def test_terminal_urls(base, index, url):
    assert web._term_url(base, index) == url


@pytest.mark.parametrize(
    ("value", "expected_none"),
    [("", True), ("garbage", True), ("2026-04-01T00:00:00Z", False), ("2026-04-01T00:00:00", False), ("2026-04-01T00:00:00+02:00", False)],
)  # fmt: skip
def test_datetimes_parse_to_aware_values_or_none(value, expected_none):
    parsed = web._parse_datetime(value)
    assert (parsed is None) is expected_none
    if parsed:
        assert parsed.tzinfo is not None


def test_nested_values_and_token_totals_tolerate_odd_shapes():
    assert web._nested_value({"a": {"b": 1}}, "a", "b") == 1
    for odd in (None, "x", {"a": 5}, {"a": None}):
        assert web._nested_value(odd, "a", "b") is None
    assert web._nested_int({"a": {"b": "7"}}, "a", "b") == 7
    assert web._nested_int({"a": {"b": "x"}}, "a", "b") is None
    assert web._nested_int({}, "a", "b") is None
    assert web._token_total({"input_tokens": 3, "output_tokens": "4", "x": 9}) == 7
    assert web._token_total({"input_tokens": "bad"}) is None
    assert web._token_total({}) is None and web._token_total("x") is None


def test_the_filters_present_values_defensively(app_config):
    filters = create_app(app_config).jinja_env.filters
    assert filters["bytes"](0) == "0 B" and filters["bytes"](1536) == "1.5 KB"
    assert filters["bytes"](5 * 1024**4) == "5120.0 GB"
    assert filters["integer"](None) == "unknown" and filters["integer"](1234567) == "1,234,567"
    assert filters["percent"](None) == "unknown" and filters["percent"](42.4) == "42%"
    assert filters["datetime"]("") == "unknown" and filters["datetime"]("garbage") == "garbage"
    assert filters["age"]("garbage") == "unknown"
    now = datetime.now(timezone.utc)
    for delta, text in (
        (timedelta(days=800), "2y"), (timedelta(days=95), "3mo"), (timedelta(days=3), "3d"),
        (timedelta(hours=5), "5h"), (timedelta(minutes=5), "now"),
    ):  # fmt: skip
        assert filters["age"]((now - delta).isoformat()) == text
    assert filters["token_total"]("x") == ""
    assert filters["token_total"]({"input_tokens": 2, "output_tokens": 3}) == "5"
    assert filters["token_total"]({"input_tokens": "x"}) == ""
    assert filters["token_total"]({"input_tokens": 1500}) == "1,500"


def test_usage_summaries_pick_the_most_recent_session_and_ignore_odd_metadata(app_config):
    codex = SimpleNamespace(
        provider="codex", updated_at="2026-04-01T00:00:00Z",
        metadata={"token_usage": {"updated_at": "2026-04-02T00:00:00Z", "last": {"total_tokens": 5},
                                  "total": {"total_tokens": 9}, "model_context_window": 100},
                  "rate_limits": {"plan_type": "pro", "primary": {"used_percent": 10, "resets_at": "x"}}},
    )  # fmt: skip
    older = SimpleNamespace(
        provider="codex", updated_at="2026-03-01T00:00:00Z",
        metadata={"token_usage": {"last": {"total_tokens": 1}}},
    )  # fmt: skip
    broken = [
        SimpleNamespace(provider="codex", updated_at="", metadata={"token_usage": "x"}),
        SimpleNamespace(provider="codex", updated_at="", metadata={"token_usage": {}}),
        SimpleNamespace(provider="claude", updated_at="2026-01-01T00:00:00Z", metadata={}),
    ]
    summary = web._codex_usage_summary((older, codex, *broken))
    assert summary["session_total_tokens"] == 9 and summary["plan_type"] == "pro"
    assert summary["primary_used_percent"] == 10 and summary["secondary_used_percent"] is None
    assert web._codex_usage_summary(tuple(broken)) is None
    claude = SimpleNamespace(
        provider="claude", updated_at="2026-04-01T00:00:00Z",
        metadata={"model": "m", "token_usage": {"total": {"input_tokens": 3, "output_tokens": 4}}},
    )  # fmt: skip
    result = web._claude_usage_summary((claude, *broken))
    assert result["input_tokens"] == 3 and result["model"] == "m"
    assert "not recorded" in result["limits_note"]
    assert web._claude_usage_summary(()) is None


def test_session_filtering_by_origin_and_text():
    def item(**kw):
        base = dict(title="Fix bug", workspace="/w/app", preview="p", session_id="abc",
                    is_subagent=False, is_short_subagent=False)  # fmt: skip
        return SimpleNamespace(**{**base, **kw})

    sub, short, plain = (
        item(is_subagent=True),
        item(is_subagent=True, is_short_subagent=True),
        item(),
    )
    everyone = (sub, short, plain)
    assert web._filter_sessions(everyone, "", "subagent") == [sub, short]
    assert web._filter_sessions(everyone, "", "short_subagent") == [short]
    assert web._filter_sessions(everyone, "", "other") == [plain]
    assert web._filter_sessions(everyone, "", "unknown-origin") == list(everyone)
    for needle in ("FIX", "/W/APP", "ABC"):
        assert web._filter_sessions((plain,), needle) == [plain]
    assert web._filter_sessions((plain,), "zzz") == []


# --------------------------------------------------------------------------- monitor TLS wiring


def tls_config(app_config, **kwargs):
    return replace(app_config, **kwargs)


def test_no_monitor_tls_settings_means_no_context(app_config):
    assert web._webterm_monitor_ssl_context(app_config) is None


def test_a_client_cert_without_its_key_is_a_configuration_error(app_config, tmp_path):
    with pytest.raises(ValueError, match="together"):
        web._webterm_monitor_ssl_context(
            tls_config(app_config, monitor_webterm_client_cert=tmp_path / "c")
        )
    with pytest.raises(ValueError, match="together"):
        web._webterm_monitor_ssl_context(
            tls_config(app_config, monitor_webterm_client_key=tmp_path / "k")
        )


def test_monitor_tls_requires_an_https_webterm_url(app_config, tmp_path):
    for url in (None, "http://term.example.test"):
        config = tls_config(app_config, webterm_url=url, monitor_webterm_ca_cert=tmp_path / "ca")
        with pytest.raises(ValueError, match="HTTPS"):
            web._webterm_monitor_ssl_context(config)


def test_the_ca_and_client_identity_are_loaded_into_a_verifying_context(app_config, tmp_path):
    config = tls_config(
        app_config, webterm_url="https://term.example.test", monitor_webterm_ca_cert=tmp_path / "ca.pem",
        monitor_webterm_client_cert=tmp_path / "c.pem", monitor_webterm_client_key=tmp_path / "k.pem",
    )  # fmt: skip
    context = mock.MagicMock(spec=ssl.SSLContext)
    context.verify_flags = ssl.VERIFY_DEFAULT | getattr(ssl, "VERIFY_X509_STRICT", 0)
    with mock.patch.object(web.ssl, "create_default_context", return_value=context):
        result = web._webterm_monitor_ssl_context(config)
    assert result is context
    context.load_verify_locations.assert_called_once_with(cafile=str(tmp_path / "ca.pem"))
    context.load_cert_chain.assert_called_once_with(
        certfile=str(tmp_path / "c.pem"), keyfile=str(tmp_path / "k.pem")
    )
    assert not (context.verify_flags & getattr(ssl, "VERIFY_X509_STRICT", 0))


def test_the_monitor_and_poller_exist_only_when_configured(app_config):
    assert web._connection_monitor(app_config) is None
    assert web._claude_status_poller(app_config) is None
    configured = tls_config(
        app_config, monitor_targets=("a=10.0.0.1",), claude_status_poll_enabled=True
    )
    assert web._connection_monitor(configured) is not None
    assert (
        web._claude_status_poller(configured).interval_seconds
        == configured.claude_status_poll_interval_seconds
    )


def test_the_page_renders_usage_for_codex_and_claude_sessions(app_config):
    seed_codex(app_config.codex_root, token_count=True)
    seed_claude(app_config.claude_root)
    assert create_app(app_config).test_client().get("/").status_code == 200
