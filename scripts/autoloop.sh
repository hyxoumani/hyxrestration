#!/usr/bin/env bash
# One bounded iteration of autonomous development (headless Claude Code).
# systemd timer is the while-loop; flock prevents overlapping iterations
# (and defers to any interactive session that grabs the lock first).
set -euo pipefail
cd /home/devs/workspace/hyxrestration
exec 9>data/autoloop.lock
flock -n 9 || { echo "[autoloop] another iteration or session holds the lock; skipping"; exit 0; }
git pull --rebase --quiet origin main || true
claude -p "$(cat scripts/autoloop-prompt.md)" \
  --dangerously-skip-permissions \
  --max-turns 120
