#!/usr/bin/env bash
# Stop-hook guard for the /autodev skill.
#
# While an autodev session is ACTIVE, this hook blocks the agent from ending
# its turn AND audits the experiment ledger for keep-working invariants:
#   - queue depth:  >= MIN_PROPOSED experiments with "status: proposed"
#   - dispatch:     >= 1 experiment with "status: running"
# Violations are named explicitly in the block reason every iteration.
#
# Modes (.autodev/MODE):
#   bounded    - COMPLETE marker (written only after the skill's completion
#                gate, incl. red-team review) allows exit.
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

MODE=$(cat "$STATE_DIR/MODE" 2>/dev/null || echo bounded)

if [[ -f "$STATE_DIR/COMPLETE" ]]; then
  if [[ "$MODE" == "continuous" ]]; then
    # Self-termination is mechanically impossible in continuous mode.
    rm -f "$STATE_DIR/COMPLETE"
    complete_violation="COMPLETE-INVALID: this session is MODE=continuous — it has no finish line and COMPLETE is never honored (the hook just deleted it; do not recreate it). The goal is a standing obligation; only the user can end this session. "
  else
    # Bounded mode: completion gate passed -> allow stopping, retire ACTIVE.
    rm -f "$STATE_DIR/ACTIVE"
    exit 0
  fi
else
  complete_violation=""
fi

# Iteration counter (logging/telemetry only — never a ceiling).
COUNT_FILE="$STATE_DIR/iteration_count"
count=$(cat "$COUNT_FILE" 2>/dev/null || echo 0)
count=$((count + 1))
echo "$count" > "$COUNT_FILE"

# ---- Ledger audit -----------------------------------------------------------
LEDGER="$STATE_DIR/EXPERIMENTS.md"
proposed=$(grep -c 'status: proposed' "$LEDGER" 2>/dev/null) || proposed=0
running=$(grep -c 'status: running' "$LEDGER" 2>/dev/null) || running=0

violations="$complete_violation"
if (( proposed < MIN_PROPOSED )); then
  violations+="QUEUE-VIOLATION: only $proposed 'status: proposed' experiments in EXPERIMENTS.md (minimum $MIN_PROPOSED). Refill the queue NOW with new, unblocked hypotheses — consult PATHS.md for unexplored avenues; wall-clock-blocked ideas must be 'status: deferred' and do not count. "
fi
if (( running == 0 )); then
  violations+="DISPATCH-VIOLATION: no experiment has 'status: running'. Pick the highest-expected-value proposal, mark it running, and dispatch an autodev-agent NOW. "
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
