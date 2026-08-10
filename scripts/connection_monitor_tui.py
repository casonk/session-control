#!/usr/bin/env python3
"""
Terminal UI for session-control connection monitor.

Usage:
    python3 scripts/connection_monitor_tui.py

Requires: rich  (pip install rich)

Reads SESSION_CONTROL_* env vars (or config/session-control.env.local) to
discover targets from SESSION_CONTROL_MONITOR_TARGETS and probe interval from
SESSION_CONTROL_MONITOR_INTERVAL.  Falls back to 8.8.8.8 + 1.1.1.1 if no
targets are configured.
"""

from __future__ import annotations

import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Allow running from the repo root without installing the package.
_repo_src = Path(__file__).resolve().parent.parent / "src"
if _repo_src.is_dir():
    sys.path.insert(0, str(_repo_src))

try:
    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
except ImportError:
    print("rich is required: pip install rich", file=sys.stderr)
    sys.exit(1)

# These must follow the sys.path bootstrap above, so E402 does not apply.
from session_control.cli import _load_default_env  # noqa: E402
from session_control.config import AppConfig  # noqa: E402
from session_control.connection_monitor import (  # noqa: E402
    ConnectionMonitor,
    TargetConfig,
    TargetState,
    _fmt_duration,
    targets_from_config,
)

REFRESH_HZ = 4  # screen redraws per second
MAX_EVENT_ROWS = 12
SPARK_TAIL = 30  # sparkline width in TUI


def _status_text(state: TargetState) -> Text:
    ok = state.current_ok
    if ok is True:
        return Text("● UP", style="bold green")
    if ok is False:
        return Text("✕ DOWN", style="bold red")
    return Text("? …", style="bold yellow")


def _latency_text(ms: float | None) -> Text:
    if ms is None:
        return Text("—", style="dim")
    if ms < 20:
        style = "green"
    elif ms < 100:
        style = "yellow"
    else:
        style = "red"
    return Text(f"{ms:.1f}", style=style)


def _loss_text(pct: float) -> Text:
    if pct == 0:
        return Text("0.0%", style="dim")
    if pct < 10:
        return Text(f"{pct:.1f}%", style="yellow")
    return Text(f"{pct:.1f}%", style="bold red")


def _spark_text(state: TargetState) -> Text:
    raw = state.sparkline(tail=SPARK_TAIL)
    t = Text()
    for ch in raw:
        if ch == "✕":
            t.append(ch, style="red")
        elif ch in "▅▆▇█":
            t.append(ch, style="yellow")
        else:
            t.append(ch, style="green")
    return t


def _ts_short(dt: datetime | None) -> str:
    if dt is None:
        return "never"
    local = dt.astimezone()
    delta = datetime.now(timezone.utc) - dt
    if delta.total_seconds() < 86400:
        return local.strftime("%H:%M:%S")
    return local.strftime("%m-%d %H:%M")


def _build_table(monitor: ConnectionMonitor, now_str: str) -> Table:
    snap = monitor.snapshot()
    table = Table(
        title=f"[bold]Connection Monitor[/bold]  [dim]{now_str}[/dim]",
        title_justify="left",
        box=None,
        show_header=True,
        header_style="bold dim",
        expand=True,
        padding=(0, 1),
    )
    table.add_column("Target", min_width=14)
    table.add_column("Host", style="dim", min_width=14)
    table.add_column("Status", min_width=8)
    table.add_column("ms", justify="right", min_width=6)
    table.add_column("avg ms", justify="right", min_width=6)
    table.add_column("Loss", justify="right", min_width=6)
    table.add_column("Last drop", min_width=10)
    table.add_column(f"History (last {SPARK_TAIL})", min_width=SPARK_TAIL)

    states = monitor._states  # noqa: SLF001 — TUI has intentional internal access
    for t in snap["targets"]:
        state = states.get(t["name"])
        if state is None:
            continue
        last_down_dt = state.last_down_at
        table.add_row(
            Text(t["name"], style="bold"),
            t["host"],
            _status_text(state),
            _latency_text(t["latency_ms"]),
            _latency_text(t["avg_latency_ms"]),
            _loss_text(t["loss_pct"]),
            _ts_short(last_down_dt),
            _spark_text(state),
        )

    return table


def _build_events_panel(monitor: ConnectionMonitor) -> Panel:
    snap = monitor.snapshot()
    events = snap["events"][:MAX_EVENT_ROWS]
    lines = Text()
    if not events:
        lines.append("No events yet.", style="dim")
    else:
        for e in events:
            ts_str = e["ts"][:19].replace("T", " ")
            kind = e["kind"]
            color = "green" if kind == "up" else "red"
            dur = _fmt_duration(e.get("prior_duration_seconds"))
            dur_str = f"  [dim]after {dur}[/dim]" if dur else ""
            lines.append(f"{ts_str}  ", style="dim")
            lines.append(f"{e['target']:<16}", style="bold")
            lines.append(f"{kind.upper():<6}", style=f"bold {color}")
            lines.append(dur_str)
            lines.append("\n")
    return Panel(
        lines, title="[bold dim]Recent Events[/bold dim]", border_style="dim", padding=(0, 1)
    )


def _default_targets() -> list[TargetConfig]:
    return [
        TargetConfig(name="dns-google", kind="icmp", host="8.8.8.8"),
        TargetConfig(name="dns-cf", kind="icmp", host="1.1.1.1"),
    ]


def main() -> None:
    _load_default_env()
    config = AppConfig.from_env()
    targets = targets_from_config(config)
    if not targets:
        print("[connection-monitor] No SESSION_CONTROL_MONITOR_TARGETS configured.")
        print("  Falling back to 8.8.8.8 + 1.1.1.1")
        targets = _default_targets()

    interval = config.monitor_interval_seconds
    monitor = ConnectionMonitor(targets, interval_seconds=interval)
    monitor.start()

    console = Console()
    stop = False

    def _on_signal(*_: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    with Live(console=console, refresh_per_second=REFRESH_HZ, screen=True) as live:
        while not stop:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            layout = Layout()
            layout.split_column(
                Layout(_build_table(monitor, now_str), name="table", ratio=3),
                Layout(_build_events_panel(monitor), name="events", ratio=2),
            )
            live.update(layout)
            time.sleep(1.0 / REFRESH_HZ)

    monitor.stop()
    console.print("\n[dim]Connection monitor stopped.[/dim]")


if __name__ == "__main__":
    main()
