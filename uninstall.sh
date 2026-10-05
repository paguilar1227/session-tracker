#!/bin/sh
# Session Tracker: uninstall. Keeps your data unless you pass --purge.
# Usage: ./uninstall.sh [--purge]
set -eu
cd "$(dirname "$0")"
HERE=$(pwd -P)
PY=""
for f in "$HOME"/Library/LaunchAgents/*.plist; do
  [ -f "$f" ] || continue
  [ "$(plutil -extract WorkingDirectory raw "$f" 2>/dev/null || true)" = "$HERE" ] || continue
  PY=$(plutil -extract ProgramArguments.0 raw "$f" 2>/dev/null || true)
  if [ -z "${CODEX_HOME:-}" ]; then
    saved=$(plutil -extract EnvironmentVariables.CODEX_HOME raw "$f" 2>/dev/null || true)
    if [ -n "$saved" ]; then export CODEX_HOME="$saved"; fi
  fi
  break
done
[ -n "$PY" ] || PY=$(command -v python3)
"$PY" -m tracker uninstall
for a in "$@"; do
  if [ "$a" = "--purge" ]; then
    DATA="${ST_DATA_DIR:-$HOME/.session-tracker}"
    if [ -d "$DATA" ]; then
      printf 'Move %s (database, logs, spool) to the Trash? [y/N] ' "$DATA"
      read -r answer
      case "$answer" in
        y|Y|yes) dest="$HOME/.Trash/session-tracker-$(date +%Y%m%d-%H%M%S)"; mv "$DATA" "$dest"; echo "Moved to $dest" ;;
        *) echo "Kept $DATA" ;;
      esac
    fi
  fi
done
echo "Artifacts and references stay in ~/.codex/visualizations (they belong to your Codex sessions)."
