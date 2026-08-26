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
