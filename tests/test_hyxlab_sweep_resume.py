"""Truncated-sweep resume floor (EXP-1271, 2026-08-12).

The module contract says a re-run "resumes where the last one stopped"
(sweep.py docstring; the MAX_MARKETS_PER_SERIES comment; every truncated
sweep_log note says "resume from <max_close>"). The code said
`floor_ts = max(now - days, wm + 1s)` — so whenever a series' watermark
fell more than `--days` behind (the daily timer runs --days 2), the
window clamp silently jumped the floor FORWARD past the watermark and
the wm -> now-days range was never requested, while the watermark then
advanced past it: a permanent hole, the EXP-931 range-loss shape.

Measured live in sweep_log before the fix: the 9 chronically-truncated
dense series (KXBTC/KXETH/KXSOLD/KXMVE*...) lost 0.8h-24h of close-time
coverage PER DAY each — every run's min_close sat at now-2d, never at
the previous run's max_close, despite every note promising resume.

Fixed: with a watermark present, the floor is wm + 1s, bounded below
only by the venue purge horizon (PURGE_HORIZON_DAYS), never by --days.
"""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from collector import sweep as sweep_mod


class _StubStore:
    def __init__(self, wm: datetime | None):
        self._wm = wm
        self.logged = []

    def watermark(self, series):
        return self._wm

    def log_sweep(self, *args):
        self.logged.append(args)


def _run(monkeypatch, wm: datetime | None, days: int) -> dict:
    """Run sweep_series with a stubbed store; capture the markets request."""
    store = _StubStore(wm)
    seen = {}

    @contextmanager
    def fake_burst(db, lock_file=None):
        yield store

    def fake_ascending(series_ticker, status, *, min_close_ts, max_close_ts, **kw):
        seen["min_close_ts"] = min_close_ts
        seen["max_close_ts"] = max_close_ts
        return [], False  # early-return path: logs "no settled markets"

    monkeypatch.setattr(sweep_mod, "writer_burst", fake_burst)
    monkeypatch.setattr(sweep_mod.kalshi, "get_markets_ascending", fake_ascending)
    monkeypatch.setattr(sweep_mod.time, "sleep", lambda s: None)
    sweep_mod.sweep_series("unused.duckdb", "KXTEST", days, session=None)
    return seen


def test_stale_watermark_resumes_from_watermark_not_window_floor(monkeypatch):
    # The live defect: wm 5 days back, daily timer --days 2. The old
    # max(now-2d, wm+1s) floored at now-2d and permanently skipped
    # wm -> now-2d. The floor must be the watermark.
    wm = datetime.now(UTC) - timedelta(days=5)
    seen = _run(monkeypatch, wm, days=2)
    expected = int((wm + timedelta(seconds=1)).timestamp())
    assert abs(seen["min_close_ts"] - expected) <= 1, (
        f"floor {datetime.fromtimestamp(seen['min_close_ts'], tz=UTC)} is not "
        f"the watermark+1s {wm + timedelta(seconds=1)} — the --days window "
        f"clamp is skipping unswept range (EXP-931 range loss)"
    )


def test_fresh_watermark_still_resumes_from_watermark(monkeypatch):
    # The healthy daily case must be unchanged: wm inside the window.
    wm = datetime.now(UTC) - timedelta(days=1)
    seen = _run(monkeypatch, wm, days=2)
    expected = int((wm + timedelta(seconds=1)).timestamp())
    assert abs(seen["min_close_ts"] - expected) <= 1


def test_ancient_watermark_bounded_by_purge_horizon(monkeypatch):
    # A watermark past the venue purge horizon buys nothing: that data
    # is gone. The floor is bounded at now - PURGE_HORIZON_DAYS so a
    # long-dormant series costs a handful of empty-window probes, not a
    # walk into a purged past.
    wm = datetime.now(UTC) - timedelta(days=400)
    seen = _run(monkeypatch, wm, days=2)
    horizon = datetime.now(UTC) - timedelta(days=sweep_mod.PURGE_HORIZON_DAYS)
    assert abs(seen["min_close_ts"] - int(horizon.timestamp())) <= 5


def test_no_watermark_uses_days_window(monkeypatch):
    # First-ever sweep of a series: --days is the capture depth.
    seen = _run(monkeypatch, None, days=2)
    expected = datetime.now(UTC) - timedelta(days=2)
    assert abs(seen["min_close_ts"] - int(expected.timestamp())) <= 5
