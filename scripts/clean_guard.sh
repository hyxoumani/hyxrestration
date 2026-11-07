#!/usr/bin/env bash
# clean_guard.sh -- keep `git clean -x` from reaching the live tape (EXP-2203).
#
#   scripts/clean_guard.sh [--check] [DATA_DIR]     (default: <repo>/data)
#
# WHY. data/ is git-ignored (`/data/` in .gitignore), so `git clean -fd`
# spares it -- but `git clean -fdx` deletes data/ WHOLE: the live
# hyxstream.duckdb, which cannot be backfilled, and (until EXP-2203 moved
# them) all seven rotated copies of it under data/backups. Git has no
# pre-clean hook, so a hook cannot refuse the command.
#
# WHAT. Git refuses to remove a NESTED repository unless -f is given TWICE
# (`git clean -ffdx`). Measured on git 2.54 (EXP-2203 probe): with an empty
# repository at data/.git, `git clean -fdx` and `git clean -fdX` both leave
# data/ and the tape in place; only `-ffdx` removes it. So an empty
# `git init` in data/ turns the one-flag mistake into a two-flag deliberate
# act. It holds for EVERY caller -- humans, any agent, any session -- which
# the Claude Code safety-net hook (a substring match, Claude sessions only)
# does not.
#
# The sentinel repo is empty and never committed to; nothing in this repo
# runs git with data/ as its cwd. Removing it (`rm -rf data/.git`) undoes it.
#
# --check exits 0 if the guard is in place, 1 if not; prints one line.
set -euo pipefail

CHECK=0
if [ "${1:-}" = "--check" ]; then CHECK=1; shift; fi
REPO="$(cd "$(dirname "$0")/.." && pwd)"
DATA="${1:-$REPO/data}"

if [ ! -d "$DATA" ]; then
    echo "[clean_guard] no data dir at $DATA" >&2
    exit 1
fi

if [ -d "$DATA/.git" ] && git -C "$DATA" rev-parse --git-dir >/dev/null 2>&1; then
    echo "[clean_guard] OK: $DATA/.git present -- git clean needs -ff to remove $DATA"
    exit 0
fi

if [ "$CHECK" = 1 ]; then
    echo "[clean_guard] MISSING: $DATA has no nested repository; one git clean -fdx removes it"
    exit 1
fi

git init -q "$DATA"
echo "[clean_guard] installed: $DATA/.git -- git clean needs -ff to remove $DATA"
