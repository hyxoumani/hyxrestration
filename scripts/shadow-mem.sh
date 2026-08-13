#!/usr/bin/env bash
# Watch-only memory read for hyxlab-shadow. Trend `anon` — the actual
# Python heap — NOT MemoryCurrent/memory.current, which adds reclaimable
# file cache (DuckDB reads) plus per-CPU stat lag and swings tens of MB
# between reads on a quiet daemon (observed 550→501MB in minutes,
# 2026-08-13; anon was ~267MB throughout). Leak question = does anon
# grow across passes; cache growth is the archive getting bigger.
set -euo pipefail
cg=$(systemctl --user show hyxlab-shadow -p ControlGroup --value)
d="/sys/fs/cgroup${cg}"
mib() { awk -v b="$1" 'BEGIN{printf "%.0fMiB", b/1048576}'; }
read -r _ anon < <(grep -m1 '^anon ' "$d/memory.stat")
read -r _ file < <(grep -m1 '^file ' "$d/memory.stat")
cur=$(cat "$d/memory.current")
peak=$(cat "$d/memory.peak" 2>/dev/null || echo 0)
echo "shadow mem: anon=$(mib "$anon") file-cache=$(mib "$file")" \
     "current=$(mib "$cur") peak=$(mib "$peak")"
