#!/usr/bin/env bash
# Stop-hook guard for the /autodev skill.
#
# While an autodev session is ACTIVE and not COMPLETE, this hook blocks the
# agent from ending its turn, forcing the orchestrator loop to continue.
# The ONLY sanctioned exit is the .autodev/COMPLETE marker, which the skill
# may write only after the completion gate passes (see SKILL.md):
#   1. every acceptance criterion in GOAL.md is demonstrably met,
#   2. full verification passed in the SAME iteration,
#   3. a completion rationale was appended to JOURNAL.md.
#
# There is deliberately NO iteration cap. Manual stop options:
#   - interrupt the session (Esc / Ctrl+C), or
#   - /autodev stop  (removes the ACTIVE marker).

set -u

STATE_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}/.autodev"

# No active session -> allow stopping normally.
if [[ ! -f "$STATE_DIR/ACTIVE" ]]; then
  exit 0
fi

# Completion gate passed -> allow stopping and retire the ACTIVE marker
# so future turns in this project are unaffected.
if [[ -f "$STATE_DIR/COMPLETE" ]]; then
  rm -f "$STATE_DIR/ACTIVE"
  exit 0
fi

# Iteration counter (logging/telemetry only — never a ceiling).
COUNT_FILE="$STATE_DIR/iteration_count"
count=$(cat "$COUNT_FILE" 2>/dev/null || echo 0)
count=$((count + 1))
echo "$count" > "$COUNT_FILE"

cat <<EOF
{"decision": "block", "reason": "AUTODEV ENFORCEMENT (iteration $count): an autodev session is ACTIVE and not COMPLETE. You must continue the orchestrator loop now. Re-read .autodev/GOAL.md and .autodev/EXPERIMENTS.md, then do the next loop step: evaluate any returned experiment results, keep or revert the work, update the ledger, propose new experiments if the goal is not met, and dispatch exactly one experiment to an autodev-agent subagent. You may only stop after the completion gate in the autodev skill passes and you have written .autodev/COMPLETE. Writing COMPLETE without a passing verification run in this same iteration is a protocol violation — never do it to escape unfinished work."}
EOF
exit 0
