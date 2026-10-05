#!/bin/sh
# Session Tracker: install or update. Safe to re-run.
# Usage: ./install.sh [--with-canvas] [--open]
# Optional settings, kept for later re-runs: ST_EXTRACT_MODEL, ST_EXTRACT_EFFORT, ST_EXTRACT_ENABLED=0, ST_GH_USER,
# ST_COPILOT_BASE, ST_COPILOT_INTEGRATION_ID, CODEX_HOME
set -eu
cd "$(dirname "$0")"
HERE=$(pwd -P)

say() { printf '%s\n' "$*"; }
fail() { printf 'install: %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || fail "macOS only (it uses launchd, Finder and the local Codex databases)."
command -v codex >/dev/null 2>&1 || fail "Codex CLI not found on PATH. Install Codex first: https://developers.openai.com/codex"

ok_python() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; }
PY="${PYTHON:-}"
PORT=8795
# An existing install of this folder: reuse its interpreter (the hook commands contain its path, and a new path makes
# Codex ask to approve the hooks again) and its Codex home.
AGENT=""
for f in "$HOME"/Library/LaunchAgents/*.plist; do
  [ -f "$f" ] || continue
  [ "$(plutil -extract WorkingDirectory raw "$f" 2>/dev/null || true)" = "$HERE" ] || continue
  [ "$(plutil -extract ProgramArguments.2 raw "$f" 2>/dev/null || true)" = "tracker" ] || continue
  AGENT="$f"
  break
done
if [ -n "$AGENT" ]; then
  [ -n "$PY" ] || PY=$(plutil -extract ProgramArguments.0 raw "$AGENT" 2>/dev/null || true)
  if [ -z "${CODEX_HOME:-}" ]; then
    saved=$(plutil -extract EnvironmentVariables.CODEX_HOME raw "$AGENT" 2>/dev/null || true)
    if [ -n "$saved" ]; then export CODEX_HOME="$saved"; fi
  fi
fi
if [ -z "$PY" ] || ! ok_python "$PY"; then
  PY=""
  for c in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 python3.14 python3.13 python3.12 python3.11; do
    if command -v "$c" >/dev/null 2>&1 && ok_python "$(command -v "$c")"; then PY=$(command -v "$c"); break; fi
  done
fi
[ -n "$PY" ] || fail "needs Python 3.11 or newer (try: brew install python)."

say "Using $PY"
"$PY" -m tracker install "$@"
if "$PY" -m unittest -q tests.test_tracker >/dev/null 2>&1; then say "Self-test: passed"; else say "Self-test: FAILED (run: $PY -m unittest tests.test_tracker)"; fi

i=0
until curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; do
  i=$((i + 1)); [ $i -gt 20 ] && fail "service did not come up; see ~/.session-tracker/logs/"
  sleep 0.5
done
say "Service: up at http://127.0.0.1:$PORT"

# The scribe (decisions, mistakes, issues and todo upkeep after every turn) runs on GitHub Copilot through the gh CLI.
if command -v gh >/dev/null 2>&1 && ! gh auth status >/dev/null 2>&1 && [ -t 0 ]; then
  printf 'The scribe uses GitHub Copilot through the gh CLI, which is not signed in. Sign in now? [Y/n] '
  read -r answer
  case "$answer" in n|N|no) ;; *) gh auth login || say "gh sign-in did not finish; run gh auth login later and re-run ./install.sh." ;; esac
fi
"$PY" -m tracker copilot || say "The service runs without the scribe until this is fixed."

for a in "$@"; do [ "$a" = "--open" ] && open "http://127.0.0.1:$PORT"; done
exit 0
