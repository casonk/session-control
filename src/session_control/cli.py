"""Command line entry points for session-control."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
from datetime import timedelta
from pathlib import Path

from session_control.actions import PruneResult, SessionActionService
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


def _parse_duration(value: str) -> timedelta:
    match = re.fullmatch(r"\s*(\d+)\s*([a-zA-Z]*)\s*", str(value))
    if not match:
        raise argparse.ArgumentTypeError("Use a duration like 180d, 26w, or 4320h.")
    amount = int(match.group(1))
    unit = (match.group(2) or "d").lower()
    if amount <= 0:
        raise argparse.ArgumentTypeError("Duration must be greater than zero.")
    if unit in {"d", "day", "days"}:
        return timedelta(days=amount)
    if unit in {"w", "week", "weeks"}:
        return timedelta(weeks=amount)
    if unit in {"h", "hour", "hours"}:
        return timedelta(hours=amount)
    raise argparse.ArgumentTypeError("Supported duration units are h, d, and w.")


def _duration_days(value: timedelta) -> str:
    seconds = int(value.total_seconds())
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{seconds}s"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="session-control")
    subparsers = parser.add_subparsers(dest="command")

    scan = subparsers.add_parser("scan", help="scan local AI session stores")
    scan.add_argument("--json", action="store_true", help="print the scan report as JSON")
    scan.add_argument("--provider", choices=PROVIDERS, action="append", help="limit scan provider")

    prune = subparsers.add_parser("prune", help="move old inactive sessions into trash")
    prune.add_argument(
        "--older-than",
        type=_parse_duration,
        default=_parse_duration(os.environ.get("SESSION_CONTROL_PRUNE_OLDER_THAN", "180d")),
        help="prune sessions whose last activity is older than this age (default: 180d)",
    )
    prune.add_argument("--provider", choices=PROVIDERS, action="append", help="limit provider")
    prune.add_argument("--dry-run", action="store_true", help="show what would be pruned")
    prune.add_argument("--json", action="store_true", help="print the prune report as JSON")

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

    if args.command == "prune":
        providers = tuple(args.provider) if args.provider else None
        result = SessionActionService(config).prune(
            args.older_than,
            providers=providers,
            dry_run=args.dry_run,
        )
        if args.json:
            print(json.dumps(_prune_result_to_dict(result), indent=2))
        else:
            _print_prune_result(result, args.older_than)
        return 1 if result.errors else 0

    parser.error(f"unknown command: {args.command}")
    return 2


def _print_prune_result(result: PruneResult, older_than: timedelta) -> None:
    action = "Would delete" if result.dry_run else "Deleted"
    affected_count = len(result.eligible) if result.dry_run else len(result.deleted)
    print(
        f"{action} {affected_count} session(s) older than {_duration_days(older_than)} "
        f"(cutoff {result.cutoff.isoformat().replace('+00:00', 'Z')})."
    )
    if result.skipped:
        print(f"Skipped {len(result.skipped)} old active session(s).")
    for deleted in result.deleted:
        print(
            f"DELETED {deleted.session.provider:8} {deleted.session.session_id} "
            f"-> {deleted.moved_to} ({deleted.moved_count} target(s))"
        )
    if result.dry_run:
        for session in result.eligible:
            print(f"DRYRUN  {session.provider:8} {session.updated_at:20} {session.title}")
    for skipped in result.skipped:
        print(
            f"SKIP    {skipped.session.provider:8} {skipped.session.session_id} ({skipped.reason})"
        )
    for error in result.errors:
        session_id = f" {error.session.session_id}" if error.session else ""
        print(f"ERROR   {error.provider:8}{session_id}: {error.message}")


def _prune_result_to_dict(result: PruneResult) -> dict:
    return {
        "cutoff": result.cutoff.isoformat().replace("+00:00", "Z"),
        "dry_run": result.dry_run,
        "scanned_count": result.scanned_count,
        "eligible_count": len(result.eligible),
        "deleted_count": len(result.deleted),
        "skipped_count": len(result.skipped),
        "error_count": len(result.errors),
        "eligible": [session.to_dict() for session in result.eligible],
        "deleted": [
            {
                "session": deleted.session.to_dict(),
                "moved_to": str(deleted.moved_to),
                "moved_count": deleted.moved_count,
            }
            for deleted in result.deleted
        ],
        "skipped": [
            {"session": skipped.session.to_dict(), "reason": skipped.reason}
            for skipped in result.skipped
        ],
        "errors": [
            {
                "provider": error.provider,
                "message": error.message,
                "session": error.session.to_dict() if error.session else None,
            }
            for error in result.errors
        ],
    }
