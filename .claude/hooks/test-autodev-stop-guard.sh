#!/usr/bin/env bash
# Regression test harness for autodev-stop-guard.sh.
#
# Builds a series of synthetic .autodev-like directories under a private
# temp root, runs the REAL hook script against each one (with
# CLAUDE_PROJECT_DIR pointed at the synthetic dir, never at this repo),
# and asserts the resulting stdout (JSON decision, or empty on allow) and
# exit code match expectations.
#
# Usage:  .claude/hooks/test-autodev-stop-guard.sh
# Exit code: 0 if all scenarios pass, 1 if any scenario fails.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK="$SCRIPT_DIR/autodev-stop-guard.sh"

if [[ ! -x "$HOOK" && ! -f "$HOOK" ]]; then
  echo "FATAL: hook not found at $HOOK" >&2
  exit 1
fi

TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/autodev-stop-guard-test.XXXXXX")
trap 'rm -rf "$TMP_ROOT"' EXIT

pass_count=0
fail_count=0
declare -a failures=()

# ---- scenario scaffolding ---------------------------------------------------

# new_scenario_dir NAME -> prints path to a fresh synthetic project dir
new_scenario_dir() {
  local name="$1"
  local dir="$TMP_ROOT/$name"
  mkdir -p "$dir/.autodev"
  printf '%s' "$dir"
}

# run_hook DIR -> sets globals HOOK_STDOUT, HOOK_EXIT
run_hook() {
  local dir="$1"
  HOOK_STDOUT=$(CLAUDE_PROJECT_DIR="$dir" bash "$HOOK" 2>"$TMP_ROOT/last_stderr")
  HOOK_EXIT=$?
}

# assert_eq LABEL EXPECTED ACTUAL
assert_eq() {
  local label="$1" expected="$2" actual="$3"
  if [[ "$expected" != "$actual" ]]; then
    echo "    FAIL detail: $label expected [$expected] got [$actual]"
    return 1
  fi
  return 0
}

# assert_contains LABEL HAYSTACK NEEDLE
assert_contains() {
  local label="$1" haystack="$2" needle="$3"
  if [[ "$haystack" != *"$needle"* ]]; then
    echo "    FAIL detail: $label expected to contain [$needle], got: $haystack"
    return 1
  fi
  return 0
}

# assert_not_contains LABEL HAYSTACK NEEDLE
assert_not_contains() {
  local label="$1" haystack="$2" needle="$3"
  if [[ "$haystack" == *"$needle"* ]]; then
    echo "    FAIL detail: $label expected NOT to contain [$needle], got: $haystack"
    return 1
  fi
  return 0
}

report() {
  local name="$1" ok="$2"
  if [[ "$ok" -eq 0 ]]; then
    echo "PASS: $name"
    pass_count=$((pass_count + 1))
  else
    echo "FAIL: $name"
    fail_count=$((fail_count + 1))
    failures+=("$name")
  fi
}

# A canonical ledger with N proposed blocks and one running block whose
# status is 'running'. Extra blocks may be appended via $2.
canonical_ledger() {
  local proposed_count="$1"
  local extra="${2:-}"
  local out=""
  local i
  for ((i = 1; i <= proposed_count; i++)); do
    out+="## Experiment P$i: placeholder hypothesis $i
- status: proposed
Some prose describing the hypothesis.

"
  done
  out+="## Experiment R1: the one running experiment
- status: running
In progress, dispatched to an autodev-agent.

"
  out+="$extra"
  printf '%s' "$out"
}

canonical_paths() {
  cat <<'EOF'
## Avenue A: some active avenue
- status: active
Notes about this avenue.

## Avenue B: an exhausted avenue
- status: exhausted
Notes.
EOF
}

exhausted_paths() {
  cat <<'EOF'
## Avenue A: fully exhausted
- status: exhausted
Nothing left here.

## Avenue B: also exhausted
- status: exhausted
Nothing left here either.
EOF
}

# ---- scenario 1: no ACTIVE -> exit 0, silent -------------------------------
{
  dir=$(new_scenario_dir "01-no-active")
  rm -f "$dir/.autodev/ACTIVE"
  run_hook "$dir"
  ok=0
  assert_eq "exit code" "0" "$HOOK_EXIT" || ok=1
  assert_eq "stdout" "" "$HOOK_STDOUT" || ok=1
  report "no ACTIVE present -> silent allow" "$ok"
}

# ---- scenario 2: ACTIVE present, MODE missing ------------------------------
{
  dir=$(new_scenario_dir "02-mode-missing")
  touch "$dir/.autodev/ACTIVE"
  rm -f "$dir/.autodev/MODE"
  run_hook "$dir"
  ok=0
  assert_eq "exit code" "0" "$HOOK_EXIT" || ok=1
  assert_contains "reason" "$HOOK_STDOUT" "MODE-INVALID" || ok=1
  report "MODE missing -> MODE-INVALID block" "$ok"
}

# ---- scenario 3a: MODE = garbage wrong-case --------------------------------
{
  dir=$(new_scenario_dir "03a-mode-wrong-case")
  touch "$dir/.autodev/ACTIVE"
  printf 'Bounded' > "$dir/.autodev/MODE"
  run_hook "$dir"
  ok=0
  assert_eq "exit code" "0" "$HOOK_EXIT" || ok=1
  assert_contains "reason" "$HOOK_STDOUT" "MODE-INVALID" || ok=1
  report "MODE='Bounded' (wrong case) -> MODE-INVALID" "$ok"
}

# ---- scenario 3b: MODE = garbage with extra trailing content ---------------
{
  dir=$(new_scenario_dir "03b-mode-extra-text")
  touch "$dir/.autodev/ACTIVE"
  printf 'bounded\nextra' > "$dir/.autodev/MODE"
  run_hook "$dir"
  ok=0
  assert_eq "exit code" "0" "$HOOK_EXIT" || ok=1
  assert_contains "reason" "$HOOK_STDOUT" "MODE-INVALID" || ok=1
  report "MODE='bounded\\nextra' -> MODE-INVALID" "$ok"
}

# ---- scenario 4: canonical healthy bounded state -> Invariants hold -------
{
  dir=$(new_scenario_dir "04-healthy-bounded")
  touch "$dir/.autodev/ACTIVE"
  printf 'bounded' > "$dir/.autodev/MODE"
  canonical_ledger 3 > "$dir/.autodev/EXPERIMENTS.md"
  canonical_paths > "$dir/.autodev/PATHS.md"
  date +%s > "$dir/.autodev/RUNNING_SINCE"
  run_hook "$dir"
  ok=0
  assert_eq "exit code" "0" "$HOOK_EXIT" || ok=1
  # Invariants hold AND RUNNING_SINCE is fresh -> quiet-wait silent allow,
  # not a "keep going" nudge (the busywork this exception exists to avoid).
  assert_eq "stdout" "" "$HOOK_STDOUT" || ok=1
  report "3 proposed + 1 running + 1 active avenue + fresh RUNNING_SINCE -> silent quiet-wait allow" "$ok"
}

# ---- scenario 4-stale-missing: invariants hold, RUNNING_SINCE absent -----
{
  dir=$(new_scenario_dir "04-stale-missing-running-since")
  touch "$dir/.autodev/ACTIVE"
  printf 'bounded' > "$dir/.autodev/MODE"
  canonical_ledger 3 > "$dir/.autodev/EXPERIMENTS.md"
  canonical_paths > "$dir/.autodev/PATHS.md"
  # No RUNNING_SINCE written at all.
  run_hook "$dir"
  ok=0
  assert_eq "exit code" "0" "$HOOK_EXIT" || ok=1
  assert_contains "reason" "$HOOK_STDOUT" "STALE-DISPATCH-CHECK" || ok=1
  report "invariants hold but RUNNING_SINCE missing -> STALE-DISPATCH-CHECK, not silent" "$ok"
}

# ---- scenario 4-stale-old: invariants hold, RUNNING_SINCE > 30 min old ----
{
  dir=$(new_scenario_dir "04-stale-old-running-since")
  touch "$dir/.autodev/ACTIVE"
  printf 'bounded' > "$dir/.autodev/MODE"
  canonical_ledger 3 > "$dir/.autodev/EXPERIMENTS.md"
  canonical_paths > "$dir/.autodev/PATHS.md"
  echo $(( $(date +%s) - 3600 )) > "$dir/.autodev/RUNNING_SINCE"
  run_hook "$dir"
  ok=0
  assert_eq "exit code" "0" "$HOOK_EXIT" || ok=1
  assert_contains "reason" "$HOOK_STDOUT" "STALE-DISPATCH-CHECK" || ok=1
  report "invariants hold but RUNNING_SINCE is 1 hour old -> STALE-DISPATCH-CHECK" "$ok"
}

# ---- scenario 4-stale-garbage: RUNNING_SINCE contains non-numeric junk ----
{
  dir=$(new_scenario_dir "04-stale-garbage-running-since")
  touch "$dir/.autodev/ACTIVE"
  printf 'bounded' > "$dir/.autodev/MODE"
  canonical_ledger 3 > "$dir/.autodev/EXPERIMENTS.md"
  canonical_paths > "$dir/.autodev/PATHS.md"
  printf 'not-a-timestamp' > "$dir/.autodev/RUNNING_SINCE"
  run_hook "$dir"
  ok=0
  assert_eq "exit code" "0" "$HOOK_EXIT" || ok=1
  assert_contains "reason" "$HOOK_STDOUT" "STALE-DISPATCH-CHECK" || ok=1
  report "RUNNING_SINCE contains non-numeric garbage -> treated as missing, STALE-DISPATCH-CHECK" "$ok"
}

# ---- scenario 4a: exactly 2 proposed (below MIN_PROPOSED=3) -> QUEUE-VIOLATION
# Boundary case: running=1 and PATHS.md valid held constant vs scenario 4,
# only the proposed-count changes (3 -> 2), to pin down the exact cutover.
{
  dir=$(new_scenario_dir "04a-queue-boundary-2-proposed")
  touch "$dir/.autodev/ACTIVE"
  printf 'bounded' > "$dir/.autodev/MODE"
  canonical_ledger 2 > "$dir/.autodev/EXPERIMENTS.md"
  canonical_paths > "$dir/.autodev/PATHS.md"
  run_hook "$dir"
  ok=0
  assert_eq "exit code" "0" "$HOOK_EXIT" || ok=1
  assert_contains "reason" "$HOOK_STDOUT" "QUEUE-VIOLATION" || ok=1
  assert_contains "reason" "$HOOK_STDOUT" "only 2 'status: proposed' experiments" || ok=1
  report "exactly 2 proposed (< MIN_PROPOSED) -> QUEUE-VIOLATION" "$ok"
}

# ---- scenario 4b: exactly 3 proposed (== MIN_PROPOSED) -> no QUEUE-VIOLATION
# Same setup as 4a but with proposed=3, the precise cutover point: nothing
# between 2 and 3 exists to test, so this pair pins down < vs <= exactly.
{
  dir=$(new_scenario_dir "04b-queue-boundary-3-proposed")
  touch "$dir/.autodev/ACTIVE"
  printf 'bounded' > "$dir/.autodev/MODE"
  canonical_ledger 3 > "$dir/.autodev/EXPERIMENTS.md"
  canonical_paths > "$dir/.autodev/PATHS.md"
  date +%s > "$dir/.autodev/RUNNING_SINCE"
  run_hook "$dir"
  ok=0
  assert_eq "exit code" "0" "$HOOK_EXIT" || ok=1
  assert_not_contains "reason" "$HOOK_STDOUT" "QUEUE-VIOLATION" || ok=1
  assert_eq "stdout" "" "$HOOK_STDOUT" || ok=1
  report "exactly 3 proposed (== MIN_PROPOSED) -> no QUEUE-VIOLATION (silent quiet-wait)" "$ok"
}

# ---- scenario 5: anchoring — own status wins over prose substring ---------
{
  dir=$(new_scenario_dir "05-anchoring")
  touch "$dir/.autodev/ACTIVE"
  printf 'bounded' > "$dir/.autodev/MODE"
  extra="## Experiment Q1: a proposed block with a misleading note
- status: proposed
Note: an earlier draft of this file mistakenly said 'status: running' here,
but that was corrected; the block's real, own status line above is proposed.

"
  canonical_ledger 3 "$extra" > "$dir/.autodev/EXPERIMENTS.md"
  canonical_paths > "$dir/.autodev/PATHS.md"
  date +%s > "$dir/.autodev/RUNNING_SINCE"
  run_hook "$dir"
  ok=0
  assert_eq "exit code" "0" "$HOOK_EXIT" || ok=1
  # Exactly one real running block (R1) exists; the anchoring prose in Q1
  # must NOT be double-counted as a second running block, so this must
  # still read as healthy (silent quiet-wait) with no DISPATCH-VIOLATION.
  assert_eq "stdout" "" "$HOOK_STDOUT" || ok=1
  report "prose containing 'status: running' inside a proposed block is not counted as running" "$ok"
}

# ---- scenario 6: 2 blocks genuinely running -> over-dispatch violation ----
{
  dir=$(new_scenario_dir "06-over-dispatch")
  touch "$dir/.autodev/ACTIVE"
  printf 'bounded' > "$dir/.autodev/MODE"
  extra="## Experiment R2: a second running experiment
- status: running
Also in progress, dispatched simultaneously (this should never happen).

"
  canonical_ledger 3 "$extra" > "$dir/.autodev/EXPERIMENTS.md"
  canonical_paths > "$dir/.autodev/PATHS.md"
  run_hook "$dir"
  ok=0
  assert_eq "exit code" "0" "$HOOK_EXIT" || ok=1
  assert_contains "reason" "$HOOK_STDOUT" "DISPATCH-VIOLATION" || ok=1
  assert_contains "reason" "$HOOK_STDOUT" "2 experiments running simultaneously" || ok=1
  report "2 blocks running -> DISPATCH-VIOLATION (over-dispatch, distinct wording)" "$ok"
}

# ---- scenario 7: 0 blocks running -> under-dispatch violation ------------
{
  dir=$(new_scenario_dir "07-under-dispatch")
  touch "$dir/.autodev/ACTIVE"
  printf 'bounded' > "$dir/.autodev/MODE"
  cat > "$dir/.autodev/EXPERIMENTS.md" <<'EOF'
## Experiment P1: placeholder
- status: proposed
Prose.

## Experiment P2: placeholder
- status: proposed
Prose.

## Experiment P3: placeholder
- status: proposed
Prose.
EOF
  canonical_paths > "$dir/.autodev/PATHS.md"
  run_hook "$dir"
  ok=0
  assert_eq "exit code" "0" "$HOOK_EXIT" || ok=1
  assert_contains "reason" "$HOOK_STDOUT" "DISPATCH-VIOLATION" || ok=1
  assert_contains "reason" "$HOOK_STDOUT" "no experiment has 'status: running'" || ok=1
  report "0 blocks running -> DISPATCH-VIOLATION (under-dispatch, distinct wording)" "$ok"
}

# ---- scenario 8: PATHS.md fully exhausted -> EXPLORATION-VIOLATION -------
{
  dir=$(new_scenario_dir "08-exploration-violation")
  touch "$dir/.autodev/ACTIVE"
  printf 'bounded' > "$dir/.autodev/MODE"
  canonical_ledger 3 > "$dir/.autodev/EXPERIMENTS.md"
  exhausted_paths > "$dir/.autodev/PATHS.md"
  run_hook "$dir"
  ok=0
  assert_eq "exit code" "0" "$HOOK_EXIT" || ok=1
  assert_contains "reason" "$HOOK_STDOUT" "EXPLORATION-VIOLATION" || ok=1
  report "PATHS.md has no unexplored/active avenue -> EXPLORATION-VIOLATION" "$ok"
}

# ---- scenario 9: COMPLETE present but missing markers ---------------------
{
  dir=$(new_scenario_dir "09-complete-missing-markers")
  touch "$dir/.autodev/ACTIVE"
  printf 'bounded' > "$dir/.autodev/MODE"
  canonical_ledger 3 > "$dir/.autodev/EXPERIMENTS.md"
  canonical_paths > "$dir/.autodev/PATHS.md"
  cat > "$dir/.autodev/COMPLETE" <<'EOF'
VERIFICATION: PASS
ACCEPTANCE_CRITERIA: MET
EOF
  run_hook "$dir"
  ok=0
  assert_eq "exit code" "0" "$HOOK_EXIT" || ok=1
  assert_contains "reason" "$HOOK_STDOUT" "COMPLETE-INVALID" || ok=1
  assert_contains "reason" "$HOOK_STDOUT" "RED_TEAM: EMPTY_HANDED" || ok=1
  report "COMPLETE missing RED_TEAM marker -> COMPLETE-INVALID naming it" "$ok"
}

# A minimal EXPERIMENTS.md fragment representing a genuinely validated,
# empty-handed RT-<N> red-team review block, in canonical field order.
validated_rt_block() {
  cat <<'EOF'
## RT-1: red-team review of completion claim
- status: validated
- path: red-team-review
- outcome: empty-handed — no unblocked positive-EV experiment found.

EOF
}

# ---- scenario 9b: COMPLETE has all 3 markers but NO RT-<N> entry at all ---
{
  dir=$(new_scenario_dir "09b-complete-no-rt-entry")
  touch "$dir/.autodev/ACTIVE"
  printf 'bounded' > "$dir/.autodev/MODE"
  canonical_ledger 3 > "$dir/.autodev/EXPERIMENTS.md"
  canonical_paths > "$dir/.autodev/PATHS.md"
  cat > "$dir/.autodev/COMPLETE" <<'EOF'
VERIFICATION: PASS
RED_TEAM: EMPTY_HANDED
ACCEPTANCE_CRITERIA: MET
EOF
  run_hook "$dir"
  ok=0
  assert_eq "exit code" "0" "$HOOK_EXIT" || ok=1
  assert_contains "reason" "$HOOK_STDOUT" "COMPLETE-INVALID" || ok=1
  assert_contains "reason" "$HOOK_STDOUT" "no validated RT-<N> red-team-review ledger entry" || ok=1
  if [[ ! -f "$dir/.autodev/ACTIVE" ]]; then
    echo "    FAIL detail: ACTIVE was removed even though no RT-<N> ledger entry exists"
    ok=1
  fi
  report "COMPLETE with all 3 markers but no RT-<N> ledger entry at all -> COMPLETE-INVALID" "$ok"
}

# ---- scenario 9c: COMPLETE has all 3 markers, RT-<N> entry still running --
{
  dir=$(new_scenario_dir "09c-complete-rt-still-running")
  touch "$dir/.autodev/ACTIVE"
  printf 'bounded' > "$dir/.autodev/MODE"
  extra="## RT-1: red-team review of completion claim
- status: running
- path: red-team-review
Dispatched to an autodev-agent, not yet evaluated.

"
  canonical_ledger 3 "$extra" > "$dir/.autodev/EXPERIMENTS.md"
  canonical_paths > "$dir/.autodev/PATHS.md"
  cat > "$dir/.autodev/COMPLETE" <<'EOF'
VERIFICATION: PASS
RED_TEAM: EMPTY_HANDED
ACCEPTANCE_CRITERIA: MET
EOF
  run_hook "$dir"
  ok=0
  assert_eq "exit code" "0" "$HOOK_EXIT" || ok=1
  assert_contains "reason" "$HOOK_STDOUT" "COMPLETE-INVALID" || ok=1
  assert_contains "reason" "$HOOK_STDOUT" "no validated RT-<N> red-team-review ledger entry" || ok=1
  # This also causes 2 blocks running (R1 + RT-1) simultaneously, which is
  # itself a legitimate DISPATCH-VIOLATION and does not undermine the point:
  # COMPLETE must still be rejected regardless.
  if [[ ! -f "$dir/.autodev/ACTIVE" ]]; then
    echo "    FAIL detail: ACTIVE was removed even though RT-<N> entry is still running"
    ok=1
  fi
  report "COMPLETE with all 3 markers, RT-<N> entry still 'status: running' -> COMPLETE-INVALID" "$ok"
}

# ---- scenario 10: valid COMPLETE, bounded -> exit 0, ACTIVE removed -------
{
  dir=$(new_scenario_dir "10-valid-complete-bounded")
  touch "$dir/.autodev/ACTIVE"
  printf 'bounded' > "$dir/.autodev/MODE"
  validated_rt_block > "$dir/.autodev/EXPERIMENTS.md"
  cat > "$dir/.autodev/COMPLETE" <<'EOF'
VERIFICATION: PASS
RED_TEAM: EMPTY_HANDED
ACCEPTANCE_CRITERIA: MET
EOF
  run_hook "$dir"
  ok=0
  assert_eq "exit code" "0" "$HOOK_EXIT" || ok=1
  assert_eq "stdout" "" "$HOOK_STDOUT" || ok=1
  if [[ -f "$dir/.autodev/ACTIVE" ]]; then
    echo "    FAIL detail: ACTIVE still present after valid bounded COMPLETE"
    ok=1
  fi
  report "valid COMPLETE + bounded + validated RT-<N> ledger entry -> silent exit 0, ACTIVE actually removed" "$ok"
}

# ---- scenario 10b: valid COMPLETE, bounded, RT-<N> field order varied ----
# path: before status: (field order should not matter -- only each field's
# OWN first occurrence within the block, order-independent).
{
  dir=$(new_scenario_dir "10b-valid-complete-rt-field-order")
  touch "$dir/.autodev/ACTIVE"
  printf 'bounded' > "$dir/.autodev/MODE"
  cat > "$dir/.autodev/EXPERIMENTS.md" <<'EOF'
## RT-2: red-team review of completion claim
- path: red-team-review
- status: validated
- outcome: empty-handed — no unblocked positive-EV experiment found.
EOF
  cat > "$dir/.autodev/COMPLETE" <<'EOF'
VERIFICATION: PASS
RED_TEAM: EMPTY_HANDED
ACCEPTANCE_CRITERIA: MET
EOF
  run_hook "$dir"
  ok=0
  assert_eq "exit code" "0" "$HOOK_EXIT" || ok=1
  assert_eq "stdout" "" "$HOOK_STDOUT" || ok=1
  if [[ -f "$dir/.autodev/ACTIVE" ]]; then
    echo "    FAIL detail: ACTIVE still present after valid bounded COMPLETE (path-before-status order)"
    ok=1
  fi
  report "valid COMPLETE + RT-<N> with path-before-status field order -> still recognized as valid" "$ok"
}

# ---- scenario 10c: stale earlier-validated RT-<N>, newer RT-<N> not validated
# The exact CodeRabbit-flagged gap: RT-1 was validated, but a newer RT-2 was
# later dispatched (e.g. because new experiments/avenues were added) and is
# still running/unresolved. RT-1 being validated must NOT satisfy the gate;
# only the highest-numbered block (RT-2) counts.
{
  dir=$(new_scenario_dir "10c-stale-earlier-validated-rt")
  touch "$dir/.autodev/ACTIVE"
  printf 'bounded' > "$dir/.autodev/MODE"
  cat > "$dir/.autodev/EXPERIMENTS.md" <<'EOF'
## RT-1: red-team review of completion claim
- status: validated
- path: red-team-review
- outcome: empty-handed on the state as of that iteration.

## RT-2: red-team review of completion claim
- status: running
- path: red-team-review
- outcome:
EOF
  cat > "$dir/.autodev/COMPLETE" <<'EOF'
VERIFICATION: PASS
RED_TEAM: EMPTY_HANDED
ACCEPTANCE_CRITERIA: MET
EOF
  run_hook "$dir"
  ok=0
  assert_eq "exit code" "0" "$HOOK_EXIT" || ok=1
  assert_contains "reason" "$HOOK_STDOUT" "COMPLETE-INVALID" || ok=1
  assert_contains "reason" "$HOOK_STDOUT" "highest-numbered RT-<N>" || ok=1
  if [[ ! -f "$dir/.autodev/ACTIVE" ]]; then
    echo "    FAIL detail: ACTIVE was removed even though the highest-numbered RT-<N> (RT-2) is not validated"
    ok=1
  fi
  report "stale validated RT-1 does not satisfy gate when newer RT-2 exists and isn't validated -> COMPLETE-INVALID" "$ok"
}

# ---- scenario 10d: newest RT-<N> is the validated one -> valid ------------
# Same shape as 10c but RT-2 (the higher number) is the validated one; RT-1's
# earlier status (rejected) is irrelevant since only the highest counts.
{
  dir=$(new_scenario_dir "10d-newest-rt-validated")
  touch "$dir/.autodev/ACTIVE"
  printf 'bounded' > "$dir/.autodev/MODE"
  cat > "$dir/.autodev/EXPERIMENTS.md" <<'EOF'
## RT-1: red-team review of completion claim
- status: rejected
- path: red-team-review
- outcome: found unblocked experiments; added to queue.

## RT-2: red-team review of completion claim
- status: validated
- path: red-team-review
- outcome: empty-handed — no unblocked positive-EV experiment found.
EOF
  cat > "$dir/.autodev/COMPLETE" <<'EOF'
VERIFICATION: PASS
RED_TEAM: EMPTY_HANDED
ACCEPTANCE_CRITERIA: MET
EOF
  run_hook "$dir"
  ok=0
  assert_eq "exit code" "0" "$HOOK_EXIT" || ok=1
  assert_eq "stdout" "" "$HOOK_STDOUT" || ok=1
  if [[ -f "$dir/.autodev/ACTIVE" ]]; then
    echo "    FAIL detail: ACTIVE still present even though highest-numbered RT-2 is validated"
    ok=1
  fi
  report "highest-numbered RT-2 validated (RT-1 rejected) -> valid, ACTIVE removed" "$ok"
}

# ---- scenario 11: valid COMPLETE, continuous -> blocked, COMPLETE deleted -
{
  dir=$(new_scenario_dir "11-valid-complete-continuous")
  touch "$dir/.autodev/ACTIVE"
  printf 'continuous' > "$dir/.autodev/MODE"
  canonical_ledger 3 > "$dir/.autodev/EXPERIMENTS.md"
  canonical_paths > "$dir/.autodev/PATHS.md"
  cat > "$dir/.autodev/COMPLETE" <<'EOF'
VERIFICATION: PASS
RED_TEAM: EMPTY_HANDED
ACCEPTANCE_CRITERIA: MET
EOF
  run_hook "$dir"
  ok=0
  assert_eq "exit code" "0" "$HOOK_EXIT" || ok=1
  assert_contains "reason" "$HOOK_STDOUT" "COMPLETE-INVALID" || ok=1
  assert_contains "reason" "$HOOK_STDOUT" "continuous" || ok=1
  if [[ -f "$dir/.autodev/COMPLETE" ]]; then
    echo "    FAIL detail: COMPLETE still present after continuous-mode rejection"
    ok=1
  fi
  if [[ ! -f "$dir/.autodev/ACTIVE" ]]; then
    echo "    FAIL detail: ACTIVE was removed even though continuous mode never exits"
    ok=1
  fi
  report "valid COMPLETE + continuous -> COMPLETE-INVALID, COMPLETE deleted, session stays ACTIVE" "$ok"
}

# ---- scenario 12: dispatch_log with < 5 lines, none opened unexplored -----
# (nothing enforced yet regardless of content)
{
  dir=$(new_scenario_dir "12-dispatch-log-under-5")
  touch "$dir/.autodev/ACTIVE"
  printf 'bounded' > "$dir/.autodev/MODE"
  canonical_ledger 3 > "$dir/.autodev/EXPERIMENTS.md"
  canonical_paths > "$dir/.autodev/PATHS.md"
  printf '1\tEXP-001\tAvenue A\topened-unexplored: no\n2\tEXP-002\tAvenue A\topened-unexplored: no\n3\tEXP-003\tAvenue B\topened-unexplored: no\n4\tEXP-004\tAvenue B\topened-unexplored: no\n' > "$dir/.autodev/dispatch_log"
  date +%s > "$dir/.autodev/RUNNING_SINCE"
  run_hook "$dir"
  ok=0
  assert_eq "exit code" "0" "$HOOK_EXIT" || ok=1
  assert_eq "stdout" "" "$HOOK_STDOUT" || ok=1
  report "dispatch_log with 4 lines, none opened unexplored -> not enforced yet (silent quiet-wait)" "$ok"
}

# ---- scenario 13: dispatch_log with exactly 5 lines, none opened unexplored
{
  dir=$(new_scenario_dir "13-dispatch-log-5-none")
  touch "$dir/.autodev/ACTIVE"
  printf 'bounded' > "$dir/.autodev/MODE"
  canonical_ledger 3 > "$dir/.autodev/EXPERIMENTS.md"
  canonical_paths > "$dir/.autodev/PATHS.md"
  printf '1\tEXP-001\tAvenue A\topened-unexplored: no\n2\tEXP-002\tAvenue A\topened-unexplored: no\n3\tEXP-003\tAvenue B\topened-unexplored: no\n4\tEXP-004\tAvenue B\topened-unexplored: no\n5\tEXP-005\tAvenue B\topened-unexplored: no\n' > "$dir/.autodev/dispatch_log"
  run_hook "$dir"
  ok=0
  assert_eq "exit code" "0" "$HOOK_EXIT" || ok=1
  assert_contains "reason" "$HOOK_STDOUT" "NOVELTY-QUOTA-VIOLATION" || ok=1
  report "dispatch_log with 5 lines, none opened unexplored -> NOVELTY-QUOTA-VIOLATION" "$ok"
}

# ---- scenario 14: dispatch_log with exactly 5 lines, exactly one opened ---
{
  dir=$(new_scenario_dir "14-dispatch-log-5-one")
  touch "$dir/.autodev/ACTIVE"
  printf 'bounded' > "$dir/.autodev/MODE"
  canonical_ledger 3 > "$dir/.autodev/EXPERIMENTS.md"
  canonical_paths > "$dir/.autodev/PATHS.md"
  printf '1\tEXP-001\tAvenue A\topened-unexplored: no\n2\tEXP-002\tAvenue A\topened-unexplored: no\n3\tEXP-003\tAvenue B\topened-unexplored: yes\n4\tEXP-004\tAvenue B\topened-unexplored: no\n5\tEXP-005\tAvenue B\topened-unexplored: no\n' > "$dir/.autodev/dispatch_log"
  date +%s > "$dir/.autodev/RUNNING_SINCE"
  run_hook "$dir"
  ok=0
  assert_eq "exit code" "0" "$HOOK_EXIT" || ok=1
  assert_eq "stdout" "" "$HOOK_STDOUT" || ok=1
  report "dispatch_log with 5 lines, one opened unexplored -> no violation (silent quiet-wait)" "$ok"
}

# ---- scenario 15: > 5 lines, only last 5 matter (old violation ignored) ---
{
  dir=$(new_scenario_dir "15-dispatch-log-only-last-5")
  touch "$dir/.autodev/ACTIVE"
  printf 'bounded' > "$dir/.autodev/MODE"
  canonical_ledger 3 > "$dir/.autodev/EXPERIMENTS.md"
  canonical_paths > "$dir/.autodev/PATHS.md"
  # First 3 lines (part of the older history) have zero "yes" among them,
  # but they fall outside the last-5 window, so they must not cause a
  # false violation as long as the last 5 contain at least one "yes".
  printf '1\tEXP-001\tAvenue A\topened-unexplored: no\n2\tEXP-002\tAvenue A\topened-unexplored: no\n3\tEXP-003\tAvenue A\topened-unexplored: no\n4\tEXP-004\tAvenue B\topened-unexplored: no\n5\tEXP-005\tAvenue B\topened-unexplored: no\n6\tEXP-006\tAvenue B\topened-unexplored: yes\n7\tEXP-007\tAvenue B\topened-unexplored: no\n8\tEXP-008\tAvenue B\topened-unexplored: no\n' > "$dir/.autodev/dispatch_log"
  date +%s > "$dir/.autodev/RUNNING_SINCE"
  run_hook "$dir"
  ok=0
  assert_eq "exit code" "0" "$HOOK_EXIT" || ok=1
  assert_eq "stdout" "" "$HOOK_STDOUT" || ok=1
  report "dispatch_log with 8 lines, only last 5 evaluated (one 'yes' at line 6) -> no violation" "$ok"
}

# ---- summary ----------------------------------------------------------------
total=$((pass_count + fail_count))
echo ""
echo "===================================================="
echo "$pass_count/$total passed"
if [[ "$fail_count" -gt 0 ]]; then
  echo "FAILED scenarios:"
  for f in "${failures[@]}"; do
    echo "  - $f"
  done
  exit 1
fi
exit 0
