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
