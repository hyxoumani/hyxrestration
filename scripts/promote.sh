#!/usr/bin/env bash
# Promote dev main -> stable deployment worktree and restart the daemons.
#
# The systemd units (collect/sweep/stream) run from
# /home/devs/workspace/hyxrestration-stable, a git worktree pinned to the
# `stable` branch, so working-tree churn in the dev checkout can never
# break running data capture. This script is the ONLY supported way to
# ship collection-side changes: it refuses to promote unless the full
# suite is green in the dev tree.
set -euo pipefail

DEV=/home/devs/workspace/hyxrestration
STABLE=/home/devs/workspace/hyxrestration-stable

echo "== tests (dev tree) =="
cd "$DEV"
.venv/bin/python -m pytest tests/ -q

echo "== fast-forward stable -> main =="
git -C "$STABLE" merge --ff-only main

echo "== sync stable venv deps =="
"$STABLE/.venv/bin/pip" install -q -r "$DEV/scripts/requirements-stable.txt"

echo "== smoke-import in stable venv =="
(cd "$STABLE" && .venv/bin/python -c "import collector.streamd, collector.collect, collector.sweep, simulator.shadow")

echo "== install systemd units (repo scripts/systemd/ is canonical) =="
cp "$DEV"/scripts/systemd/hyxlab-* ~/.config/systemd/user/
systemctl --user daemon-reload

echo "== restart daemons (timers pick up new code on next run) =="
systemctl --user restart hyxlab-stream.service hyxlab-shadow.service
sleep 3
systemctl --user is-active hyxlab-stream.service hyxlab-shadow.service

echo "== promoted: $(git -C "$STABLE" log --oneline -1) =="
