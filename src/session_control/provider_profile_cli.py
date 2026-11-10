"""CLI for codex-profile, claude-profile, continue-profile, and copilot-profile."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

from session_control.home_profiles import (
    SPECS,
    HomeProfile,
    HomeProfileSpec,
    HomeRoute,
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


def _cmd_list(spec: HomeProfileSpec, _args: argparse.Namespace) -> int:
    registry = load_profiles(spec)
    with contextlib.suppress(Exception):
        harvest_usage(registry)
    usage = {item["profile"]: item for item in get_usage_summary(spec).get("profiles", [])}
    print(f"{spec.title}:")
    print("-" * 78)
    print(f"{'Profile':<16} {'Active':<8} {'Account':<28} {'Sessions':<10} {'Tokens':<12}")
    print("-" * 78)
    for name, profile in registry.profiles.items():
        mark = "*" if name == registry.active_profile else ""
        totals = usage.get(name, {})
        label = profile.email or "(not set)"
        print(
            f"{name:<16} {mark:<8} {label:<28} "
            f"{int(totals.get('session_count') or 0):<10} "
            f"{int(totals.get('total_tokens') or 0):<12,}"
        )
    print("-" * 78)
    print(f"Config path: {get_profiles_config_path(spec)}")
    return 0


def _cmd_current(spec: HomeProfileSpec, _args: argparse.Namespace) -> int:
    registry = load_profiles(spec)
    resolved = resolve_profile(registry, cwd=Path.cwd())
    config = Path(registry.config_path) if registry.config_path else get_profiles_config_path(spec)
    print(f"Active default: {registry.active_profile}")
    print(f"Resolved for {Path.cwd()}: {resolved.name} ({resolved.email or 'no account label'})")
    print(f"{spec.env_var}: {resolved.home(config)}")
    return 0


def _cmd_switch(spec: HomeProfileSpec, args: argparse.Namespace) -> int:
    registry = load_profiles(spec)
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


def _cmd_add(spec: HomeProfileSpec, args: argparse.Namespace) -> int:
    name = args.name.strip()
    problem = validate_profile_name(name)
    if problem:
        print(f"Error: {problem}", file=sys.stderr)
        return 1
    registry = load_profiles(spec)
    if name in registry.profiles and not args.force:
        print(f"Error: Profile '{name}' already exists. Use --force to overwrite.", file=sys.stderr)
        return 1
    entry = HomeProfile(
        name=name,
        email=args.email or "",
        home_dir=args.dir or "",
        description=args.desc or "",
        enabled=True,
    )
    registry.profiles[name] = entry
    save_profiles(registry)
    config = Path(registry.config_path) if registry.config_path else get_profiles_config_path(spec)
    home = entry.home(config)
    home.mkdir(parents=True, exist_ok=True)
    print(f"Profile '{name}' added.")
    print(f"{spec.env_var}: {home}")
    hint = " ".join(spec.sign_in_args)
    print(f"Sign in with: {spec.launcher} run --profile {name} {hint}")
    return 0


def _cmd_remove(spec: HomeProfileSpec, args: argparse.Namespace) -> int:
    registry = load_profiles(spec)
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


def _cmd_route(spec: HomeProfileSpec, args: argparse.Namespace) -> int:
    registry = load_profiles(spec)
    if args.route_action == "add":
        if args.profile not in registry.profiles:
            print(f"Error: Profile '{args.profile}' does not exist.", file=sys.stderr)
            return 1
        registry.routes.append(HomeRoute(pattern=args.pattern, profile=args.profile))
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


def _cmd_usage(spec: HomeProfileSpec, args: argparse.Namespace) -> int:
    registry = load_profiles(spec)
    with contextlib.suppress(Exception):
        harvest_usage(registry)
    summary = get_usage_summary(spec, profile=args.profile)
    if args.json:
        print(json.dumps(summary, indent=2))
        return 0
    print(f"{spec.title} token usage:")
    print("=" * 88)
    print(
        f"{'Profile':<16} {'Sessions':<10} {'Input':<12} {'Output':<12} {'Cache':<12} {'Total':<12}"
    )
    print("-" * 88)
    sessions = inputs = outputs = cache = total = 0
    for item in summary.get("profiles", []):
        sessions += int(item.get("session_count") or 0)
        inputs += int(item.get("total_input") or 0)
        outputs += int(item.get("total_output") or 0)
        cache += int(item.get("total_cache") or 0)
        total += int(item.get("total_tokens") or 0)
        print(
            f"{item.get('profile', ''):<16} {int(item.get('session_count') or 0):<10} "
            f"{int(item.get('total_input') or 0):<12,} "
            f"{int(item.get('total_output') or 0):<12,} "
            f"{int(item.get('total_cache') or 0):<12,} "
            f"{int(item.get('total_tokens') or 0):<12,}"
        )
    print("=" * 88)
    print(f"{'TOTAL':<16} {sessions:<10} {inputs:<12,} {outputs:<12,} {cache:<12,} {total:<12,}")
    return 0


def _cmd_harvest(spec: HomeProfileSpec, _args: argparse.Namespace) -> int:
    result = harvest_usage(load_profiles(spec))
    print(
        "Harvest complete: "
        f"{result.get('sessions_seen', 0)} sessions read, "
        f"{result.get('sessions_updated', 0)} updated."
    )
    return 0


def _cmd_run(spec: HomeProfileSpec, args: argparse.Namespace, remaining: list[str]) -> int:
    return execute_with_profile(
        spec,
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


def _build_parser(spec: HomeProfileSpec) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=spec.launcher,
        description=(
            f"Run {spec.executable} under an isolated account. "
            f"Each account is its own {spec.env_var}."
        ),
    )
    sub = parser.add_subparsers(dest="profile_subcommand")
    sub.add_parser("list", help="list accounts and token totals")
    sub.add_parser("current", help="show the account selected for this directory")
    switch = sub.add_parser("switch", help="set the active account")
    switch.add_argument("name")
    add = sub.add_parser("add", help="register an account")
    add.add_argument("name")
    add.add_argument("--email", default="", help="label stored in the registry")
    add.add_argument("--dir", default="", help=f"{spec.env_var} for this account")
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
    sub.add_parser("harvest", help="read local session files into the token total")
    sub.add_parser("run", help=f"launch {spec.executable} with the selected account")
    return parser


def main_for(provider: str, argv: list[str] | None = None) -> int:
    spec = SPECS[provider]
    raw = list(sys.argv[1:] if argv is None else argv)
    if not raw or (raw[0] not in KNOWN_COMMANDS and not raw[0].startswith("-h")):
        parsed, remaining = _run_parser(spec.launcher).parse_known_args(raw)
        return _cmd_run(spec, parsed, remaining)

    parser = _build_parser(spec)
    if raw[0] == "run":
        parsed, remaining = _run_parser(f"{spec.launcher} run").parse_known_args(raw[1:])
        return _cmd_run(spec, parsed, remaining)

    args = parser.parse_args(raw)
    command = args.profile_subcommand
    handlers = {
        "list": _cmd_list,
        "current": _cmd_current,
        "switch": _cmd_switch,
        "add": _cmd_add,
        "remove": _cmd_remove,
        "route": _cmd_route,
        "usage": _cmd_usage,
        "harvest": _cmd_harvest,
    }
    handler = handlers.get(command or "")
    if handler is None:
        parser.print_help()
        return 0
    return handler(spec, args)


def codex_main(argv: list[str] | None = None) -> int:
    return main_for("codex", argv)


def claude_main(argv: list[str] | None = None) -> int:
    return main_for("claude", argv)


def continue_main(argv: list[str] | None = None) -> int:
    return main_for("continue", argv)


def copilot_main(argv: list[str] | None = None) -> int:
    return main_for("copilot", argv)
