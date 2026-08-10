"""Background connection prober for session-control."""

from __future__ import annotations

import platform
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

HISTORY_LEN = 60  # ring buffer depth (~5 min at 5s interval)
MAX_EVENTS = 100
SPARK_CHARS = "▁▂▃▄▅▆▇█"
SPARK_FAIL = "✕"
MAX_SPARK_MS = 400.0  # latency ceiling mapped to top spark bar

_PING_TIME_RE = re.compile(r"time[=<]\s*([0-9.]+)\s*ms", re.IGNORECASE)


@dataclass(frozen=True)
class TargetConfig:
    name: str
    kind: str  # "icmp" | "tcp" | "http"
    host: str
    port: int | None = None
    timeout_seconds: float = 2.0


@dataclass
class ProbeResult:
    ts: float  # time.monotonic() at probe start
    wall_ts: datetime
    ok: bool
    latency_ms: float | None


@dataclass
class ConnEvent:
    ts: datetime
    target: str
    kind: str  # "up" | "down"
    prior_duration_seconds: float | None = None


@dataclass
class TargetState:
    config: TargetConfig
    history: deque = field(default_factory=lambda: deque(maxlen=HISTORY_LEN))
    last_down_at: datetime | None = None
    last_up_at: datetime | None = None

    def __post_init__(self) -> None:
        self._transition_ts: float = time.monotonic()

    @property
    def current_ok(self) -> bool | None:
        return self.history[-1].ok if self.history else None

    @property
    def latency_ms(self) -> float | None:
        for r in reversed(self.history):
            if r.ok and r.latency_ms is not None:
                return r.latency_ms
        return None

    @property
    def avg_latency_ms(self) -> float | None:
        samples = [r.latency_ms for r in self.history if r.ok and r.latency_ms is not None]
        return sum(samples) / len(samples) if samples else None

    @property
    def loss_pct(self) -> float:
        if not self.history:
            return 0.0
        return 100.0 * sum(1 for r in self.history if not r.ok) / len(self.history)

    def sparkline(self, tail: int = 30) -> str:
        window = list(self.history)[-tail:]
        chars = []
        for r in window:
            if not r.ok:
                chars.append(SPARK_FAIL)
            elif r.latency_ms is None:
                chars.append(SPARK_CHARS[0])
            else:
                idx = min(7, int(r.latency_ms / MAX_SPARK_MS * 8))
                chars.append(SPARK_CHARS[idx])
        return "".join(chars)

    def record(self, result: ProbeResult) -> ConnEvent | None:
        prev_ok = self.history[-1].ok if self.history else None
        self.history.append(result)

        if prev_ok is None:
            # First probe — set initial timestamps but don't emit an event.
            if result.ok:
                self.last_up_at = result.wall_ts
            else:
                self.last_down_at = result.wall_ts
            return None

        if result.ok == prev_ok:
            return None

        now_ts = time.monotonic()
        prior = now_ts - self._transition_ts
        self._transition_ts = now_ts

        if result.ok:
            self.last_up_at = result.wall_ts
            return ConnEvent(ts=result.wall_ts, target=self.config.name, kind="up",
                             prior_duration_seconds=prior)
        else:
            self.last_down_at = result.wall_ts
            return ConnEvent(ts=result.wall_ts, target=self.config.name, kind="down",
                             prior_duration_seconds=prior)

    def to_dict(self) -> dict:
        return {
            "name": self.config.name,
            "kind": self.config.kind,
            "host": self.config.host,
            "status": ("up" if self.current_ok else "down")
                      if self.current_ok is not None else "unknown",
            "latency_ms": self.latency_ms,
            "avg_latency_ms": round(self.avg_latency_ms, 1) if self.avg_latency_ms else None,
            "loss_pct": round(self.loss_pct, 1),
            "last_down_at": self.last_down_at.isoformat() if self.last_down_at else None,
            "last_up_at": self.last_up_at.isoformat() if self.last_up_at else None,
            "sparkline": self.sparkline(),
            "history": [
                {"ts": r.ts, "ok": r.ok, "latency_ms": r.latency_ms}
                for r in self.history
            ],
        }


class ConnectionMonitor:
    def __init__(self, targets: list[TargetConfig], interval_seconds: float = 5.0):
        self._states: dict[str, TargetState] = {t.name: TargetState(t) for t in targets}
        self._interval = interval_seconds
        self._lock = threading.Lock()
        self._events: deque[ConnEvent] = deque(maxlen=MAX_EVENTS)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="conn-monitor")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "targets": [s.to_dict() for s in self._states.values()],
                "events": [
                    {
                        "ts": e.ts.isoformat(),
                        "target": e.target,
                        "kind": e.kind,
                        "prior_duration_seconds": e.prior_duration_seconds,
                    }
                    for e in reversed(self._events)
                ],
                "interval_seconds": self._interval,
            }

    def _loop(self) -> None:
        self._probe_all()
        while not self._stop.wait(self._interval):
            self._probe_all()

    def _probe_all(self) -> None:
        for state in self._states.values():
            result = _probe(state.config)
            with self._lock:
                event = state.record(result)
                if event:
                    self._events.append(event)


# ── probe implementations ─────────────────────────────────────────────────────

def _probe(config: TargetConfig) -> ProbeResult:
    wall_ts = datetime.now(timezone.utc)
    t0 = time.monotonic()
    try:
        if config.kind == "icmp":
            ok, ms = _ping_icmp(config.host, config.timeout_seconds)
        elif config.kind == "tcp":
            ok, ms = _probe_tcp(config.host, config.port or 22, config.timeout_seconds)
        elif config.kind == "http":
            ok, ms = _probe_http(config.host, config.timeout_seconds)
        else:
            ok, ms = False, None
    except Exception:
        ok, ms = False, None
    return ProbeResult(ts=t0, wall_ts=wall_ts, ok=ok, latency_ms=ms)


def _ping_icmp(host: str, timeout: float) -> tuple[bool, float | None]:
    timeout_int = max(1, int(timeout))
    if platform.system() == "Darwin":
        args = ["ping", "-c", "1", "-t", str(timeout_int), host]
    else:
        args = ["ping", "-c", "1", "-W", str(timeout_int), host]
    result = subprocess.run(
        args, capture_output=True, text=True, timeout=timeout + 3
    )
    if result.returncode != 0:
        return False, None
    m = _PING_TIME_RE.search(result.stdout)
    return True, float(m.group(1)) if m else None


def _probe_tcp(host: str, port: int, timeout: float) -> tuple[bool, float | None]:
    t0 = time.monotonic()
    with socket.create_connection((host, port), timeout=timeout):
        pass
    return True, (time.monotonic() - t0) * 1000.0


def _probe_http(url: str, timeout: float) -> tuple[bool, float | None]:
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            resp.read(16)
    except urllib.error.HTTPError:
        # Server responded with an error code — it's reachable.
        pass
    return True, (time.monotonic() - t0) * 1000.0


# ── config helpers ────────────────────────────────────────────────────────────

def targets_from_config(config: object) -> list[TargetConfig]:
    """
    Build a target list from AppConfig.monitor_targets plus the webterm URL.

    Each monitor_targets entry has the form  name=address  where address is:
      - an ICMP host:   desktop=192.168.10.5
      - a TCP endpoint: desktop-ssh=192.168.10.5:22
      - an HTTP URL:    webterm=https://webterm.example.local
    """
    targets: list[TargetConfig] = []
    seen: set[str] = set()

    for spec in getattr(config, "monitor_targets", ()):
        spec = spec.strip()
        if not spec or "=" not in spec:
            continue
        name, addr = spec.split("=", 1)
        name, addr = name.strip(), addr.strip()
        if not name or not addr:
            continue
        if addr.startswith("http://") or addr.startswith("https://"):
            targets.append(TargetConfig(name=name, kind="http", host=addr))
        elif ":" in addr.lstrip("["):
            host, _, port_str = addr.rpartition(":")
            host = host.strip("[]") if host else addr
            try:
                port = int(port_str)
                targets.append(TargetConfig(name=name, kind="tcp", host=host, port=port))
            except ValueError:
                targets.append(TargetConfig(name=name, kind="icmp", host=addr))
        else:
            targets.append(TargetConfig(name=name, kind="icmp", host=addr))
        seen.add(addr)

    webterm_url = getattr(config, "webterm_url", None)
    if webterm_url and webterm_url not in seen:
        targets.append(
            TargetConfig(name="webterm", kind="http", host=webterm_url, timeout_seconds=3.0)
        )

    return targets


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return ""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    return f"{s // 3600}h {(s % 3600) // 60}m"
