"""CLI entry points for agy-profile and session-control profile."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

from session_control.profiles import (
    ProfileEntry,
    ProfileRouteRule,
    execute_with_profile,
    get_profiles_config_path,
    get_usage_summary,
    harvest_usage,
    load_profiles,
    resolve_profile_for_context,
    save_profiles,
)


def _cmd_list(args: argparse.Namespace) -> int:
    registry = load_profiles()
    with contextlib.suppress(Exception):
        harvest_usage(registry)
    usage = get_usage_summary()
    usage_by_prof = {p["profile"]: p for p in usage.get("profiles", [])}

    print("Google AGY / Gemini Profiles:")
    print("-" * 75)
    print(f"{'Profile':<15} {'Active':<8} {'Email':<25} {'Sessions':<10} {'Total Tokens':<12}")
    print("-" * 75)

    for name, prof in registry.profiles.items():
        is_active = "*" if name == registry.active_profile else ""
        prof_usage = usage_by_prof.get(name, {})
        sess_cnt = prof_usage.get("session_count", 0)
        tokens = prof_usage.get("total_tokens", 0)
        email = prof.email or "(not set)"
        print(f"{name:<15} {is_active:<8} {email:<25} {sess_cnt:<10} {tokens:<12,}")

    print("-" * 75)
    print(f"Config path: {get_profiles_config_path()}")
    return 0


def _cmd_current(args: argparse.Namespace) -> int:
    registry = load_profiles()
    resolved = resolve_profile_for_context(registry, cwd=Path.cwd())
    print(f"Active default: {registry.active_profile}")
    print(
        f"Resolved for current directory ({Path.cwd()}): {resolved.name} ({resolved.email or 'no email'})"
    )
    print(f"Gemini dir: {resolved.get_gemini_path()}")
    return 0


def _cmd_switch(args: argparse.Namespace) -> int:
    registry = load_profiles()
    if args.name not in registry.profiles:
        print(
            f"Error: Unknown profile '{args.name}'. Available: {', '.join(registry.profiles.keys())}",
            file=sys.stderr,
        )
        return 1
    registry.active_profile = args.name
    save_profiles(registry)
    print(f"Switched active profile to '{args.name}'.")
    return 0


def _cmd_add(args: argparse.Namespace) -> int:
    registry = load_profiles()
    name = args.name.strip()
    if not name:
        print("Error: Profile name cannot be empty.", file=sys.stderr)
        return 1
    if name in registry.profiles and not args.force:
        print(f"Error: Profile '{name}' already exists. Use --force to overwrite.", file=sys.stderr)
        return 1

    entry = ProfileEntry(
        name=name,
        email=args.email or "",
        gemini_dir=args.dir or "",
        description=args.desc or "",
        enabled=True,
    )
    registry.profiles[name] = entry
    save_profiles(registry)
    print(f"Profile '{name}' added successfully.")
    print(f"Gemini Directory: {entry.get_gemini_path()}")
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
    registry.routes = [r for r in registry.routes if r.profile != name]
    save_profiles(registry)
    print(f"Profile '{name}' removed.")
    return 0


def _cmd_route(args: argparse.Namespace) -> int:
    registry = load_profiles()
    if args.route_action == "add":
        if args.profile not in registry.profiles:
            print(f"Error: Profile '{args.profile}' does not exist.", file=sys.stderr)
            return 1
        registry.routes.append(ProfileRouteRule(pattern=args.pattern, profile=args.profile))
        save_profiles(registry)
        print(f"Route added: '{args.pattern}' -> {args.profile}")
        return 0
    elif args.route_action == "remove":
        orig_len = len(registry.routes)
        registry.routes = [r for r in registry.routes if r.pattern != args.pattern]
        if len(registry.routes) == orig_len:
            print(f"No route found matching pattern '{args.pattern}'.", file=sys.stderr)
            return 1
        save_profiles(registry)
        print(f"Removed route pattern '{args.pattern}'.")
        return 0
    else:  # list
        print("Repository & Workspace Routing Rules:")
        print("-" * 60)
        print(f"{'Pattern':<40} {'Target Profile':<20}")
        print("-" * 60)
        for r in registry.routes:
            print(f"{r.pattern:<40} {r.profile:<20}")
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

    print("Token Usage & Telemetry Summary:")
    print("=" * 85)
    print(
        f"{'Profile':<15} {'Sessions':<10} {'Input Tok':<14} {'Output Tok':<14} {'Cache Read':<16} {'Total Tokens':<14}"
    )
    print("-" * 85)

    tot_in = 0
    tot_out = 0
    tot_cache = 0
    tot_all = 0
    tot_sess = 0

    for p in summary.get("profiles", []):
        name = p.get("profile", "")
        s_cnt = p.get("session_count", 0)
        tin = p.get("total_input", 0) or 0
        tout = p.get("total_output", 0) or 0
        tcache = p.get("total_cache", 0) or 0
        tall = p.get("total_tokens", 0) or 0

        tot_sess += s_cnt
        tot_in += tin
        tot_out += tout
        tot_cache += tcache
        tot_all += tall

        print(f"{name:<15} {s_cnt:<10} {tin:<14,} {tout:<14,} {tcache:<16,} {tall:<14,}")

    print("=" * 85)
    print(
        f"{'TOTAL':<15} {tot_sess:<10} {tot_in:<14,} {tot_out:<14,} {tot_cache:<16,} {tot_all:<14,}"
    )
    print("=" * 85)

    recent = summary.get("recent_sessions", [])
    if recent:
        print("\nRecent Sessions Breakdown (Top 10):")
        print("-" * 90)
        print(
            f"{'Conversation ID':<38} {'Profile':<10} {'Input':<10} {'Output':<10} {'Cache':<12} {'Total':<12}"
        )
        print("-" * 90)
        for s in recent[:10]:
            cid = s.get("conversation_id", "")[:36]
            prof = s.get("profile", "")
            ti = s.get("total_input_tokens", 0) or 0
            to = s.get("total_output_tokens", 0) or 0
            tc = s.get("total_cache_read_tokens", 0) or 0
            tt = s.get("total_tokens", 0) or 0
            print(f"{cid:<38} {prof:<10} {ti:<10,} {to:<10,} {tc:<12,} {tt:<12,}")
        print("-" * 90)

    return 0


def _cmd_harvest(args: argparse.Namespace) -> int:
    registry = load_profiles()
    res = harvest_usage(registry)
    print(
        f"Harvest complete: {res.get('conversations_processed', 0)} conversations checked, {res.get('new_steps', 0)} steps indexed."
    )
    return 0


def _cmd_run(args: argparse.Namespace, remaining_args: list[str]) -> int:
    return execute_with_profile(
        args=remaining_args,
        requested_profile=args.profile,
        cwd=Path.cwd(),
        auto_failover=not args.no_failover,
    )


def build_profile_subparser(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="profile_subcommand")

    # list
    sub.add_parser("list", help="list configured profiles and usage")

    # current
    sub.add_parser("current", help="show active profile for cwd")

    # switch
    sw = sub.add_parser("switch", help="set active profile")
    sw.add_argument("name", help="profile name to activate")

    # add
    ad = sub.add_parser("add", help="register a new profile")
    ad.add_argument("name", help="profile name")
    ad.add_argument("--email", help="associated Google email")
    ad.add_argument("--dir", help="custom gemini directory path")
    ad.add_argument("--desc", help="profile description")
    ad.add_argument("--force", action="store_true", help="overwrite existing profile")

    # remove
    rm = sub.add_parser("remove", help="remove a profile")
    rm.add_argument("name", help="profile name to remove")

    # route
    rt = sub.add_parser("route", help="manage workspace routing rules")
    rt_sub = rt.add_subparsers(dest="route_action")
    rt_sub.add_parser("list", help="list route rules")
    rt_add = rt_sub.add_parser("add", help="add route rule")
    rt_add.add_argument("pattern", help="glob pattern for directory or repo")
    rt_add.add_argument("profile", help="target profile name")
    rt_rm = rt_sub.add_parser("remove", help="remove route rule")
    rt_rm.add_argument("pattern", help="glob pattern to remove")

    # usage
    us = sub.add_parser("usage", help="view token usage summary")
    us.add_argument("--profile", help="filter to specific profile")
    us.add_argument("--json", action="store_true", help="output JSON")

    # harvest
    sub.add_parser("harvest", help="sync usage logs into telemetry database")

    # run
    rn = sub.add_parser("run", help="run agy with the appropriate profile")
    rn.add_argument("--profile", help="force specific profile")
    rn.add_argument(
        "--no-failover", action="store_true", help="disable automatic failover on error"
    )


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)

    # If invoked directly as `agy-profile` with agy flags or without subcommands,
    # default to `run` if the first argument isn't one of the subcommands.
    known_commands = {
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

    if not raw_argv or (raw_argv[0] not in known_commands and not raw_argv[0].startswith("-h")):
        parser = argparse.ArgumentParser(
            prog="agy-profile",
            description="Profile-aware launcher and usage tracer for Google Antigravity CLI",
        )
        parser.add_argument("--profile", help="force specific profile")
        parser.add_argument(
            "--no-failover", action="store_true", help="disable automatic failover on error"
        )
        parsed, remaining = parser.parse_known_args(raw_argv)
        return _cmd_run(parsed, remaining)

    parser = argparse.ArgumentParser(
        prog="agy-profile",
        description="Profile manager and telemetry tracker for Google AGY",
    )
    build_profile_subparser(parser)

    # If first arg is 'run', separate remaining args to pass through to agy
    if raw_argv and raw_argv[0] == "run":
        run_parser = argparse.ArgumentParser(prog="agy-profile run")
        run_parser.add_argument("--profile", help="force specific profile")
        run_parser.add_argument("--no-failover", action="store_true", help="disable failover")
        parsed, remaining = run_parser.parse_known_args(raw_argv[1:])
        return _cmd_run(parsed, remaining)

    args = parser.parse_args(raw_argv)
    cmd = args.profile_subcommand

    if cmd == "list":
        return _cmd_list(args)
    elif cmd == "current":
        return _cmd_current(args)
    elif cmd == "switch":
        return _cmd_switch(args)
    elif cmd == "add":
        return _cmd_add(args)
    elif cmd == "remove":
        return _cmd_remove(args)
    elif cmd == "route":
        return _cmd_route(args)
    elif cmd == "usage":
        return _cmd_usage(args)
    elif cmd == "harvest":
        return _cmd_harvest(args)
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
