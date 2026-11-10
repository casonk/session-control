"""CLI entry point for grok-profile and `session-control grok-profile`."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

from session_control.grok_profiles import (
    GrokProfile,
    GrokRoute,
    execute_with_profile,
    get_profiles_config_path,
    get_usage_summary,
    harvest_usage,
    load_profiles,
    resolve_profile,
    save_profiles,
    validate_profile_name,
)

KNOWN_COMMANDS = {
    "list",
    "current",
    "switch",
    "add",
    "remove",
    "route",
    "usage",
    "harvest",
    "run",
    "help",
    "-h",
    "--help",
}


def _cmd_list(_args: argparse.Namespace) -> int:
    registry = load_profiles()
    with contextlib.suppress(Exception):
        harvest_usage(registry)
    usage = {item["profile"]: item for item in get_usage_summary().get("profiles", [])}
    print("Grok accounts:")
    print("-" * 78)
    print(f"{'Profile':<16} {'Active':<8} {'Email':<28} {'Sessions':<10} {'Tokens':<12}")
    print("-" * 78)
    for name, profile in registry.profiles.items():
        mark = "*" if name == registry.active_profile else ""
        totals = usage.get(name, {})
        email = profile.email or "(not set)"
        print(
            f"{name:<16} {mark:<8} {email:<28} "
            f"{int(totals.get('session_count') or 0):<10} "
            f"{int(totals.get('total_tokens') or 0):<12,}"
        )
    print("-" * 78)
    print(f"Config path: {get_profiles_config_path()}")
    return 0


def _cmd_current(_args: argparse.Namespace) -> int:
    registry = load_profiles()
    resolved = resolve_profile(registry, cwd=Path.cwd())
    print(f"Active default: {registry.active_profile}")
    print(f"Resolved for {Path.cwd()}: {resolved.name} ({resolved.email or 'no email'})")
    config = Path(registry.config_path) if registry.config_path else None
    print(f"GROK_HOME: {resolved.home(config)}")
    return 0


def _cmd_switch(args: argparse.Namespace) -> int:
    registry = load_profiles()
    if args.name not in registry.profiles:
        print(
            f"Error: Unknown profile '{args.name}'. Available: {', '.join(registry.profiles)}",
            file=sys.stderr,
        )
        return 1
    registry.active_profile = args.name
    save_profiles(registry)
    print(f"Switched active profile to '{args.name}'.")
    return 0


def _cmd_add(args: argparse.Namespace) -> int:
    name = args.name.strip()
    problem = validate_profile_name(name)
    if problem:
        print(f"Error: {problem}", file=sys.stderr)
        return 1
    registry = load_profiles()
    if name in registry.profiles and not args.force:
        print(f"Error: Profile '{name}' already exists. Use --force to overwrite.", file=sys.stderr)
        return 1
    entry = GrokProfile(
        name=name,
        email=args.email or "",
        grok_home=args.dir or "",
        description=args.desc or "",
        enabled=True,
    )
    registry.profiles[name] = entry
    save_profiles(registry)
    home = entry.home(Path(registry.config_path) if registry.config_path else None)
    home.mkdir(parents=True, exist_ok=True)
    print(f"Profile '{name}' added.")
    print(f"GROK_HOME: {home}")
    print(f"Sign in with: grok-profile run --profile {name} login")
    return 0


def _cmd_remove(args: argparse.Namespace) -> int:
    registry = load_profiles()
    name = args.name.strip()
    if name not in registry.profiles:
        print(f"Error: Profile '{name}' not found.", file=sys.stderr)
        return 1
    if name == "default":
        print("Error: Cannot remove the 'default' profile.", file=sys.stderr)
        return 1
    del registry.profiles[name]
    if registry.active_profile == name:
        registry.active_profile = "default"
    registry.routes = [route for route in registry.routes if route.profile != name]
    save_profiles(registry)
    print(f"Profile '{name}' removed from the registry. Its directory was left in place.")
    return 0


def _cmd_route(args: argparse.Namespace) -> int:
    registry = load_profiles()
    if args.route_action == "add":
        if args.profile not in registry.profiles:
            print(f"Error: Profile '{args.profile}' does not exist.", file=sys.stderr)
            return 1
        registry.routes.append(GrokRoute(pattern=args.pattern, profile=args.profile))
        save_profiles(registry)
        print(f"Route added: '{args.pattern}' -> {args.profile}")
        return 0
    if args.route_action == "remove":
        kept = [route for route in registry.routes if route.pattern != args.pattern]
        if len(kept) == len(registry.routes):
            print(f"No route found matching pattern '{args.pattern}'.", file=sys.stderr)
            return 1
        registry.routes = kept
        save_profiles(registry)
        print(f"Removed route pattern '{args.pattern}'.")
        return 0
    print("Directory routing rules:")
    print("-" * 60)
    print(f"{'Pattern':<40} {'Profile':<20}")
    print("-" * 60)
    for route in registry.routes:
        print(f"{route.pattern:<40} {route.profile:<20}")
    print("-" * 60)
    return 0


def _cmd_usage(args: argparse.Namespace) -> int:
    registry = load_profiles()
    with contextlib.suppress(Exception):
        harvest_usage(registry)
    summary = get_usage_summary(profile=args.profile)
    if args.json:
        print(json.dumps(summary, indent=2))
        return 0
    print("Grok token usage:")
    print("=" * 78)
    print(f"{'Profile':<16} {'Sessions':<10} {'Input':<14} {'Output':<12} {'Total':<14}")
    print("-" * 78)
    sessions = inputs = outputs = total = 0
    for item in summary.get("profiles", []):
        sessions += int(item.get("session_count") or 0)
        inputs += int(item.get("total_input") or 0)
        outputs += int(item.get("total_output") or 0)
        total += int(item.get("total_tokens") or 0)
        print(
            f"{item.get('profile', ''):<16} {int(item.get('session_count') or 0):<10} "
            f"{int(item.get('total_input') or 0):<14,} "
            f"{int(item.get('total_output') or 0):<12,} "
            f"{int(item.get('total_tokens') or 0):<14,}"
        )
    print("=" * 78)
    print(f"{'TOTAL':<16} {sessions:<10} {inputs:<14,} {outputs:<12,} {total:<14,}")
    return 0


def _cmd_harvest(_args: argparse.Namespace) -> int:
    result = harvest_usage(load_profiles())
    print(
        "Harvest complete: "
        f"{result.get('sessions_seen', 0)} sessions read, "
        f"{result.get('sessions_updated', 0)} updated."
    )
    return 0


def _cmd_run(args: argparse.Namespace, remaining: list[str]) -> int:
    return execute_with_profile(
        args=remaining,
        requested_profile=args.profile,
        cwd=Path.cwd(),
        auto_failover=not args.no_failover,
    )


def _run_parser(prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog)
    parser.add_argument("--profile", help="force a specific profile")
    parser.add_argument("--no-failover", action="store_true", help="do not try another account")
    return parser


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if not raw or (raw[0] not in KNOWN_COMMANDS and not raw[0].startswith("-h")):
        parsed, remaining = _run_parser("grok-profile").parse_known_args(raw)
        return _cmd_run(parsed, remaining)

    parser = argparse.ArgumentParser(
        prog="grok-profile",
        description="Run Grok under an isolated account, the way agy-profile does for AGY.",
    )
    sub = parser.add_subparsers(dest="profile_subcommand")
    sub.add_parser("list", help="list accounts and token totals")
    sub.add_parser("current", help="show the account selected for this directory")
    switch = sub.add_parser("switch", help="set the active account")
    switch.add_argument("name")
    add = sub.add_parser("add", help="register an account")
    add.add_argument("name")
    add.add_argument("--email", default="")
    add.add_argument("--dir", default="", help="GROK_HOME for this account")
    add.add_argument("--desc", default="")
    add.add_argument("--force", action="store_true")
    remove = sub.add_parser("remove", help="drop an account from the registry")
    remove.add_argument("name")
    route = sub.add_parser("route", help="send matching directories to an account")
    route_sub = route.add_subparsers(dest="route_action")
    route_sub.add_parser("list")
    route_add = route_sub.add_parser("add")
    route_add.add_argument("pattern")
    route_add.add_argument("profile")
    route_remove = route_sub.add_parser("remove")
    route_remove.add_argument("pattern")
    usage = sub.add_parser("usage", help="show harvested token totals")
    usage.add_argument("--profile", default=None)
    usage.add_argument("--json", action="store_true")
    sub.add_parser("harvest", help="read usage.json files into the local total")
    sub.add_parser("run", help="launch grok with the selected account")

    if raw[0] == "run":
        parsed, remaining = _run_parser("grok-profile run").parse_known_args(raw[1:])
        return _cmd_run(parsed, remaining)

    args = parser.parse_args(raw)
    command = args.profile_subcommand
    if command == "list":
        return _cmd_list(args)
    if command == "current":
        return _cmd_current(args)
    if command == "switch":
        return _cmd_switch(args)
    if command == "add":
        return _cmd_add(args)
    if command == "remove":
        return _cmd_remove(args)
    if command == "route":
        return _cmd_route(args)
    if command == "usage":
        return _cmd_usage(args)
    if command == "harvest":
        return _cmd_harvest(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
