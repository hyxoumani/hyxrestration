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

# ---------------------------------------------------------------- bound 14
# A shadow restart costs the LIVE RUN, and the run's value is its span:
# `simulator/shadow_diurnal.py` judges a run UNDERPOWERED below MIN_DAYS
# (3) whole days per hour-of-day, and its 12Z control needs ~10 panel
# days. Measured 2026-09-02 over 51 closed runs: 19 ended within 3 min
# of a promote, and ALL 15 of the runs the diurnal census calls
# day-starved (`no_balanced_panel`, median span 23.4 h) ended by a stop
# with the successor writing within 15 min -- not one ran out of data.
# The restart guard above was right every time (the closure moved), and
# the famine happened anyway, because "the closure moved" was the only
# question asked. This asks the second one: how old is what you are
# about to kill.
#
# WHY A CONSTANT (decided 2026-09-02, closing the question the lifetime
# pass left open). The alternative was to price the run's age against
# `own_days_needed` of the LIVE run, read off the latest diurnal
# reading, so the guard protects a run for as long as its own control
# wants (~10 days for 12Z). Refused, for three measured reasons:
#   (1) The number does not exist when it matters. A run below MIN_DAYS
#       is unscored, and an unscored run publishes `own_days_needed`
#       = None (the 2026-09-02 14:24Z reading carries None for the
#       live run at 91 h). The guard would then fall back to a constant
#       exactly in the window it was written for.
#   (2) The deferral's cost is also a function of age: a deferred
#       daemon runs OLD code, and every promote it survives widens the
#       gap between what it runs and what the tests test. A 10-day
#       floor with promotes every ~2 days is not a guard, it is "shadow
#       is never restarted by promote", stated indirectly.
#   (3) MIN_DAYS is where a run's span stops being worthless -- kill it
#       below that and its deltas are discarded whole (bound 7). Above
#       it, killing costs power, not scorability, and that is a judgment
#       the operator makes per promote ("is this worth restarting shadow
#       for?" -- status.md's standing rule), not one a script should make
#       from a stale JSON. The guard prints the age so that judgment has
#       its number.
# Revisit if a reading ever publishes `own_days_needed` for an OPEN run
# below MIN_DAYS -- then (1) stops being true.
#
# young_run_guard UNIT AGE_S
#   Returns 0 iff restarting UNIT should be DEFERRED because its live run
#   is younger than YOUNG_RUN_S (default 3 days) and the caller has not
#   said --restart-all (FORCE_RESTART=1) or --restart-young (RESTART_YOUNG=1).
#   An AGE_S that is empty or not a number reads as "unknown" and does NOT
#   defer: the guard protects a measured span, never an assumed one.
YOUNG_RUN_S="${YOUNG_RUN_S:-259200}"
young_run_guard() {
    local unit="$1" age="$2"
    [[ "${FORCE_RESTART:-0}" == 1 || "${RESTART_YOUNG:-0}" == 1 ]] && return 1
    [[ "$age" =~ ^[0-9]+$ ]] || { echo "   NOTE: $unit age unknown ($age); not deferring on age"; return 1; }
    if (( age < YOUNG_RUN_S )); then
        echo "   YOUNG RUN: $unit has been up $(( age / 3600 ))h of the $(( YOUNG_RUN_S / 3600 ))h a scorable run needs;"
        echo "             its code moved, but restarting now discards that span."
        echo "             Deferred: it runs OLD code until its next natural restart."
        echo "             Force with --restart-young (this unit) or --restart-all."
        return 0
    fi
    return 1
}

# unit_age_s UNIT -> seconds since the unit last entered 'active', or ""
# when systemd cannot say (inactive, never started, no user manager).
unit_age_s() {
    local ts now then
    ts=$(systemctl --user show "$1" -p ActiveEnterTimestamp --value 2>/dev/null) || return 0
    [[ -n "$ts" && "$ts" != "n/a" ]] || return 0
    then=$(date -d "$ts" +%s 2>/dev/null) || return 0
    now=$(date +%s)
    echo $(( now - then ))
}
