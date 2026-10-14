Run one iteration of autonomous development per CLAUDE.md Working Mode.

Cold-start: read docs/wiki/status.md first, then git log --oneline -15,
then run `.venv/bin/python -m collector.health` — the machine-written
digest of what the manager and QA already decided (it re-runs no check
and opens no DB). Anything it flags outranks the ladder below.
Pick the highest-value item: the execution queue if non-empty, else the
investigation ladder — (1) re-run standing reports (divergence, maker
bracket, atlas, QA) on newly accumulated data and chase drift; (2)
verify an unverified design-note assumption; (3) analyze data for the
next strategy lead; (4) harden mistakes-log/backlog items.

Ship it: tests green, commit, promote via scripts/promote.sh when
collection-side, push origin main, update the wiki. Hard rules are
binding: zero capital, no retro-rescues, pre-registration before any
backtest verdict. If everything is user-gated or data-gated, write one
line to docs/wiki/status.md noting the check and stop cleanly.

Budget: `AUTOLOOP_DEADLINE` (env, epoch seconds) is when the launcher
sends SIGTERM to this process; `date +%s` tells you what is left. Before
starting a ship or arming a wakeup, check it. Under 30 minutes left:
finish and commit what is in flight, write the status line, and end the
turn WITHOUT arming — the hyxlab-autoloop timer (6h) is the armed next
turn. Never arm a wakeup that lands after the deadline: a pass cut by the
kill leaves uncommitted work for the next run to inherit unread.
