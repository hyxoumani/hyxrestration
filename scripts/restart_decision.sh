# shellcheck shell=bash
# ^ This file has no shebang ON PURPOSE -- it is sourced by promote.sh, never
#   executed -- so shellcheck cannot infer the dialect (SC2148) and the
#   directive states it instead. The functions below use bash-only syntax
#   ([[ ]], local, (( ))); a `sh` reading of them is not a thing that works.
#
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
# WHY A CONSTANT (decided 2026-09-02) AND WHY IT IS NO LONGER ONE ALONE
# (2026-09-18). The 2026-09-02 alternative was to price the run's age
# against the requirement the LIVE run publishes, so the guard protects a
# run for as long as its own control wants. It was refused for three
# measured reasons. Reason (1) is now spent and reasons (2) and (3) are
# answered below rather than re-argued; the constant survives as a FLOOR.
#
#   (1) SPENT. "The number does not exist when it matters" named
#       `own_days_needed`, which is the OFF-PANEL counterfactual and is
#       None for any run that HAS a panel. The LEVEL path publishes
#       `panel_days_needed` for every run with a balanced panel -- open,
#       closed, scored or underpowered. The 2026-09-02 comment set its
#       own revisit condition ("if a reading ever publishes [the
#       requirement] for an OPEN run below MIN_DAYS") and the live run
#       meets it: 5 panel days of 10, at 149.7 h up, read 2026-09-18.
#
#   (2) ANSWERED BY THE CAP. "A 10-day floor with promotes every ~2 days
#       is not a guard, it is 'shadow is never restarted by promote'."
#       True of an UNBOUNDED deferral, so the deferral is bounded:
#       PANEL_GUARD_MAX_S releases it, and the requirement itself ends it
#       (at `needed` panel days the guard stops deferring for good, and
#       that event is the whole reason the panel is being accumulated).
#       The frequency worry is also measurable, and it was overstated: a
#       deferral costs nothing on a promote that does not move shadow's
#       closure, and since 2026-09-02 exactly TWO promotes moved it.
#
#   (3) ANSWERED BY THE PRINT, which was already the design. "Killing
#       above MIN_DAYS costs power, not scorability, and that is a
#       judgment the operator makes per promote" -- still true, and the
#       operator still makes it with --restart-young. What changed is the
#       number they are shown. On 2026-09-07 they were shown "211h", said
#       nothing, and the default killed a run at 8 of the 10 panel days
#       it needed. A judgment offered the wrong statistic is not a
#       judgment; the guard now prints the shortfall that decides.
#
# WHAT THE OLD CONSTANT MEASURED, AND WHY IT WAS THE WRONG QUANTITY.
# MIN_DAYS (3) is the PROFILE threshold -- below it `profile_status`
# reads underpowered. The run is not accumulated for its profile; it is
# accumulated for `level_shape_status`, which needs `panel_days_needed`
# = 10 panel days. MEASURED 2026-09-18 over the 54-run ledger: NO run has
# ever reached 10. The two closest died at 9 and 8. In the 16 days this
# guard has been live it deferred ZERO restarts and waved through TWO --
# `20260829T191841` at 8 panel days, killed 81 s after the 54d05c7
# promote, and `20260907T142900` at 4. The closure guard was right both
# times (`hyxlab/store.py` is in shadow's closure); the age guard was
# asleep because 211 h is not younger than 72 h.
#
# UPTIME IS ALSO THE WRONG UNIT, which is why the ceiling is expressed in
# panel days and queried rather than converted. A panel day needs a whole
# clock of whole hours, so it lags wall-clock by a NON-CONSTANT amount:
# 253.2 h (10.6 d) -> 9 panel days, 211.2 h (8.8 d) -> 8, 149.7 h
# (6.2 d) -> 5. The lag runs 1.0-1.6 days, so any threshold in seconds is
# wrong by about two days before it is wrong by anything else.
#
# THE FLOOR STAYS A CONSTANT, and reason (3)'s bound 7 argument is why: a
# run below MIN_DAYS has its deltas discarded whole, and a run that young
# often has NO balanced panel at all, so the query has no number to
# return. Floor on age, ceiling on the panel, and an absent panel number
# leaves the floor in charge -- never "defer forever".

# panel_shortfall_days -> "BANKED NEEDED" for the live shadow run, or ""
#   when there is no measured shortfall (no open run, no balanced panel
#   yet, or the query failed). Read-only on the shadow ledger, ~0.25 s.
#   PANEL_SHORTFALL_CMD is the injection point the tests use; nothing
#   else should set it.
PANEL_SHORTFALL_CMD="${PANEL_SHORTFALL_CMD:-}"
panel_shortfall_days() {
    local out rid banked needed
    if [[ -n "$PANEL_SHORTFALL_CMD" ]]; then
        out=$($PANEL_SHORTFALL_CMD 2>/dev/null) || return 0
    else
        # Both CWD-independent ON PURPOSE. `-m` resolves the package off
        # the CWD, and `SHADOW_DB` is a RELATIVE path, so a promote run
        # from anywhere but $DEV would either fail to import or read a
        # ledger that does not exist. $STABLE/data is a symlink to
        # $DEV/data -- one archive, two trees -- so the absolute path
        # names the live ledger either way. Dev's code, as needs_restart
        # already does: the tree being promoted is the one that decides.
        out=$(cd "$DEV" && "$DEV/.venv/bin/python" -m simulator.shadow_diurnal \
              --panel-shortfall --ledger "$DEV/data/hyxshadow.duckdb" 2>/dev/null) || return 0
    fi
    # `panel_shortfall RUN_ID BANKED NEEDED` or `panel_shortfall none REASON`.
    read -r _ rid banked needed <<<"$out"
    [[ "$rid" != none && "$banked" =~ ^[0-9]+$ && "$needed" =~ ^[0-9]+$ ]] || return 0
    echo "$banked $needed"
}

# ---------------------------------------------------------------- bound 14b
# young_run_guard UNIT AGE_S
#   Returns 0 iff restarting UNIT should be DEFERRED, because either
#     * FLOOR: its live run is younger than YOUNG_RUN_S (default 3 days),
#       below which the run's deltas are discarded whole (bound 7); or
#     * PANEL CEILING: the run's level panel is short of the
#       `panel_days_needed` it publishes, and the unit is younger than
#       PANEL_GUARD_MAX_S.
#   and the caller has not said --restart-all (FORCE_RESTART=1) or
#   --restart-young (RESTART_YOUNG=1).
#   An AGE_S that is empty or not a number reads as "unknown" and does NOT
#   defer: the guard protects a measured span, never an assumed one. An
#   absent panel number reads the same way -- the floor stays in charge.
YOUNG_RUN_S="${YOUNG_RUN_S:-259200}"
# The cap that keeps the deferral from becoming "never restart" (reason 2).
# 14 days: the requirement is 10 panel days and the measured wall-to-panel
# lag is 1.0-1.6 days, so a HEALTHY run reaches its panel in <= ~12 days
# and this never binds on one. It binds on a run whose panel stopped
# growing -- which is the case where deferring buys nothing at all.
PANEL_GUARD_MAX_S="${PANEL_GUARD_MAX_S:-1209600}"
young_run_guard() {
    local unit="$1" age="$2" panel banked needed
    [[ "${FORCE_RESTART:-0}" == 1 || "${RESTART_YOUNG:-0}" == 1 ]] && return 1
    [[ "$age" =~ ^[0-9]+$ ]] || { echo "   NOTE: $unit age unknown ($age); not deferring on age"; return 1; }
    if (( age < YOUNG_RUN_S )); then
        echo "   YOUNG RUN: $unit has been up $(( age / 3600 ))h of the $(( YOUNG_RUN_S / 3600 ))h a scorable run needs;"
        echo "             its code moved, but restarting now discards that span."
        echo "             Deferred: it runs OLD code until its next natural restart."
        echo "             Force with --restart-young (this unit) or --restart-all."
        return 0
    fi
    panel=$(panel_shortfall_days)
    if [[ -z "$panel" ]]; then
        echo "   NOTE: $unit panel shortfall unknown; not deferring on the panel"
        return 1
    fi
    read -r banked needed <<<"$panel"
    if (( banked >= needed )); then
        echo "   PANEL COMPLETE: $unit holds $banked of the $needed panel days its level shape needs;"
        echo "                   restarting costs power, not scorability. Not deferred."
        return 1
    fi
    if (( age >= PANEL_GUARD_MAX_S )); then
        echo "   PANEL STALLED: $unit is $(( age / 86400 ))d up and still only $banked of $needed panel days,"
        echo "                  past the $(( PANEL_GUARD_MAX_S / 86400 ))d cap. A panel this slow will not"
        echo "                  arrive; deferring further only ages the code. Not deferred."
        return 1
    fi
    echo "   SHORT PANEL: $unit has been up $(( age / 3600 ))h and banked $banked of the $needed panel days"
    echo "                its level shape needs (panel days lag wall days; this is the run's own number)."
    echo "                Restarting now resets the panel to zero and discards all $banked."
    echo "                Deferred: it runs OLD code until its next natural restart."
    echo "                Force with --restart-young (this unit) or --restart-all."
    return 0
}

# unit_age_s UNIT -> seconds since the unit last entered 'active', or ""
# when systemd cannot say (inactive, never started, no user manager).
unit_age_s() {
    local ts now started
    ts=$(systemctl --user show "$1" -p ActiveEnterTimestamp --value 2>/dev/null) || return 0
    [[ -n "$ts" && "$ts" != "n/a" ]] || return 0
    started=$(date -d "$ts" +%s 2>/dev/null) || return 0
    now=$(date +%s)
    echo $(( now - started ))
}
