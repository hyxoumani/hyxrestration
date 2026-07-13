#!/usr/bin/env bash
# Stop-hook guard for the /autodev skill.
#
# While an autodev session is ACTIVE, this hook blocks the agent from ending
# its turn AND audits the experiment ledger and paths map for keep-working
# invariants. Each EXPERIMENTS.md/PATHS.md block (starting with a `## `
# header line) is parsed separately, taking only its OWN first `- status:`
# line — occurrences of the word "status:" elsewhere (prose, notes, quoted
# examples, code fences) are never counted. Mechanically checked:
#   - queue depth:  >= MIN_PROPOSED experiment blocks whose own status line
#                    is "status: proposed"
#   - dispatch:     exactly 1 experiment block whose own status line is
#                    "status: running" (0 and >1 are both violations)
#   - exploration:  PATHS.md has >= 1 avenue block whose own status line is
#                    "unexplored" or "active"
#   - novelty quota: once .autodev/dispatch_log has >= 5 lines, at least one
#                    of the LAST 5 lines must have field 4 (tab-separated:
#                    <iteration>\t<experiment-id>\t<avenue>\t
#                    <opened-unexplored: yes|no>) equal to "yes" — i.e. at
#                    least every 5th dispatch opens an unexplored avenue.
#                    The orchestrator is responsible for appending one line
#                    per dispatch (skill step 5, "Dispatch"); with fewer than
#                    5 lines logged so far, this check is skipped entirely
#                    (nothing to enforce yet).
# Violations are named explicitly in the block reason every iteration.
#
# Modes (.autodev/MODE):
#   bounded    - COMPLETE marker (written only after the skill's completion
#                gate, incl. red-team review) allows exit. Validity requires
#                BOTH the 3 literal COMPLETE markers AND a real, ledger-
#                tracked, `validated` `## RT-<N>` (`path: red-team-review`)
#                block in EXPERIMENTS.md — the literal RED_TEAM marker text
#                alone is never sufficient (a hand-written COMPLETE with no
#                actual red-team ledger entry is rejected as invalid).
#   continuous - COMPLETE is INVALID: the hook deletes it and blocks anyway.
#                The only exits are the user interrupting the session or an
#                explicitly user-requested /autodev stop (removes ACTIVE).
#
# There is deliberately NO iteration cap.

set -u

STATE_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}/.autodev"
MIN_PROPOSED=3

# No active session -> allow stopping normally.
if [[ ! -f "$STATE_DIR/ACTIVE" ]]; then
  exit 0
fi

MODE_RAW=$(cat "$STATE_DIR/MODE" 2>/dev/null)
# Trim leading/trailing whitespace (incl. trailing newline) and require an
# exact match — anything else (missing file, empty, garbled, extra text)
# is invalid and must NEVER be silently treated as "bounded".
MODE=$(printf '%s' "$MODE_RAW" | tr -d '[:space:]')

if [[ "$MODE" != "bounded" && "$MODE" != "continuous" ]]; then
  reason="AUTODEV ENFORCEMENT: MODE-INVALID: .autodev/MODE must contain exactly 'bounded' or 'continuous' (found: '${MODE_RAW}'). This is fail-closed — an invalid/missing/garbled MODE file is never treated as bounded. Fix MODE before this session can exit, and continue the loop."
  python3 - "$reason" <<'PYEOF' 2>/dev/null || printf '{"decision": "block", "reason": "AUTODEV ENFORCEMENT: MODE-INVALID: .autodev/MODE must be exactly bounded or continuous."}\n'
import json, sys
print(json.dumps({"decision": "block", "reason": sys.argv[1]}))
PYEOF
  exit 0
fi

# A well-formed completion attestation requires ALL of these labeled
# markers to be present in COMPLETE (grep-able, not just file existence) —
# see the /autodev skill's "Completion gate" section for what each attests.
# The literal RED_TEAM marker alone is NOT sufficient evidence a real
# red-team review happened, though: it also cross-checks EXPERIMENTS.md for
# an actual `## RT-<N>` ledger block (`path: red-team-review`, own status
# line `validated`) below.
required_markers=("VERIFICATION: PASS" "RED_TEAM: EMPTY_HANDED" "ACCEPTANCE_CRITERIA: MET")

complete_present=0
complete_valid=0
missing_markers=""
if [[ -f "$STATE_DIR/COMPLETE" ]]; then
  complete_present=1
  complete_valid=1
  for marker in "${required_markers[@]}"; do
    if ! grep -qF "$marker" "$STATE_DIR/COMPLETE" 2>/dev/null; then
      complete_valid=0
      missing_markers+="'${marker}' "
    fi
  done

  # Cross-check the ledger: require a real, ledger-tracked red-team review,
  # not just the literal marker text. A block qualifies only if its OWN
  # header matches `## RT-<N>`, its own first `- path:` line is exactly
  # `- path: red-team-review`, and its own first `- status:` line is
  # exactly `- status: validated` (empty-handed).
  rt_ledger_valid=$(awk '
    function block_ok() {
      return (header ~ /^## RT-[0-9]+/ && status == "- status: validated" && path == "- path: red-team-review")
    }
    /^## / {
      if (in_block && block_ok()) { found = 1 }
      in_block = 1; header = $0; status = ""; path = ""; next
    }
    in_block && status == "" && /^- status:/ { status = $0 }
    in_block && path == "" && /^- path:/ { path = $0 }
    END { if (in_block && block_ok()) { found = 1 }; print (found ? "yes" : "no") }
  ' "$STATE_DIR/EXPERIMENTS.md" 2>/dev/null)
  if [[ "$rt_ledger_valid" != "yes" ]]; then
    complete_valid=0
    missing_markers+="[no validated RT-<N> red-team-review ledger entry found in EXPERIMENTS.md — need a '## RT-<N>' block with its own '- path: red-team-review' and '- status: validated' lines] "
  fi
fi

if [[ "$complete_present" -eq 1 ]]; then
  if [[ "$MODE" == "continuous" ]]; then
    # Self-termination is mechanically impossible in continuous mode.
    rm -f "$STATE_DIR/COMPLETE"
    complete_violation="COMPLETE-INVALID: this session is MODE=continuous — it has no finish line and COMPLETE is never honored (the hook just deleted it; do not recreate it). The goal is a standing obligation; only the user can end this session. "
  elif [[ "$complete_valid" -eq 1 ]]; then
    # Bounded mode: completion gate passed -> allow stopping, retire ACTIVE.
    rm -f "$STATE_DIR/ACTIVE"
    if [[ -f "$STATE_DIR/ACTIVE" ]]; then
      reason="AUTODEV ENFORCEMENT: ACTIVE-REMOVAL-FAILED: rm -f .autodev/ACTIVE did not remove the file (permission or filesystem error). The session cannot be confirmed retired, so it may not exit. Investigate and retry."
      python3 - "$reason" <<'PYEOF' 2>/dev/null || printf '{"decision": "block", "reason": "AUTODEV ENFORCEMENT: ACTIVE-REMOVAL-FAILED: could not remove .autodev/ACTIVE."}\n'
import json, sys
print(json.dumps({"decision": "block", "reason": sys.argv[1]}))
PYEOF
      exit 0
    fi
    exit 0
  else
    # COMPLETE exists but is missing required markers -> treat as if it
    # didn't exist, and name exactly what's missing.
    complete_violation="COMPLETE-INVALID: .autodev/COMPLETE is missing required marker(s)/evidence: ${missing_markers}(need all of VERIFICATION: PASS, RED_TEAM: EMPTY_HANDED, ACCEPTANCE_CRITERIA: MET, PLUS a validated RT-<N> red-team-review ledger entry in EXPERIMENTS.md). A bare/incomplete COMPLETE file, or the 3 marker strings without a real ledger-tracked red-team review, is never honored — rewrite it per the completion gate in the /autodev skill, or continue the loop if the gate isn't actually satisfied yet. "
  fi
else
  complete_violation=""
fi

# Iteration counter (logging/telemetry only — never a ceiling).
COUNT_FILE="$STATE_DIR/iteration_count"
count=$(cat "$COUNT_FILE" 2>/dev/null || echo 0)
count=$((count + 1))
echo "$count" > "$COUNT_FILE"

# Parse a ledger/paths-map file into one line per top-level block (lines
# starting with "## "), each line being that block's own first
# "- status: ..." line (or nothing, if the block has none). Occurrences of
# "status:" elsewhere in the block (prose, notes, code fences) are ignored
# because only the FIRST match after each "## " header, before the next
# one, is taken.
block_statuses() {
  awk '
    /^## / { if (in_block && status != "") print status; in_block=1; status=""; next }
    in_block && status == "" && /^- status:/ { status=$0 }
    END { if (in_block && status != "") print status }
  ' "$1" 2>/dev/null
}

# ---- Ledger audit -----------------------------------------------------------
LEDGER="$STATE_DIR/EXPERIMENTS.md"
ledger_statuses=$(block_statuses "$LEDGER")
proposed=$(printf '%s\n' "$ledger_statuses" | grep -c 'status: proposed')
running=$(printf '%s\n' "$ledger_statuses" | grep -c 'status: running')

violations="$complete_violation"
if (( proposed < MIN_PROPOSED )); then
  violations+="QUEUE-VIOLATION: only $proposed 'status: proposed' experiments in EXPERIMENTS.md (minimum $MIN_PROPOSED). Refill the queue NOW with new, unblocked hypotheses — consult PATHS.md for unexplored avenues; wall-clock-blocked ideas must be 'status: deferred' and do not count. "
fi
if (( running == 0 )); then
  violations+="DISPATCH-VIOLATION: no experiment has 'status: running' (expected exactly 1). Pick the highest-expected-value proposal, mark it running, and dispatch an autodev-agent NOW. "
elif (( running > 1 )); then
  violations+="DISPATCH-VIOLATION: $running experiments running simultaneously, expected exactly 1 (sequential mode). Demote all but one back to 'status: proposed' or conclude them before continuing. "
fi

# ---- Exploration audit ------------------------------------------------------
PATHS_FILE="$STATE_DIR/PATHS.md"
paths_statuses=$(block_statuses "$PATHS_FILE")
if ! printf '%s\n' "$paths_statuses" | grep -qE 'status: (unexplored|active)'; then
  violations+="EXPLORATION-VIOLATION: PATHS.md has no avenue with 'status: unexplored' or 'status: active'. Open a new avenue now — 'waiting for time/data' is never a reason to have zero open avenues. "
fi

# ---- Novelty-quota audit (dispatch-history-backed half of exploration) -----
DISPATCH_LOG="$STATE_DIR/dispatch_log"
dispatch_line_count=0
if [[ -f "$DISPATCH_LOG" ]]; then
  dispatch_line_count=$(grep -c '' "$DISPATCH_LOG" 2>/dev/null || echo 0)
fi
if (( dispatch_line_count >= 5 )); then
  last5=$(tail -n 5 "$DISPATCH_LOG")
  if ! printf '%s\n' "$last5" | grep -qE $'opened-unexplored:[[:space:]]*yes'; then
    violations+="NOVELTY-QUOTA-VIOLATION: none of the last 5 dispatch_log entries opened an 'unexplored' avenue (need >= 1 in every 5). Your next dispatch MUST target an unexplored PATHS.md avenue, and remember to append its dispatch_log line. "
  fi
fi

if [[ -n "$violations" ]]; then
  directive="Fix the named violations this iteration, then continue the loop."
else
  directive="Invariants hold. Continue the loop: evaluate any returned results (keep/revert with evidence, file a library brief), refresh PATHS.md, propose, and keep exactly one experiment running."
fi

reason="AUTODEV ENFORCEMENT (iteration $count, mode $MODE): session ACTIVE — you may not stop. ${violations}${directive} Always be investigating: waiting on time or data is never a reason to idle; generating new hypotheses is itself the job. Never weaken GOAL.md, never fake ledger statuses to satisfy this audit."

python3 - "$reason" <<'PYEOF' 2>/dev/null || printf '{"decision": "block", "reason": "AUTODEV ENFORCEMENT: session ACTIVE and not complete. Continue the loop per the autodev skill."}\n'
import json, sys
print(json.dumps({"decision": "block", "reason": sys.argv[1]}))
PYEOF
exit 0
