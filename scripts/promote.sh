#!/usr/bin/env bash
# Promote dev main -> stable deployment worktree and restart the daemons.
#
# The systemd units (collect/sweep/stream) run from
# /home/devs/workspace/hyxrestration-stable, a git worktree pinned to the
# `stable` branch, so working-tree churn in the dev checkout can never
# break running data capture. This script is the ONLY supported way to
# ship collection-side changes: it refuses to promote unless the full
# suite is green in the dev tree.
#
# A PROMOTE IS NOT ONE INDIVISIBLE ACT (EXP-961). Restarting a daemon costs
# whatever that daemon had accumulated. The rule: restart a daemon only when
# the promotion actually moves code that daemon RUNS. `--restart-all` forces
# the old behaviour.
#
# RE-COSTED 2026-08-23. The original rationale was that an unbroken
# `hyxlab-shadow` run is "the scarcest asset in the archive" because
# `shadow_settlements` was 0 rows. That premise is dead:
# `shadow_settlements` now holds 2,190 rows across 4 runs (509/112/1324/245),
# so settlements are no longer scarce and protecting the guard on those
# grounds would be paying a premium for something the archive already has.
# The guard still earns its place, for a DIFFERENT asset: contiguous
# DURATION. The diurnal analyses (`simulator/shadow_diurnal.py`) are judged
# on per-hour draw counts and pairwise day agreement, and read UNDERPOWERED
# below ~3 spanned days with 3 draws per hour — a restart resets that clock
# to zero no matter how many settlements are already banked. So the cost of
# a needless restart is measured in DAYS OF SPAN, not in settlement rows,
# and the 2026-08-23 promote that killed a 2d18h run lost exactly that and
# nothing else. Re-derive this cost again if the analyses change shape.
set -euo pipefail

DEV=/home/devs/workspace/hyxrestration
STABLE=/home/devs/workspace/hyxrestration-stable

FORCE_RESTART=0
RESTART_YOUNG=0
DEFER=""
for arg in "$@"; do
    case "$arg" in
        --restart-all) FORCE_RESTART=1 ;;
        # --restart-young: restart hyxlab-shadow even though its live run
        # is younger than the span a scorable run needs (bound 14; see
        # young_run_guard in restart_decision.sh). Without it, a young
        # shadow run is DEFERRED automatically, because 2026-09-02's
        # measurement found all 15 day-starved runs were stopped, not
        # starved, and 19 promotes did the stopping.
        --restart-young) RESTART_YOUNG=1 ;;
        # --defer=UNIT[,UNIT]: promote everything but leave the named
        # daemon(s) running old code until their next natural break.
        # For when the guard is RIGHT that code the daemon runs moved,
        # but the daemon only reads through it and what it has
        # accumulated is worth more than the new code (2026-08-04: a
        # store-kernel speedup fired the shadow restart rule while
        # shadow was ~24h into the first run ever positioned to observe
        # a settlement — the third pass forced to decompose by hand, so
        # the exception is now stated to the script, not run around it).
        --defer=*) DEFER="${arg#--defer=}" ;;
    esac
done

echo "== tests (dev tree) =="
cd "$DEV"
.venv/bin/python -m pytest tests/ -q

echo "== what this promotion moves =="
# Computed BEFORE the fast-forward, while stable still points at the
# currently-deployed commit.
CHANGED=$(git -C "$STABLE" diff --name-only HEAD main || true)
printf '%s\n' "${CHANGED:-(nothing)}" | sed 's/^/   /'

# Each daemon's ExecStart module, plus everything it imports.
# hyxlab-stream: collector.streamd | hyxlab-shadow: simulator.shadow
# hyxlab-simui is the third Type=simple unit and is deliberately NOT in
# the restart list: it holds a live paper-trading session that a restart
# would drop (tests/test_systemd_units.py pins that exclusion). But it is
# also `Restart=always` running from stable, which makes it the one
# daemon with NO path to new code at all — it ran whatever it started
# with from its install (2026-08-20) through every promotion, silently,
# because "not restarted" was implemented as "not mentioned". It is now
# mentioned: the operator is told its code moved and restarts it when the
# UI is idle. Its closure root is `simulator.simui.__main__`, not
# `simulator.simui` — `python -m PKG` executes the package's __main__,
# and rooting at a docstring-only __init__.py is a rule that always
# answers no.
# needs_restart (scripts/restart_decision.sh, EXP-1276) intersects CHANGED
# with the daemon's static import closure via scripts/daemon_imports.py —
# the old directory regexes survive only as the fallback if the tool errors.
# (Three promotions on 2026-08-12 hit the regexes' coarseness: sweep-only /
# venue-only changes demanded hand-verified --defer=hyxlab-stream.service.)
source "$DEV/scripts/restart_decision.sh"

echo "== fast-forward stable -> main =="
git -C "$STABLE" merge --ff-only main

echo "== sync stable venv deps =="
"$STABLE/.venv/bin/pip" install -q -r "$DEV/scripts/requirements-stable.txt"

echo "== smoke-import in stable venv =="
(cd "$STABLE" && .venv/bin/python -c "import collector.streamd, collector.collect, collector.sweep, simulator.shadow, simulator.simui.server")

echo "== install systemd units (repo scripts/systemd/ is canonical) =="
cp "$DEV"/scripts/systemd/hyxlab-* ~/.config/systemd/user/
systemctl --user daemon-reload

echo "== restart daemons whose code moved (timers pick up new code on next run) =="
RESTART=()
needs_restart collector.streamd '^(collector|hyxlab)/' && RESTART+=(hyxlab-stream.service)
needs_restart simulator.shadow '^(simulator|strategies|hyxlab)/' && RESTART+=(hyxlab-shadow.service)
if needs_restart simulator.simui.__main__ '^(simulator|strategies|hyxlab)/'; then
    echo "   NOTICE: hyxlab-simui.service executes changed code and is NOT auto-restarted"
    echo "           (it holds a live paper session). Restart it when the UI is idle:"
    echo "           systemctl --user restart hyxlab-simui.service"
fi
# Bound 14: the closure guard asks "did the code move"; this asks "how
# old is the run you are about to kill". A young shadow run is deferred
# unless the operator says otherwise, and the deferral is printed with
# the age, so it is recorded the same way an explicit --defer is.
if [[ " ${RESTART[*]} " == *" hyxlab-shadow.service "* ]] \
   && young_run_guard hyxlab-shadow.service "$(unit_age_s hyxlab-shadow.service)"; then
    DEFER="${DEFER:+$DEFER,}hyxlab-shadow.service"
fi
if [[ -n "$DEFER" ]]; then
    KEPT=()
    for u in "${RESTART[@]}"; do
        if [[ ",$DEFER," == *",$u,"* ]]; then
            echo "   DEFERRED: $u — its code moved but its restart is deferred;"
            echo "             it runs OLD code until its next restart. Record this."
        else
            KEPT+=("$u")
        fi
    done
    RESTART=()
    ((${#KEPT[@]})) && RESTART=("${KEPT[@]}")
fi
if ((${#RESTART[@]})); then
    echo "   restarting: ${RESTART[*]}"
    systemctl --user restart "${RESTART[@]}"
    sleep 3
    systemctl --user is-active "${RESTART[@]}"
else
    echo "   none — no daemon's code moved (use --restart-all to force)"
fi
for u in hyxlab-stream.service hyxlab-shadow.service hyxlab-simui.service; do
    printf '   %-24s %s  since %s\n' "$u" \
        "$(systemctl --user is-active "$u")" \
        "$(systemctl --user show "$u" -p ActiveEnterTimestamp --value)"
done

echo "== promoted: $(git -C "$STABLE" log --oneline -1) =="
