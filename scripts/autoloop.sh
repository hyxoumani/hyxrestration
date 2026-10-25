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
#
# The body is ONE function and the last line calls it, then exits. bash
# reads a script by byte offset AS it executes, so a pass that commits a
# new launcher while `claude -p` runs rewrites this file under the bash
# waiting on claude, and when claude returns bash resumes at the old
# offset in the new text. Measured 2026-10-14 21:45Z: the 20:15Z run
# shipped 76a860c (this file, longer) mid-run and its own launcher died
# `line 13: syntax error near unexpected token ')'`, exit 2, unit `failed`
# (mistakes #123; tests/test_autoloop_budget.py replays it). A function is
# parsed whole before any of it runs, and `exit` on the call's own line
# keeps bash from reading past it. An editor that renames a new file over
# the path leaves the old inode to the running copy; one that truncates in
# place does not -- the script must not depend on which one was used.
#
# A refused call is not a crash. When the account is over its Claude usage
# limit, `claude -p` prints one line (`You've hit your weekly limit · resets
# 1pm (America/Chicago)`) and exits 1 -- the same status as any failure --
# and the digest's RECENT line filed both 2026-10-24 runs as `exit-code/1`
# (08:28Z mid-pass after 13 min of work, 14:15Z at the first call), which
# reads as the launcher or the pass having broken. Nothing in this repo can
# fix a usage limit; it is a USER item with a reset time, and the timer is
# its retry. So the launcher keeps a copy of claude's output
# (`data/autoloop.last.out`, the whole run's text, overwritten each run),
# and on a non-zero exit whose output carries the limit sentinel it names
# the refusal and exits 75 (EX_TEMPFAIL) -- the status `collector.collect`
# and `collector.breadth` already use for "declined, try again later", and
# the one `collector.health` documents as a skip, not a crash. Any other
# non-zero exit still passes through unchanged.
set -euo pipefail

main() {
  ROOT="${AUTOLOOP_ROOT:-/home/devs/workspace/hyxrestration}"
  cd "$ROOT"
  exec 9>data/autoloop.lock
  flock -n 9 || { echo "[autoloop] another iteration or session holds the lock; skipping"; return 0; }
  git pull --rebase --quiet origin main || true

  # 170 min + 5 min TERM->KILL grace = 175 min < the unit's 3h
  # (tests/test_autoloop_budget.py pins the sum against the unit file).
  BUDGET_S="${AUTOLOOP_BUDGET_S:-10200}"
  GRACE_S="${AUTOLOOP_GRACE_S:-300}"
  AUTOLOOP_DEADLINE=$(( $(date +%s) + BUDGET_S ))
  export AUTOLOOP_DEADLINE
  echo "[autoloop] budget ${BUDGET_S}s; deadline $(date -u -d "@${AUTOLOOP_DEADLINE}" +%H:%M:%SZ) (AUTOLOOP_DEADLINE=${AUTOLOOP_DEADLINE})"

  # The limit message is one line of claude's own output; `tee` keeps it
  # flowing to the journal while the file keeps it for the exit below.
  local capture="data/autoloop.last.out"
  : > "$capture"
  local rc=0
  timeout --signal=TERM --kill-after="$GRACE_S" "$BUDGET_S" \
    claude -p "$(cat scripts/autoloop-prompt.md)" \
    --dangerously-skip-permissions \
    --max-turns 120 2>&1 | tee "$capture" || rc=$?

  case "$rc" in
    0) return 0 ;;
    124) echo "[autoloop] budget ${BUDGET_S}s spent; bounded stop on SIGTERM (the timer is the next wakeup)"
         return 0 ;;
    137) echo "[autoloop] budget ${BUDGET_S}s spent and SIGTERM ignored for ${GRACE_S}s; SIGKILLed -- a finding, not a stop"
         return 137 ;;
  esac
  local limit_line
  if limit_line=$(grep -m1 -E "hit your (weekly|usage|session|daily|monthly) limit|usage limit" "$capture"); then
    echo "[autoloop] claude REFUSED (usage limit, a USER item; the timer is the retry): ${limit_line} -- exit 75 (EX_TEMPFAIL), not ${rc}"
    return 75
  fi
  return "$rc"
}

main "$@"; exit "$?"
