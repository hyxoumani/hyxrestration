# Shell / Process Ops

- Never `pkill -f` / `pgrep -f` a pattern that appears in your own
  command line — the harness wraps commands in `bash -c`, so the
  pattern self-matches and kills your own shell (mistakes #4, #11).
  Use a bracket class (`sim[u]i`), or kill by a PID you hold.
- Long-running background jobs: `python -u`, harness-tracked tasks
  only — never nohup chains without captured output (mistakes #5).
- A job meant to OUTLIVE the turn must be a `systemd-run --user`
  transient unit (journald-captured, session-independent). Harness
  background tasks die with the session — the 08-04 drain died
  silently at launch this way (mistakes #19). Verify liveness by
  querying the job's persisted state (DB rows, journal), never by
  trusting that it was started.
- Multi-hour DuckDB writers exist (poly sweep ~7h). Sim-side readers
  degrade + retry lazily; never wait on the archive lock in a loop.
- Ad-hoc queries on ANY live DuckDB (hyxlab.duckdb, hyxstream.duckdb,
  hyxshadow.duckdb) MUST connect read-only (`hyxlab.store.connect_retry`
  or `read_only=True`). A default read-write connect takes the writer
  lock and made the shadow daemon crash mid-persist, ending a 1d20h
  run (mistakes #20).
- A daemon that OWNS a DuckDB file (streamd -> hyxstream.duckdb, shadow
  -> hyxshadow.duckdb) must take `hyxlab.lockid.db_owner_lock_or_reason`
  and exit 75 when refused. DuckDB's own file lock is held only for the
  duration of each write burst, so it excludes nothing between them: two
  copies interleave duplicate rows into tables with no key and no dedupe
  (EXP-1372, measured). Never start a second copy by hand to "check"
  something — the archive is unrecoverable.
- Never attach DuckDB with a bare `duckdb.connect` outside
  `hyxlab/store.py`. Use `connect_retry`/`open_retry`/`Store`, or
  `hyxlab.store.duck_connect` when the site hand-rolls a retry budget,
  degrades on error, or owns the file. A bare attach inherits DuckDB's
  DEFAULT spill directory, `<db>.tmp`, which is shared by every process
  that opens the file and whose temp files carry no pid: two spilling
  processes there SIGSEGV or read each other's blocks as their own data
  (EXP-1373, measured — the same pair with separate directories is
  clean, every time). Two readers of one archive are legitimate and take
  no lock, so no lock covers this; the kernel gives each connection
  `<db>.tmp/pid-<pid>` instead. Never pin `temp_directory` to a constant
  path — that is the bug with a name on it.
- `MemoryPeak` on a cgroup-capped unit is NOT the process's footprint.
  `memory.max` charges the PAGE CACHE the unit's own reads pull in, and
  the kernel reclaims that rather than killing, so a healthy daemon doing
  heavy file I/O reports `MemoryPeak` == its cap forever. Measured
  2026-08-27 on `hyxlab-stream` (cap 2G): `MemoryPeak` 2,147,500,032,
  `MemoryCurrent` 1.08 GiB, `memory.stat` anon 68 MiB / file 839 MiB. The
  health signal is `memory.events` — `oom_kill 0` with a large `max`
  count is reclaim, not death; `oom_kill > 0` is the kill. Read the
  cgroup, never `MemoryPeak` alone.
- A daemon's shutdown path is production code only if the signal that
  actually stops it reaches that path. systemd stops a unit with
  SIGTERM, and Python's default disposition for SIGTERM is the OS one:
  terminate at once -- no `finally`, no `atexit`, no context-manager
  `__exit__`. `collector.streamd`'s final drain, commented "never lose
  buffered events", was reachable only from Ctrl-C and `--smoke` and had
  run ZERO times in production (mistakes #54; 14 days of journal, 4
  starts, 0 shutdown lines). Before writing cleanup into a `finally`,
  name the signal the supervisor sends and install a handler for it --
  then prove it by grepping the journal for the line that path prints.
  Absence of that line is the whole proof. SIGKILL and a cgroup OOM kill
  skip every one of these regardless, so cleanup that must survive those
  belongs on disk, not in a handler.
- "Single writer" has to name its concurrency UNIT. The daemon-owns-the-
  file rules above (EXP-1372 lock ID, EXP-1373 spill directory) exclude a
  second PROCESS and prove nothing about the threads inside the one
  legitimate owner. `streamd` ran its shutdown drain CONCURRENTLY with a
  periodic flush on one `StreamStore`, because `run()`'s `finally` cancels
  the flusher task and cancelling a task awaiting `asyncio.to_thread`
  returns in 0.00s while the worker thread keeps running (joined only at
  interpreter exit). Measured: 400 sidecar rows archived as 800, and a
  60-row `spill_all` deleted by the in-flight flush's post-commit unlink --
  absent from archive, sidecar and buffer, with the gap rows that would
  have marked the hole in the same lost batch (mistakes #62). So: a
  buffered writer takes its OWN lock across every buffer/sidecar mutation,
  a shutdown path acquires that lock across its decision AND the write it
  leads to, and it waits with a bound charged against the supervisor's stop
  budget -- never `cancel()` as a synchronisation primitive.
- A cleanup handler that RUNS is not a cleanup handler that FINISHES.
  systemd SIGKILLs a unit `TimeoutStopSec` after SIGTERM, so a shutdown
  path has a row budget, not just a code path. `streamd._final_drain`
  flushes at a measured 7,100 rows/s against an unbounded sidecar (a 7h
  wedge is 6.3M rows, ~890s) while the unit inherited systemd's 90s
  DEFAULT -- the deadline was not written down anywhere in this repo.
  Losing that race is worse than declining it: SIGKILL lands after
  `flush()` has moved the buffers into locals, skipping the `except` that
  restores them and the spill that would have saved them (mistakes #61).
  So: pin `TimeoutStopSec` in the unit, measure the cleanup's throughput,
  and make the cleanup REFUSE work it cannot finish in the budget --
  preferring the fast lossless path (spill to disk, drain next boot) over
  the slow one that gets killed halfway.
- A lock held ACROSS an operation is held only if the operation cannot
  drop it, and on POSIX any `close()` does. DuckDB's file lock is an
  `fcntl` record lock, and POSIX releases every record lock a process
  holds on a file the instant that process closes ANY descriptor on it
  -- not just the one the lock was taken through. `collector.backup`
  held a read-only attach across `shutil.copyfile(src, ...)`, which
  opens the source and closes it: measured 2026-09-26, an outside
  process is refused before that close and takes the file READ-WRITE
  after it, with the holder still attached (mistakes #90). So: inside a
  hold, copy from a descriptor opened once and closed only after the
  connection is gone (`os.copy_file_range`, which also reflinks), never
  by path -- and prove the hold AND the release from outside the
  process, both arms, because a test that only checks the release
  passes equally against code that never locked.
- A measured fact with an unmeasured CAUSE cannot tell you which changes
  are safe. `collector.backup`'s 26 GB copy took 0.2s for months because
  /home is btrfs and dest shared the filesystem, so `copyfile` reflinked
  (measured: `--reflink=always` 0.199s vs `--reflink=never` 20.5s on the
  same NVMe). Nothing recorded that, and the repo's own written next
  step -- point `HYXLAB_BACKUP_DIR` at an off-box mount -- deletes the
  reflink and turns a 0.2s writer exclusion into minutes. Before
  trusting a cheap number in a journal, name the mechanism that makes it
  cheap, then ask which planned change removes it.
