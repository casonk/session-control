"""Connection monitor: probe results, state transitions, target parsing.

The probes ping hosts, open sockets and fetch URLs, so every one is stubbed at its call
site: no test here touches the network, starts a process or sleeps. What is pinned is
what a probe result becomes: a failure of any kind is a "down" sample (never an
exception that stops the monitor), a transition emits exactly one event, and a redirect
cannot carry the client certificate to another origin.
"""

from __future__ import annotations

import urllib.error
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import pytest

from session_control import connection_monitor as cm
from session_control.connection_monitor import (
    ConnectionMonitor,
    ProbeResult,
    TargetConfig,
    TargetState,
    _fmt_duration,
    _probe,
    _url_origin,
    targets_from_config,
)

T0 = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def sample(ok=True, latency=10.0, n=0):
    return ProbeResult(ts=float(n), wall_ts=T0 + timedelta(seconds=n), ok=ok, latency_ms=latency)


def state(*results) -> TargetState:
    s = TargetState(TargetConfig("t", "tcp", "host", 22))
    for r in results:
        s.record(r)
    return s


# --------------------------------------------------------------------------- TargetState


def test_an_empty_state_is_unknown_with_no_latency_and_no_loss():
    s = state()
    assert s.current_ok is None and s.latency_ms is None and s.avg_latency_ms is None
    assert s.loss_pct == 0.0
    assert s.to_dict()["status"] == "unknown"


def test_latency_is_the_latest_successful_sample_not_a_failure():
    s = state(sample(True, 10, 0), sample(True, 30, 1), sample(False, None, 2))
    assert s.current_ok is False
    assert s.latency_ms == 30
    assert s.avg_latency_ms == 20


def test_a_success_without_a_latency_is_ignored_for_the_average():
    s = state(sample(True, None, 0), sample(True, 40, 1))
    assert s.avg_latency_ms == 40 and s.latency_ms == 40


def test_loss_is_the_share_of_failed_samples():
    s = state(
        sample(True, 5, 0), sample(False, None, 1), sample(False, None, 2), sample(True, 5, 3)
    )
    assert s.loss_pct == 50.0


def test_the_sparkline_marks_failures_unknown_latency_and_scales_to_a_ceiling():
    s = state(
        sample(False, None, 0),
        sample(True, None, 1),
        sample(True, 0.0, 2),
        sample(True, 200.0, 3),
        sample(True, 349.0, 4),
        sample(True, 5000.0, 5),
    )
    assert s.sparkline() == cm.SPARK_FAIL + "▁▁▅▇█"


def test_the_sparkline_shows_only_the_most_recent_samples():
    s = state(*(sample(True, 1.0, n) for n in range(50)))
    assert len(s.sparkline()) == 30
    assert len(s.sparkline(tail=5)) == 5


def test_history_is_a_bounded_ring():
    s = state(*(sample(True, 1.0, n) for n in range(cm.HISTORY_LEN + 25)))
    assert len(s.history) == cm.HISTORY_LEN


def test_the_first_sample_sets_a_timestamp_but_raises_no_event():
    up, down = state(), state()
    assert up.record(sample(True, 1, 0)) is None and up.last_up_at == T0 and up.last_down_at is None
    first_down = sample(False, None, 0)
    assert down.record(first_down) is None and down.last_down_at == T0 and down.last_up_at is None


def test_an_unchanged_status_raises_no_event_and_keeps_the_timestamps():
    s = state(sample(True, 1, 0))
    assert s.record(sample(True, 1, 5)) is None
    assert s.last_up_at == T0


def test_a_transition_raises_one_event_with_how_long_the_previous_state_lasted():
    with mock.patch.object(cm.time, "monotonic", side_effect=[100.0, 130.0, 190.0]):
        s = TargetState(TargetConfig("link", "icmp", "h"))
        s.record(sample(True, 1, 0))
        down = s.record(sample(False, None, 30))
        up = s.record(sample(True, 1, 90))
    assert (down.kind, down.target, down.prior_duration_seconds) == ("down", "link", 30.0)
    assert (up.kind, up.prior_duration_seconds) == ("up", 60.0)
    assert s.last_down_at == T0 + timedelta(seconds=30)
    assert s.last_up_at == T0 + timedelta(seconds=90)


def test_to_dict_reports_status_rounded_stats_and_history():
    s = state(sample(True, 10.04, 0), sample(False, None, 1))
    data = s.to_dict()
    assert data["status"] == "down" and data["name"] == "t" and data["kind"] == "tcp"
    assert data["loss_pct"] == 50.0 and data["avg_latency_ms"] == 10.0
    assert (
        data["last_up_at"] == T0.isoformat()
        and data["last_down_at"] == (T0 + timedelta(seconds=1)).isoformat()
    )
    assert data["history"][1] == {"ts": 1.0, "ok": False, "latency_ms": None}
    assert state(sample(True, 1, 0)).to_dict()["status"] == "up"


# --------------------------------------------------------------------------- monitor


def monitor_for(*targets, **kwargs) -> ConnectionMonitor:
    return ConnectionMonitor(list(targets), 5.0, **kwargs)


def test_events_are_reported_newest_first_and_bounded():
    runner = monitor_for(TargetConfig("a", "tcp", "h", 22))
    results = iter(
        [sample(True, 1, 0)] + [sample(bool(n % 2), 1 if n % 2 else None, n) for n in range(1, 260)]
    )
    with mock.patch.object(cm, "_probe", side_effect=lambda config, **kw: next(results)):
        for _ in range(260):
            runner._probe_all()
    events = runner.snapshot()["events"]
    assert len(events) == cm.MAX_EVENTS
    stamps = [e["ts"] for e in events]
    assert stamps == sorted(stamps, reverse=True)


def test_a_snapshot_carries_every_target_and_the_interval():
    runner = monitor_for(TargetConfig("a", "tcp", "h", 22), TargetConfig("b", "icmp", "h2"))
    snapshot = runner.snapshot()
    assert [t["name"] for t in snapshot["targets"]] == ["a", "b"]
    assert snapshot["interval_seconds"] == 5.0 and snapshot["events"] == []


def test_the_loop_probes_at_once_then_after_each_wait_until_stopped():
    runner = monitor_for()
    waits = iter([False, False, True])
    runner._stop = SimpleNamespace(
        wait=lambda interval: next(waits), clear=lambda: None, set=lambda: None
    )
    with mock.patch.object(runner, "_probe_all") as probe:
        runner._loop()
    assert probe.call_count == 3


def test_a_probe_that_raises_is_a_down_sample_so_the_monitor_never_dies():
    runner = monitor_for(TargetConfig("a", "tcp", "h", 22))
    with mock.patch.object(cm.socket, "create_connection", side_effect=OSError("refused")):
        runner._probe_all()
    assert runner.snapshot()["targets"][0]["status"] == "down"


def test_start_is_idempotent_and_stop_ends_the_loop():
    runner = monitor_for()
    release = []
    with mock.patch.object(runner, "_loop", side_effect=lambda: runner._stop.wait(5)):
        runner.start()
        thread = runner._thread
        assert thread.daemon and thread.name == "conn-monitor"
        runner.start()
        assert runner._thread is thread
        runner.stop()
        thread.join(5)
    assert not thread.is_alive() and not release


# --------------------------------------------------------------------------- probes


def test_probe_dispatches_by_kind_and_defaults_the_tcp_port():
    with (
        mock.patch.object(cm, "_ping_icmp", return_value=(True, 1.5)) as icmp,
        mock.patch.object(cm, "_probe_tcp", return_value=(True, 2.5)) as tcp,
        mock.patch.object(cm, "_probe_http", return_value=(True, 3.5)) as http,
    ):
        assert _probe(TargetConfig("a", "icmp", "h", timeout_seconds=4)).latency_ms == 1.5
        assert _probe(TargetConfig("b", "tcp", "h")).latency_ms == 2.5
        assert _probe(TargetConfig("c", "http", "https://x")).latency_ms == 3.5
    icmp.assert_called_once_with("h", 4)
    tcp.assert_called_once_with("h", 22, 2.0)
    assert http.call_args.kwargs == {"ssl_context": None}


def test_an_unknown_kind_or_any_exception_is_a_down_sample():
    assert _probe(TargetConfig("x", "carrier-pigeon", "h")).ok is False
    for error in (OSError("x"), RuntimeError("y"), TimeoutError()):
        with mock.patch.object(cm, "_probe_tcp", side_effect=error):
            result = _probe(TargetConfig("t", "tcp", "h", 80))
        assert (result.ok, result.latency_ms) == (False, None)


@pytest.mark.parametrize(("system", "flag"), [("Linux", "-W"), ("Darwin", "-t"), ("Windows", "-W")])
def test_ping_uses_the_platform_timeout_flag_and_a_whole_second_floor(system, flag):
    with (
        mock.patch.object(cm.platform, "system", return_value=system),
        mock.patch.object(
            cm.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="")
        ) as run,
    ):
        cm._ping_icmp("h", 0.4)
    args = run.call_args.args[0]
    assert args[:3] == ["ping", "-c", "1"] and args[3] == flag and args[4] == "1"
    assert args[-1] == "h"
    assert run.call_args.kwargs["timeout"] == pytest.approx(3.4)


@pytest.mark.parametrize(
    ("stdout", "latency"),
    [
        ("64 bytes from h: icmp_seq=1 ttl=64 time=12.3 ms", 12.3),
        ("reply from h: bytes=32 time<1 ms", 1.0),
        ("TIME=7 MS", 7.0),
        ("64 bytes from h", None),
    ],
)
def test_ping_reads_the_latency_when_the_output_has_it(stdout, latency):
    with mock.patch.object(
        cm.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=stdout)
    ):
        assert cm._ping_icmp("h", 2) == (True, latency)


def test_a_failed_ping_is_down():
    with mock.patch.object(
        cm.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="time=1 ms")
    ):
        assert cm._ping_icmp("h", 2) == (False, None)


def test_tcp_measures_the_connect_and_closes_the_socket():
    connection = mock.MagicMock()
    with (
        mock.patch.object(cm.socket, "create_connection", return_value=connection) as create,
        mock.patch.object(cm.time, "monotonic", side_effect=[10.0, 10.25]),
    ):
        ok, ms = cm._probe_tcp("h", 2222, 3.0)
    create.assert_called_once_with(("h", 2222), timeout=3.0)
    connection.__exit__.assert_called_once()
    assert ok is True and ms == pytest.approx(250.0)


def test_a_refused_tcp_connection_raises_for_the_caller_to_record_as_down():
    with (
        mock.patch.object(cm.socket, "create_connection", side_effect=ConnectionRefusedError()),
        pytest.raises(ConnectionRefusedError),
    ):
        cm._probe_tcp("h", 1, 1.0)


def http_response():
    response = mock.MagicMock()
    response.__enter__.return_value = response
    return response


def test_http_without_a_client_certificate_uses_the_plain_opener_and_reads_a_little():
    response = http_response()
    with mock.patch.object(cm.urllib.request, "urlopen", return_value=response) as urlopen:
        ok, ms = cm._probe_http("https://x.test", 2.0)
    urlopen.assert_called_once_with("https://x.test", timeout=2.0)
    response.read.assert_called_once_with(16)
    assert ok is True and ms >= 0


def test_an_http_error_status_still_means_the_host_is_reachable():
    error = urllib.error.HTTPError("https://x.test", 503, "unavailable", {}, None)
    with mock.patch.object(cm.urllib.request, "urlopen", side_effect=error):
        assert cm._probe_http("https://x.test", 2.0)[0] is True


def test_an_unreachable_http_host_raises_for_the_caller_to_record_as_down():
    with (
        mock.patch.object(
            cm.urllib.request, "urlopen", side_effect=urllib.error.URLError("no route")
        ),
        pytest.raises(urllib.error.URLError),
    ):
        cm._probe_http("https://x.test", 2.0)
    with mock.patch.object(
        cm.urllib.request, "urlopen", side_effect=urllib.error.URLError("no route")
    ):
        assert _probe(TargetConfig("w", "http", "https://x.test")).ok is False


def test_http_with_a_client_certificate_builds_a_same_origin_opener():
    response = http_response()
    context = object()
    opener = mock.Mock(open=mock.Mock(return_value=response))
    with (
        mock.patch.object(cm.urllib.request, "build_opener", return_value=opener) as build,
        mock.patch.object(cm.urllib.request, "HTTPSHandler") as https,
    ):
        assert cm._probe_http("https://x.test:8443/p", 2.0, ssl_context=context)[0] is True
    https.assert_called_once_with(context=context)
    redirect = build.call_args.args[0]
    assert isinstance(redirect, cm._SameOriginRedirectHandler)
    assert redirect._origin == ("https", "x.test", 8443)
    opener.open.assert_called_once_with("https://x.test:8443/p", timeout=2.0)


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("https://Host.Example/x", ("https", "host.example", 443)),
        ("http://host.example", ("http", "host.example", 80)),
        ("HTTPS://host.example:8443/", ("https", "host.example", 8443)),
        ("ftp://host.example", None),
        ("https:///nohost", None),
        ("not a url", None),
        ("https://host.example:99999", None),
        ("https://host.example:abc", None),
    ],
)
def test_url_origin(url, origin):
    assert _url_origin(url) == origin


def test_a_redirect_handler_with_no_known_origin_follows_nothing():
    handler = cm._SameOriginRedirectHandler(None)
    request = urllib.request.Request("https://x.test/")
    assert handler.redirect_request(request, None, 302, "Found", {}, "https://x.test/y") is None


import urllib.request  # noqa: E402  (used by the last test only)

# --------------------------------------------------------------------------- target parsing


def config(*targets, webterm=None):
    return SimpleNamespace(monitor_targets=targets, webterm_url=webterm)


def test_each_address_form_becomes_the_matching_probe_kind():
    parsed = targets_from_config(
        config(
            "desktop=192.168.10.5",
            "desktop-ssh=192.168.10.5:22",
            "web=https://webterm.example.local",
            "plain=http://svc.local:8080/x",
            "v6=[fe80::1]:2222",
        )
    )
    assert [(t.name, t.kind, t.host, t.port) for t in parsed] == [
        ("desktop", "icmp", "192.168.10.5", None),
        ("desktop-ssh", "tcp", "192.168.10.5", 22),
        ("web", "http", "https://webterm.example.local", None),
        ("plain", "http", "http://svc.local:8080/x", None),
        ("v6", "tcp", "fe80::1", 2222),
    ]


def test_a_port_that_is_not_a_number_falls_back_to_pinging_the_whole_address():
    parsed = targets_from_config(config("odd=host:abc"))
    assert [(t.kind, t.host) for t in parsed] == [("icmp", "host:abc")]


def test_malformed_entries_are_skipped_and_the_rest_kept():
    parsed = targets_from_config(
        config("no-equals", "=missing-name", "missing-addr=", "  ", "ok = host ")
    )
    assert [(t.name, t.host) for t in parsed] == [("ok", "host")]


def test_the_webterm_url_is_added_once_unless_already_a_target():
    url = "https://webterm.example.local"
    added = targets_from_config(config(webterm=url))
    assert [(t.name, t.kind, t.host, t.timeout_seconds) for t in added] == [
        ("webterm", "http", url, 3.0)
    ]
    duplicate = targets_from_config(config(f"term={url}", webterm=url))
    assert [t.name for t in duplicate] == ["term"]


def test_a_config_without_the_attributes_has_no_targets():
    assert targets_from_config(SimpleNamespace()) == []
    assert targets_from_config(config()) == []


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (None, ""),
        (0, "0s"),
        (59.9, "59s"),
        (60, "1m 0s"),
        (125, "2m 5s"),
        (3599, "59m 59s"),
        (3600, "1h 0m"),
        (7380, "2h 3m"),
    ],
)
def test_durations_read_naturally(seconds, text):
    assert _fmt_duration(seconds) == text
