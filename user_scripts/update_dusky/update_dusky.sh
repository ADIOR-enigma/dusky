#!/usr/bin/env bash
# ==============================================================================
#  DUSKY UPDATER — ROUTER (v9.6.0)
# ==============================================================================
#  The legacy bash engine has been superseded by the maintained Python updater
#  at: <dir>/python/update_dusky.py
#
#  This router preserves the old entry point so existing launchers
#  (desktop entries, quickpanel, keybinds, aliases) keep working unchanged.
#  All arguments are passed straight through to the Python updater.
#
#  Usage is identical to the Python updater, e.g.:
#      update_dusky.sh                       # full TUI update
#      update_dusky.sh --sync-only           # git sync only
#      update_dusky.sh --list                # list active scripts
#      update_dusky.sh --dry-run             # simulate without changes
# ==============================================================================

set -u

# Resolve this script's real directory (survives symlinks and foreign cwds).
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)" || {
    printf '%s\n' "[FATAL] Could not determine updater directory." >&2
    exit 1
}

PY_UPDATER="${SCRIPT_DIR}/python/update_dusky.py"

if [[ ! -f "$PY_UPDATER" ]]; then
    printf '%s\n' "[FATAL] Python updater not found: ${PY_UPDATER}" >&2
    printf '%s\n' "Expected it at: ${SCRIPT_DIR}/python/update_dusky.py" >&2
    printf '%s\n' "The Python updater must be deployed before this router can run." >&2
    exit 1
fi

exec python3 "$PY_UPDATER" "$@"
