"""Command line entry points for session-control."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import ssl
import stat
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from session_control.actions import (
    DeduplicateResult,
    PruneResult,
    RestoreCandidate,
    RestoreResult,
    SessionActionError,
    SessionActionService,
)
from session_control.config import AppConfig
from session_control.dedup import DEFAULT_SIMILARITY
from session_control.federation import build_inventory
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


def _relative_short_subagent_age(
    baseline: timedelta, *, percent: str | None = None, multiplier: str | None = None
) -> timedelta | None:
    if percent is None and multiplier is None:
        return None
    if percent is not None and multiplier is not None:
        raise argparse.ArgumentTypeError("Choose a percentage or a multiplier, not both.")
    raw = str(percent if percent is not None else multiplier).strip().lower()
    suffix = "%" if percent is not None else "x"
    if raw.endswith(suffix):
        raw = raw[:-1].strip()
    try:
        factor = Decimal(raw)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("Use a number such as 25% or 4x.") from exc
    if not factor.is_finite() or factor <= 0:
        raise argparse.ArgumentTypeError("Percentage or multiplier must be positive and finite.")
    if percent is not None and factor > 100:
        raise argparse.ArgumentTypeError("Percentage must be at most 100%.")
    if multiplier is not None and factor < 1:
        raise argparse.ArgumentTypeError("Multiplier must be at least 1x.")
    ratio = factor / 100 if percent is not None else 1 / factor
    age = timedelta(seconds=float(Decimal(str(baseline.total_seconds())) * ratio))
    if age <= timedelta(0):
        raise argparse.ArgumentTypeError("Calculated short subagent age is too small.")
    return age


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="session-control")
    subparsers = parser.add_subparsers(dest="command")

    scan = subparsers.add_parser("scan", help="scan local AI session stores")
    scan.add_argument("--json", action="store_true", help="print the scan report as JSON")
    scan.add_argument("--provider", choices=PROVIDERS, action="append", help="limit scan provider")

    inventory = subparsers.add_parser(
        "inventory", help="export a metadata-only local session inventory"
    )
    inventory.add_argument("--json", action="store_true", help="print inventory JSON")
    inventory.add_argument(
        "--output", type=Path, help="write inventory JSON to this owner-only file"
    )
    inventory.add_argument("--provider", choices=PROVIDERS, action="append", help="limit provider")

    publish = subparsers.add_parser(
        "publish-inventory", help="publish a metadata-only inventory to a trusted aggregator"
    )
    publish.add_argument("--url", required=True, help="HTTPS inventory receiver URL")
    publish.add_argument(
        "--token-file", type=Path, required=True, help="owner-only bearer token file"
    )
    publish.add_argument("--ca-file", type=Path, help="CA PEM for the receiver")
    publish.add_argument("--cert-file", type=Path, help="client certificate PEM for mTLS")
    publish.add_argument("--key-file", type=Path, help="client private-key PEM for mTLS")
    publish.add_argument("--provider", choices=PROVIDERS, action="append", help="limit provider")

    prune = subparsers.add_parser("prune", help="move old inactive sessions into trash")
    prune.add_argument(
        "--older-than",
        type=_parse_duration,
        default=_parse_duration(os.environ.get("SESSION_CONTROL_PRUNE_OLDER_THAN", "180d")),
        help="prune sessions whose last activity is older than this age (default: 180d)",
    )
    prune.add_argument("--provider", choices=PROVIDERS, action="append", help="limit provider")
    relative_age = prune.add_mutually_exclusive_group()
    relative_age.add_argument(
        "--short-subagent-percent",
        metavar="PERCENT",
        help="prune short subagents at this percentage of --older-than (e.g. 25%%)",
    )
    relative_age.add_argument(
        "--short-subagent-multiplier",
        metavar="MULTIPLIER",
        help="prune short subagents this many times sooner (e.g. 4x)",
    )
    prune.add_argument(
        "--short-subagents-only",
        action="store_true",
        help="limit cleanup to subagents with at most five minutes of recorded activity",
    )
    prune.add_argument("--dry-run", action="store_true", help="show what would be pruned")
    prune.add_argument("--json", action="store_true", help="print the prune report as JSON")

    dedup = subparsers.add_parser("deduplicate", help="merge duplicate sessions in an age window")
    dedup.add_argument(
        "--min-age",
        type=_parse_duration,
        default=_parse_duration(os.environ.get("SESSION_CONTROL_DEDUP_MIN_AGE", "50d")),
        help="lower bound of the age window (default: 50d)",
    )
    dedup.add_argument(
        "--max-age",
        type=_parse_duration,
        default=_parse_duration(os.environ.get("SESSION_CONTROL_DEDUP_MAX_AGE", "100d")),
        help="upper bound of the age window (default: 100d)",
    )
    dedup.add_argument(
        "--similarity",
        type=float,
        default=float(os.environ.get("SESSION_CONTROL_DEDUP_SIMILARITY", str(DEFAULT_SIMILARITY))),
        help=f"fuzzy-match threshold 0–1 (default: {DEFAULT_SIMILARITY})",
    )
    dedup.add_argument("--provider", choices=PROVIDERS, action="append", help="limit provider")
    dedup.add_argument("--dry-run", action="store_true", help="show what would be merged")
    dedup.add_argument("--json", action="store_true", help="print the report as JSON")

    restore = subparsers.add_parser("restore", help="restore a session from local trash")
    restore.add_argument(
        "trash_id",
        nargs="?",
        help="entry from `restore --list`, as timestamp/provider/session-id",
    )
    restore.add_argument("--list", action="store_true", help="list restorable trash entries")
    restore.add_argument("--json", action="store_true", help="print restore data as JSON")

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

    if args.command in {"inventory", "publish-inventory"}:
        providers = tuple(args.provider) if args.provider else None
        inventory = build_inventory(
            SessionScanner(config).scan(providers=providers),
            node_id=config.federation_node_id,
            node_label=config.federation_node_label,
        )
        if args.command == "inventory":
            payload = json.dumps(inventory, indent=2) + "\n"
            if args.output:
                _write_owner_only(args.output, payload)
            if args.json or not args.output:
                print(payload, end="")
            return 0
        try:
            _publish_inventory(inventory, args)
        except (OSError, ValueError, HTTPError, URLError) as exc:
            print(f"ERROR: Could not publish inventory: {exc}")
            return 1
        print(f"Published {len(inventory['sessions'])} session summary record(s).")
        return 0

    if args.command == "deduplicate":
        providers = tuple(args.provider) if args.provider else None
        result = SessionActionService(config).deduplicate(
            args.min_age,
            args.max_age,
            similarity=args.similarity,
            providers=providers,
            dry_run=args.dry_run,
        )
        if args.json:
            print(json.dumps(_dedup_result_to_dict(result), indent=2))
        else:
            _print_dedup_result(result)
        return 1 if result.errors else 0

    if args.command == "prune":
        providers = tuple(args.provider) if args.provider else None
        percent = args.short_subagent_percent
        multiplier = args.short_subagent_multiplier
        if percent is None and multiplier is None:
            percent = os.environ.get("SESSION_CONTROL_PRUNE_SHORT_SUBAGENT_PERCENT")
            multiplier = os.environ.get("SESSION_CONTROL_PRUNE_SHORT_SUBAGENT_MULTIPLIER")
        try:
            short_subagent_age = _relative_short_subagent_age(
                args.older_than, percent=percent, multiplier=multiplier
            )
        except argparse.ArgumentTypeError as exc:
            parser.error(str(exc))
        result = SessionActionService(config).prune(
            args.older_than,
            providers=providers,
            dry_run=args.dry_run,
            short_subagents_only=args.short_subagents_only,
            short_subagent_age=short_subagent_age,
        )
        if args.json:
            print(json.dumps(_prune_result_to_dict(result), indent=2))
        else:
            _print_prune_result(result, args.older_than)
        return 1 if result.errors else 0

    if args.command == "restore":
        service = SessionActionService(config)
        if args.list:
            candidates = service.list_trash()
            if args.json:
                print(
                    json.dumps([_restore_candidate_to_dict(item) for item in candidates], indent=2)
                )
            else:
                _print_restore_candidates(candidates)
            return 0
        if not args.trash_id:
            parser.error("restore requires a trash entry or --list")
        try:
            result = service.restore(args.trash_id)
        except SessionActionError as exc:
            print(f"ERROR: {exc}")
            return 1
        if args.json:
            print(json.dumps(_restore_result_to_dict(result), indent=2))
        else:
            print(
                f"RESTORED {result.candidate.provider:8} {result.candidate.session_id} "
                f"from {result.candidate.trash_id}"
            )
            for path in result.restored_to:
                print(f"  -> {path}")
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2


def _write_owner_only(path: Path, payload: str) -> None:
    path = path.expanduser()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload)
    os.chmod(path, 0o600)


def _publish_inventory(inventory: dict, args: argparse.Namespace) -> None:
    target = urlsplit(args.url)
    if target.scheme != "https" or not target.netloc or target.username or target.password:
        raise ValueError("receiver URL must be an HTTPS URL without embedded credentials")
    token = _read_owner_only_token(args.token_file)
    context = ssl.create_default_context(cafile=str(args.ca_file) if args.ca_file else None)
    if bool(args.cert_file) != bool(args.key_file):
        raise ValueError("--cert-file and --key-file must be supplied together")
    if args.cert_file and args.key_file:
        context.load_cert_chain(str(args.cert_file), str(args.key_file))
    request = Request(
        args.url,
        data=json.dumps(inventory, separators=(",", ":")).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(request, context=context, timeout=15) as response:
        if response.status not in {200, 201, 204}:
            raise ValueError(f"receiver returned HTTP {response.status}")


def _read_owner_only_token(path: Path) -> str:
    path = path.expanduser()
    details = path.stat()
    if not stat.S_ISREG(details.st_mode) or details.st_mode & 0o077:
        raise ValueError("token file must be a regular owner-only file")
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError("token file is empty")
    return token


def _print_dedup_result(result: DeduplicateResult) -> None:
    window = f"{_duration_days(result.min_age)}–{_duration_days(result.max_age)}"
    action = "Would merge" if result.dry_run else "Merged"
    group_count = len(result.groups) if result.dry_run else len(result.merged)
    print(f"{action} {group_count} duplicate group(s) in age window {window}.")
    if result.dry_run:
        for group in result.groups:
            print(f"  GROUP ({len(group)} sessions, provider={group[0].provider}):")
            for s in sorted(group, key=lambda x: x.updated_at or x.created_at or ""):
                print(f"    {s.updated_at[:10]}  {s.title[:60]}")
    else:
        for merge in result.merged:
            absorbed_ids = ", ".join(s.session_id[:8] for s in merge.absorbed)
            print(
                f"  MERGED  kept={merge.kept.session_id[:8]}  "
                f"absorbed=[{absorbed_ids}]  -> {merge.moved_to}"
            )
    for error in result.errors:
        print(f"  ERROR   {error}")


def _dedup_result_to_dict(result: DeduplicateResult) -> dict:
    return {
        "min_age": _duration_days(result.min_age),
        "max_age": _duration_days(result.max_age),
        "dry_run": result.dry_run,
        "scanned_count": result.scanned_count,
        "group_count": len(result.groups),
        "merged_count": len(result.merged),
        "error_count": len(result.errors),
        "groups": [[s.to_dict() for s in group] for group in result.groups],
        "merged": [
            {
                "kept": m.kept.to_dict(),
                "absorbed": [s.to_dict() for s in m.absorbed],
                "moved_to": str(m.moved_to),
            }
            for m in result.merged
        ],
        "errors": list(result.errors),
    }


def _print_prune_result(result: PruneResult, older_than: timedelta) -> None:
    action = "Would delete" if result.dry_run else "Deleted"
    affected_count = len(result.eligible) if result.dry_run else len(result.deleted)
    standard_cutoff = result.cutoff.isoformat().replace("+00:00", "Z")
    if result.short_subagent_cutoff:
        short_age = result.short_subagent_cutoff - result.cutoff
        short_cutoff = result.short_subagent_cutoff.isoformat().replace("+00:00", "Z")
        print(f"{action} {affected_count} session(s).")
        print(f"Standard retention: {_duration_days(older_than)} (cutoff {standard_cutoff}).")
        print(f"Short subagents: {_duration_days(older_than - short_age)} (cutoff {short_cutoff}).")
    else:
        print(
            f"{action} {affected_count} session(s) older than {_duration_days(older_than)} "
            f"(cutoff {standard_cutoff})."
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
        "short_subagent_cutoff": (
            result.short_subagent_cutoff.isoformat().replace("+00:00", "Z")
            if result.short_subagent_cutoff
            else None
        ),
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


def _print_restore_candidates(candidates: tuple[RestoreCandidate, ...]) -> None:
    if not candidates:
        print("No restorable sessions in local trash.")
        return
    for item in candidates:
        print(
            f"{item.trash_id}  {item.provider:8} {item.session_id} ({item.moved_count} target(s))"
        )


def _restore_candidate_to_dict(candidate: RestoreCandidate) -> dict:
    return {
        "trash_id": candidate.trash_id,
        "provider": candidate.provider,
        "session_id": candidate.session_id,
        "moved_at": candidate.moved_at,
        "moved_count": candidate.moved_count,
    }


def _restore_result_to_dict(result: RestoreResult) -> dict:
    return {
        "candidate": _restore_candidate_to_dict(result.candidate),
        "restored_to": [str(path) for path in result.restored_to],
    }
