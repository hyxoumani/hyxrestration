"""collector — 24/7 data capture (venue connectors, sweeps, stream daemons, QA).

Runs unattended from the stable worktree; deployed only via
scripts/promote.sh. May import the `hyxlab` kernel only — never
`simulator` or `strategies` (enforced by tests/test_boundaries.py).
"""
