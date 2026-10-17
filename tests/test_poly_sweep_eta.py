"""The poly sweep's progress line names which ETA it is (2026-10-17).

The mean rate since the start carried the run's slow opening to its last
line: `~404 min left` at 11,600/18,497 for 328 actual, `~60` at 17,400 for
48, and three passes pre-registered a budget FAIL off it. The line now
prints the mean-rate ETA AND the trailing-window one, named.
"""

from __future__ import annotations

from collector import poly_sweep as ps


def test_the_two_estimates_price_the_remainder_at_the_mean_and_the_window_rate():
    # 1,000 done in 2,000 s (2 s/market mean); the last 200 took 200 s (1 s/market)
    mean, window = ps.eta_minutes(1000, 1600, elapsed_s=2000.0, window_s=200.0)
    assert mean == 600 * 2 / 60
    assert window == 600 * 1 / 60
    # a zero-length window (first line, or a stubbed clock) falls back to the mean
    assert ps.eta_minutes(1000, 1600, 2000.0, 0.0) == (20.0, 20.0)
    # nothing done yet, or nothing left: no division, zero minutes
    assert ps.eta_minutes(0, 10, 0.0, 0.0) == (0.0, 0.0)
    assert ps.eta_minutes(10, 10, 100.0, 10.0) == (0.0, 0.0)


def test_the_line_names_both_estimates(monkeypatch, capsys, tmp_path):
    """Drives `sweep` end to end (no network, no archive) with a clock that
    makes the opening 200 markets cost 2 s each and the rest 1 s each."""
    clock = [0.0]
    monkeypatch.setattr(ps.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(ps.time, "sleep", lambda s: None)
    n = 3 * ps.PROGRESS_EVERY
    markets = [{"conditionId": f"0x{i:04d}"} for i in range(n)]
    seen = [0]

    def fake_tail(cond, session=None):
        clock[0] += 2.0 if seen[0] < ps.PROGRESS_EVERY else 1.0
        seen[0] += 1
        return []

    class _Store:
        def poly_price_watermarks(self):
            return {}

        def close(self):
            pass

    monkeypatch.setattr(
        ps.poly, "iter_markets_by_volume", lambda *a, **k: [] if k.get("closed") else list(markets)
    )
    monkeypatch.setattr(ps.poly, "token_pair", lambda m: ("yes-token", "no-token"))
    monkeypatch.setattr(ps.poly, "gamma_market_info", lambda m: (m["conditionId"],))
    monkeypatch.setattr(ps.poly, "prices_history_range", lambda *a, **k: [])
    monkeypatch.setattr(ps.poly, "price_rows", lambda *a, **k: [])
    monkeypatch.setattr(ps.poly, "trades_tail", fake_tail)
    monkeypatch.setattr(ps.poly, "take_tail_stops", lambda: [])
    monkeypatch.setattr(ps, "_flush", lambda db, batch: None)
    monkeypatch.setattr(ps, "_open", lambda db: _Store())
    monkeypatch.setattr(ps, "note_holder", lambda path: None)
    monkeypatch.setattr(ps, "LOCK_FILE", str(tmp_path / "writer.lock"))
    totals = ps.sweep("unused.duckdb", min_volume=0.0)
    assert totals["markets"] == n
    lines = [ln for ln in capsys.readouterr().out.splitlines() if "min left" in ln]
    # 200 done in 400 s, 400 left: mean 2 s -> 13.3 min; the window IS the mean here
    assert lines[0].endswith("~13 min left (mean rate; ~13 at the last 200's rate)")
    # 400 done in 600 s (1.5 s mean), 200 left: mean 5.0 min; the last 200 ran 1 s -> 3.3
    assert lines[1].endswith("~5 min left (mean rate; ~3 at the last 200's rate)")
    assert len(lines) == 3  # the final line has nothing left and says so
    assert lines[2].endswith("~0 min left (mean rate; ~0 at the last 200's rate)")
