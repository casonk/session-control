#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE_PATH="$ROOT/config/clockwork/prune-sessions.toml.template"
CLOCKWORK_REPO="${CLOCKWORK_REPO:-$ROOT/../clockwork}"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
OLDER_THAN="${SESSION_CONTROL_PRUNE_OLDER_THAN:-180d}"
RENDER_ONLY=0

usage() {
  printf 'Usage: %s [--older-than 180d] [--unit-dir DIR] [--clockwork-repo DIR] [--render-only]\n' "$0"
}

run_clockwork() {
  if command -v clockwork >/dev/null 2>&1; then
    clockwork "$@"
    return
  fi
  if [[ -d "$CLOCKWORK_REPO/src/clockwork" ]]; then
    PYTHONPATH="$CLOCKWORK_REPO/src${PYTHONPATH:+:$PYTHONPATH}" python3 -m clockwork "$@"
    return
  fi
  printf 'Could not find clockwork on PATH or at %s\n' "$CLOCKWORK_REPO" >&2
  return 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --older-than)
      OLDER_THAN="${2:?missing value for --older-than}"
      shift 2
      ;;
    --unit-dir)
      UNIT_DIR="${2:?missing value for --unit-dir}"
      shift 2
      ;;
    --clockwork-repo)
      CLOCKWORK_REPO="$(cd "${2:?missing value for --clockwork-repo}" && pwd)"
      shift 2
      ;;
    --render-only)
      RENDER_ONLY=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
done

if [[ ! -x "$ROOT/.venv/bin/session-control" ]]; then
  printf 'Expected executable not found: %s/.venv/bin/session-control\n' "$ROOT" >&2
  printf 'Run: python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"\n' >&2
  exit 1
fi

TMP_MANIFEST="$(mktemp "${TMPDIR:-/tmp}/session-control-prune.XXXXXX.toml")"
trap 'rm -f "$TMP_MANIFEST"' EXIT

sed \
  -e "s#__REPO_ROOT__#$ROOT#g" \
  -e "s#__OLDER_THAN__#$OLDER_THAN#g" \
  "$TEMPLATE_PATH" > "$TMP_MANIFEST"

if [[ "$RENDER_ONLY" -eq 1 ]]; then
  run_clockwork render --manifest "$TMP_MANIFEST" --target systemd-user
  exit 0
fi

run_clockwork install --manifest "$TMP_MANIFEST" --target systemd-user --unit-dir "$UNIT_DIR"
systemctl --user daemon-reload
systemctl --user enable --now session-control-prune.timer
systemctl --user list-timers session-control-prune.timer --no-pager
