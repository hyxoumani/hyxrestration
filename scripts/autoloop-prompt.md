Run one iteration of autonomous development per CLAUDE.md Working Mode.

Cold-start: read docs/wiki/status.md first, then git log --oneline -15.
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
