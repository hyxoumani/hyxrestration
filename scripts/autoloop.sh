#!/usr/bin/env bash
# One bounded iteration of autonomous development (headless Claude Code).
# systemd timer is the while-loop; flock prevents overlapping iterations
# (and defers to any interactive session that grabs the lock first).
#
# The bound is THIS script's, not the unit's. `TimeoutStartSec=3h` on
# hyxlab-autoloop.service is the backstop, and a run that reaches it is
# filed `result=timeout` -- a FAILURE in `collector.health`'s RECENT line.
# Measured 2026-10-14: the never-stop working mode keeps ONE claude
# process alive across passes (ScheduleWakeup), so both of that day's runs
# lived the full 3h and were SIGTERMed by systemd at an arbitrary point --
# 11:15Z with a status entry uncommitted, 17:15Z five minutes after a
# commit (mistakes #122). So the launcher owns a budget strictly inside the
# unit's, exports the deadline, and files the stop as the designed one.
# The pass reads AUTOLOOP_DEADLINE (epoch seconds) and does not arm a
# wakeup it cannot finish before it -- scripts/autoloop-prompt.md.
set -euo pipefail
ROOT="${AUTOLOOP_ROOT:-/home/devs/workspace/hyxrestration}"
cd "$ROOT"
exec 9>data/autoloop.lock
flock -n 9 || { echo "[autoloop] another iteration or session holds the lock; skipping"; exit 0; }
git pull --rebase --quiet origin main || true

# 170 min + 5 min TERM->KILL grace = 175 min < the unit's 3h
# (tests/test_autoloop_budget.py pins the sum against the unit file).
BUDGET_S="${AUTOLOOP_BUDGET_S:-10200}"
GRACE_S="${AUTOLOOP_GRACE_S:-300}"
AUTOLOOP_DEADLINE=$(( $(date +%s) + BUDGET_S ))
export AUTOLOOP_DEADLINE
echo "[autoloop] budget ${BUDGET_S}s; deadline $(date -u -d "@${AUTOLOOP_DEADLINE}" +%H:%M:%SZ) (AUTOLOOP_DEADLINE=${AUTOLOOP_DEADLINE})"

rc=0
timeout --signal=TERM --kill-after="$GRACE_S" "$BUDGET_S" \
  claude -p "$(cat scripts/autoloop-prompt.md)" \
  --dangerously-skip-permissions \
  --max-turns 120 || rc=$?

case "$rc" in
  124) echo "[autoloop] budget ${BUDGET_S}s spent; bounded stop on SIGTERM (the timer is the next wakeup)"
       exit 0 ;;
  137) echo "[autoloop] budget ${BUDGET_S}s spent and SIGTERM ignored for ${GRACE_S}s; SIGKILLed -- a finding, not a stop"
       exit 137 ;;
esac
exit "$rc"
