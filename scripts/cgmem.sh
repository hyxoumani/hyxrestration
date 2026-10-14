#!/usr/bin/env bash
# Print this unit's cgroup memory counters to stdout (the journal) at stop.
#
# WHY: a capped unit's `memory.events` -- oom_kill, and `max` (the count of
# times the cgroup hit its cap and the kernel reclaimed instead of killing)
# -- lives in the cgroup, and the cgroup is removed the moment the unit
# stops. systemd's own `Consumed ... memory peak` line survives, but it is
# memory.peak: total charge INCLUDING page cache, which reads == the cap for
# any healthy unit doing heavy file I/O (ops.md). So after the fact, a run
# that fit comfortably, one that reclaimed all day, and one that was nearly
# killed all print the same number. Measured 2026-10-13: the poly-sweep's
# first capped run showed `max 2,800,000 / oom_kill 0` live and left only
# `6G memory peak` behind. ExecStopPost runs INSIDE the service's cgroup
# (every Exec* of a service does), so this is the last reader with access.
#
# Usage: cgmem.sh [CGROUP_DIR]   -- the dir is for tests; production derives
# it from /proc/self/cgroup. Missing counters print as `?`, never a failure:
# a reporter must not turn a clean run into a failed one.
set -u

dir="${1:-}"
if [ -z "$dir" ]; then
    rel=$(sed -n 's/^0::\(.*\)$/\1/p' /proc/self/cgroup 2>/dev/null || true)
    dir="/sys/fs/cgroup${rel}"
fi

read_one() {
    # $1 file -> contents or `?`
    if [ -r "$dir/$1" ]; then tr -d '\n' <"$dir/$1"; else printf '?'; fi
}

events=""
if [ -r "$dir/memory.events" ]; then
    # `low 0\nhigh 0\nmax 12\noom 0\noom_kill 0\n...` -> `low=0 high=0 ...`
    events=$(awk '{printf "%s%s=%s", (NR>1?" ":""), $1, $2}' "$dir/memory.events")
else
    events="events=?"
fi

# `memory.pressure` (PSI): microseconds every task in the cgroup spent
# stalled on memory, i.e. what the `max` reclaim events above COST. Measured
# 2026-10-14: stream 43,761 events = 0.14s of stall; poly-sweep 5.18M events
# = 27.9s over 10.8h. The share of wall is the reader's to compute against
# systemd's own `Consumed ... wall clock time` line, printed beside this.
psi="?"
if [ -r "$dir/memory.pressure" ]; then
    psi=$(awk '$1=="full"{for(i=2;i<=NF;i++){split($i,kv,"=");if(kv[1]=="total")print kv[2]}}' \
        "$dir/memory.pressure")
    [ -n "$psi" ] || psi="?"
fi

printf '[cgmem] dir=%s peak=%s cap=%s %s psi_full_us=%s\n' \
    "$dir" "$(read_one memory.peak)" "$(read_one memory.max)" "$events" "$psi"
