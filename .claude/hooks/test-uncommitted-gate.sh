#!/usr/bin/env bash
# Regression test harness for uncommitted-gate.sh.
#
# Builds throwaway git repos under a private temp root, runs the REAL hook
# against each (CLAUDE_PROJECT_DIR pointed at the synthetic repo, never at
# this one), and asserts the exit code.
#
# Usage:  .claude/hooks/test-uncommitted-gate.sh
# Exit code: 0 if all scenarios pass, 1 if any fails.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK="$SCRIPT_DIR/uncommitted-gate.sh"

if [[ ! -f "$HOOK" ]]; then
  echo "FATAL: hook not found at $HOOK" >&2
  exit 1
fi

TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/uncommitted-gate-test.XXXXXX")
trap 'rm -rf "$TMP_ROOT"' EXIT

pass_count=0
fail_count=0
declare -a failures=()

new_repo() {
  local d="$TMP_ROOT/$1"
  mkdir -p "$d"
  git -C "$d" init -q
  git -C "$d" config user.email t@t.t
  git -C "$d" config user.name t
  echo base >"$d/f.txt"
  git -C "$d" add -A
  git -C "$d" commit -qm base
  echo "$d"
}

run_hook() {  # run_hook <dir> -> echoes exit code
  ( CLAUDE_PROJECT_DIR="$1" bash "$HOOK" >/dev/null 2>&1 )
  echo $?
}

check() {  # check <name> <expected> <actual>
  if [ "$2" = "$3" ]; then
    pass_count=$((pass_count + 1))
    echo "  PASS  $1"
  else
    fail_count=$((fail_count + 1))
    failures+=("$1: expected exit $2, got $3")
    echo "  FAIL  $1 (expected $2, got $3)"
  fi
}

echo "uncommitted-gate.sh"

# A clean tree must never block.
d=$(new_repo clean)
check "a clean tree allows the stop" 0 "$(run_hook "$d")"

# THE load-bearing case: a modified tracked file blocks. This is the state
# that shipped an uncommitted systemd unit to production on 2026-08-03.
d=$(new_repo modified)
echo changed >"$d/f.txt"
check "a modified tracked file blocks the stop" 2 "$(run_hook "$d")"

# Untracked counts too: the recurrence included a brand-new test file that
# existed only in the working tree.
d=$(new_repo untracked)
echo new >"$d/extra.py"
check "an untracked file blocks the stop" 2 "$(run_hook "$d")"

# Escalate on CHANGE: the identical dirty state must not block twice, or a
# deliberate leftover loops the agent without bound.
d=$(new_repo repeat)
echo changed >"$d/f.txt"
first=$(run_hook "$d")
second=$(run_hook "$d")
check "the same dirty state blocks once" 2 "$first"
check "the same dirty state does not block twice" 0 "$second"

# ...but a DIFFERENT dirty state after an accepted one must block again,
# otherwise one accepted leftover silences every later mistake.
echo more >"$d/another.py"
check "a new dirty state escalates again" 2 "$(run_hook "$d")"

# Discrimination control: committing the work must clear the gate, so a
# "fix" that simply always allowed would not pass the blocking cases above
# and a gate that always blocked would not pass this one.
d=$(new_repo committed)
echo changed >"$d/f.txt"
[ "$(run_hook "$d")" = "2" ] || true
git -C "$d" add -A && git -C "$d" commit -qm work
check "committing the work clears the gate" 0 "$(run_hook "$d")"

# A non-git directory is out of scope, not an error.
d="$TMP_ROOT/nogit"
mkdir -p "$d"
check "a non-git directory allows the stop" 0 "$(run_hook "$d")"

echo
echo "passed: $pass_count  failed: $fail_count"
if [ "$fail_count" -ne 0 ]; then
  printf '  %s\n' "${failures[@]}" >&2
  exit 1
fi
exit 0
