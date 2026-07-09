# Shell / Process Ops

- Never `pkill -f` / `pgrep -f` a pattern that appears in your own
  command line — the harness wraps commands in `bash -c`, so the
  pattern self-matches and kills your own shell (mistakes #4, #11).
  Use a bracket class (`sim[u]i`), or kill by a PID you hold.
- Long-running background jobs: `python -u`, harness-tracked tasks
  only — never nohup chains without captured output (mistakes #5).
- Multi-hour DuckDB writers exist (poly sweep ~7h). Sim-side readers
  degrade + retry lazily; never wait on the archive lock in a loop.
