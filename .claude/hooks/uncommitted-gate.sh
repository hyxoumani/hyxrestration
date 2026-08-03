#!/usr/bin/env bash
# Uncommitted Work Gate — Stop hook
#
# WHY THIS EXISTS. On 2026-08-03 a pass ended with a large uncommitted
# tree. That alone is recoverable — but `scripts/promote.sh` installs
# systemd units from the DEV WORKING TREE, so one of those uncommitted
# edits had ALREADY SHIPPED to production: the installed timer read
# `OnCalendar=*-*-* 06:10:00 UTC` while both git worktrees read the
# suffix-less line. Deployed state was ahead of committed state with
# `git log` showing nothing wrong. It also wedged `autoloop.sh`, which
# journalled `cannot pull with rebase: You have unstaged changes` and
# so had silently stopped syncing with origin.
#
# It then recurred on the very next pass (EXP-957/959/960 sat uncommitted
# for ~5h). The mistakes-log doctrine is explicit: gotchas do not survive
# sessions, and anything that recurs jumps straight to rule/test/hook.
# This is the hook.
#
# ESCALATE ON CHANGE, NOT FOREVER. A Stop hook that blocks on every
# dirty state can loop without bound when the leftover is deliberate.
# So the gate blocks ONCE per distinct dirty state: it fingerprints
# `git status --porcelain` and allows the stop if that exact state was
# already reported. Same shape as the fade-window check's
# FAIL-then-WATCH escalation — say it loudly once, then stop nagging.
#
# Exit 0 = allow stop. Exit 2 = Claude sees stderr and keeps working.

set -uo pipefail

DIR="${CLAUDE_PROJECT_DIR:-$PWD}"
cd "$DIR" 2>/dev/null || exit 0

# Not a git repo (or git unavailable): nothing to assert.
git rev-parse --git-dir >/dev/null 2>&1 || exit 0

STATUS=$(git status --porcelain 2>/dev/null) || exit 0
[ -z "$STATUS" ] && exit 0

GITDIR=$(git rev-parse --git-dir 2>/dev/null) || exit 0
STAMP="$GITDIR/.claude-uncommitted-gate"

# Fingerprint the dirty state so an unchanged tree is reported only once.
FP=$(printf '%s' "$STATUS" | cksum | tr -d ' ')
if [ -f "$STAMP" ] && [ "$(cat "$STAMP" 2>/dev/null)" = "$FP" ]; then
  exit 0
fi
printf '%s' "$FP" >"$STAMP" 2>/dev/null || true

{
  echo "Uncommitted work gate: the tree is dirty at end of pass."
  echo
  echo "$STATUS" | sed 's/^/    /'
  echo
  echo "This is blocking because promote.sh installs systemd units from THIS"
  echo "working tree, so an uncommitted edit can reach production while"
  echo "'git log' shows nothing — that already happened on 2026-08-03. A dirty"
  echo "tree also wedges autoloop.sh's rebase, which silently stops origin sync."
  echo
  echo "Resolve, don't ignore: commit the work, gitignore genuine scratch, or"
  echo "remove it. Stopping again with this exact state will be allowed, so"
  echo "leave it dirty only if that is a decision you can defend."
} >&2

exit 2
