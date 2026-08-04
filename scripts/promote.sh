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
# whatever that daemon had accumulated, and for `hyxlab-shadow` that is the
# scarcest asset in the archive: an unbroken run long enough to observe a
# settlement (~16h, and `shadow_settlements` is still 0 rows archive-wide).
# Two consecutive passes had to decompose this script by hand to avoid
# killing a live run for changes that touched only timer-driven code, so the
# rule is encoded here instead of re-derived each time: restart a daemon only
# when the promotion actually moves code that daemon RUNS. `--restart-all`
# forces the old behaviour.
set -euo pipefail

DEV=/home/devs/workspace/hyxrestration
STABLE=/home/devs/workspace/hyxrestration-stable

FORCE_RESTART=0
DEFER=""
for arg in "$@"; do
    case "$arg" in
        --restart-all) FORCE_RESTART=1 ;;
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

# Each daemon's ExecStart module, plus the shared kernel it imports.
# hyxlab-stream: collector.streamd | hyxlab-shadow: simulator.shadow
needs_restart() {  # $1 = extended regex of paths that unit executes
    [[ "$FORCE_RESTART" == 1 ]] && return 0
    grep -qE "$1" <<<"$CHANGED"
}

echo "== fast-forward stable -> main =="
git -C "$STABLE" merge --ff-only main

echo "== sync stable venv deps =="
"$STABLE/.venv/bin/pip" install -q -r "$DEV/scripts/requirements-stable.txt"

echo "== smoke-import in stable venv =="
(cd "$STABLE" && .venv/bin/python -c "import collector.streamd, collector.collect, collector.sweep, simulator.shadow")

echo "== install systemd units (repo scripts/systemd/ is canonical) =="
cp "$DEV"/scripts/systemd/hyxlab-* ~/.config/systemd/user/
systemctl --user daemon-reload

echo "== restart daemons whose code moved (timers pick up new code on next run) =="
RESTART=()
needs_restart '^(collector|hyxlab)/' && RESTART+=(hyxlab-stream.service)
needs_restart '^(simulator|strategies|hyxlab)/' && RESTART+=(hyxlab-shadow.service)
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
for u in hyxlab-stream.service hyxlab-shadow.service; do
    printf '   %-24s %s  since %s\n' "$u" \
        "$(systemctl --user is-active "$u")" \
        "$(systemctl --user show "$u" -p ActiveEnterTimestamp --value)"
done

echo "== promoted: $(git -C "$STABLE" log --oneline -1) =="
