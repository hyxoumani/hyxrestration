# Restart decision for promote.sh (EXP-1276). Sourced, not executed, so
# tests can exercise the decision logic without touching live daemons.
#
# Expects in the caller's environment:
#   DEV            dev checkout root (has .venv and scripts/daemon_imports.py)
#   CHANGED        newline-separated changed paths (stable HEAD..main)
#   FORCE_RESTART  1 => always restart (--restart-all)
#
# needs_restart ROOT_MODULE FALLBACK_REGEX
#   Returns 0 iff the daemon whose ExecStart module is ROOT_MODULE must be
#   restarted. Primary: intersect CHANGED with the daemon's static import
#   closure (scripts/daemon_imports.py — lazy imports included, since a
#   running daemon would load NEW code mid-run on its next lazy import: a
#   half-old/half-new process). Fallback: if the tool errors, use the old
#   coarse directory regex and say so — conservative in the "restart too
#   much" direction, never "restart too little" silently.
needs_restart() {
    local root="$1" fallback_regex="$2" hits
    [[ "${FORCE_RESTART:-0}" == 1 ]] && return 0
    if hits=$(printf '%s\n' "${CHANGED:-}" \
              | "$DEV/.venv/bin/python" "$DEV/scripts/daemon_imports.py" intersect "$root"); then
        if [[ -n "$hits" ]]; then
            echo "   $root executes changed file(s): $(tr '\n' ' ' <<<"$hits")"
            return 0
        fi
        return 1
    fi
    echo "   WARNING: daemon_imports.py failed for $root — falling back to path regex '$fallback_regex'"
    grep -qE "$fallback_regex" <<<"${CHANGED:-}"
}
