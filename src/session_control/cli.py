"""Command line entry points for session-control."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path

from session_control.config import AppConfig
from session_control.scanner import PROVIDERS, SessionScanner
from session_control.web import create_app


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _is_loopback_host(host: str) -> bool:
    candidate = host.strip()
    if not candidate:
        return False
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    if candidate.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def _validate_bind(host: str) -> None:
    if _is_loopback_host(host):
        return
    if _truthy(os.environ.get("SESSION_CONTROL_ALLOW_REMOTE")):
        return
    raise SystemExit(
        "Refusing to bind session-control to a non-loopback host without "
        "SESSION_CONTROL_ALLOW_REMOTE=1."
    )


def _default_env_file() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "session-control.env.local"


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _load_default_env() -> None:
    env_path = Path(os.environ.get("SESSION_CONTROL_ENV_FILE") or _default_env_file()).expanduser()
    _load_env_file(env_path)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="session-control")
    subparsers = parser.add_subparsers(dest="command")

    scan = subparsers.add_parser("scan", help="scan local AI session stores")
    scan.add_argument("--json", action="store_true", help="print the scan report as JSON")
    scan.add_argument("--provider", choices=PROVIDERS, action="append", help="limit scan provider")

    web = subparsers.add_parser("web", help="run the private web UI")
    web.add_argument("--host", default=os.environ.get("SESSION_CONTROL_HOST", "127.0.0.1"))
    web.add_argument(
        "--port", type=int, default=int(os.environ.get("SESSION_CONTROL_PORT", "5420"))
    )
    web.add_argument("--debug", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    _load_default_env()
    parser = _build_parser()
    args = parser.parse_args(argv)
    config = AppConfig.from_env()

    if args.command in {None, "web"}:
        host = getattr(args, "host", os.environ.get("SESSION_CONTROL_HOST", "127.0.0.1"))
        port = getattr(args, "port", int(os.environ.get("SESSION_CONTROL_PORT", "5420")))
        _validate_bind(host)
        create_app(config).run(host=host, port=port, debug=getattr(args, "debug", False))
        return 0

    if args.command == "scan":
        providers = tuple(args.provider) if args.provider else None
        report = SessionScanner(config).scan(providers=providers)
        if args.json:
            print(json.dumps(report.to_dict(), indent=2))
        else:
            for session in report.sessions:
                print(f"{session.provider:8} {session.updated_at:20} {session.title}")
            for error in report.errors:
                print(f"WARN {error.provider}: {error.message}")
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2
