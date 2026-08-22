"""Shadow PnL attribution: equity_delta = realized + open_carry, and the
band curve is only readable beside its settlement-selection share.
Synthetic ledgers, no network."""

from datetime import datetime, timedelta

import duckdb

from simulator.shadow_attribution import band_label, build_attribution

T0 = datetime(2026, 8, 1, 0, 0)


def _ledger(fills, settlements, equity):
    """fills: [(run_id, market_id, side, qty, price, fee)];
    settlements: [(run_id, market_id, side, payout)];
    equity: [(run_id, hours_from_T0, equity)]."""
    conn = duckdb.connect(":memory:")
    conn.execute(
        "CREATE TABLE shadow_fills (run_id VARCHAR, strategy VARCHAR, venue VARCHAR,"
        " market_id VARCHAR, side VARCHAR, qty DOUBLE, price DOUBLE, fee DOUBLE,"
        " ts TIMESTAMP)"
    )
    conn.execute(
        "CREATE TABLE shadow_settlements (run_id VARCHAR, market_id VARCHAR,"
        " side VARCHAR, payout DOUBLE, ts TIMESTAMP)"
    )
    conn.execute("CREATE TABLE shadow_equity (run_id VARCHAR, ts TIMESTAMP, equity DOUBLE)")
    conn.executemany(
        "INSERT INTO shadow_fills VALUES (?,'probe','kalshi',?,?,?,?,?,?)",
        [(r, m, s, q, p, f, T0) for (r, m, s, q, p, f) in fills],
    )
    if settlements:
        conn.executemany(
            "INSERT INTO shadow_settlements VALUES (?,?,?,?,?)",
            [(r, m, s, p, T0 + timedelta(hours=12)) for (r, m, s, p) in settlements],
        )
    conn.executemany(
        "INSERT INTO shadow_equity VALUES (?,?,?)",
        [(r, T0 + timedelta(hours=h), e) for (r, h, e) in equity],
    )
    return conn


def _run(report, run_id):
    return next(r for r in report["runs"] if r["run_id"] == run_id)


def _band(run, prefix):
    return next(b for b in run["bands"] if b["band"].startswith(prefix))


def test_identity_closes_equity_delta_into_realized_plus_carry():
    """LOAD-BEARING, and the reason the module exists. One position settles
    worthless (cost 10, fee 1 -> realized -11); a second is still open. The
    equity curve moves -20 over the run, so open_carry MUST absorb exactly
    the -9 that settlement does not explain. An implementation that reports
    realized as the whole story, or that silently drops the residual, fails
    on the arithmetic rather than on a missing key."""
    ledger = _ledger(
        fills=[
            ("r1", "SETTLED", "yes", 100.0, 0.10, 1.0),
            ("r1", "OPEN", "yes", 100.0, 0.20, 2.0),
        ],
        settlements=[("r1", "SETTLED", "yes", 0.0)],
        equity=[("r1", 0, 0.0), ("r1", 24, -20.0)],
    )
    run = _run(build_attribution(ledger), "r1")
    assert run["equity_delta"] == -20.0
    assert run["realized"] == -11.0
    assert run["open_carry"] == -9.0
    assert run["realized"] + run["open_carry"] == run["equity_delta"]


def test_a_run_that_settled_nothing_reads_null_not_zero():
    """A run killed before any of its markets resolve has NOT broken even --
    that is the standing shadow_coverage lesson. `realized` must be null, and
    open_carry with it, because a 0.0 in that column reads as a measurement
    of a flat book."""
    ledger = _ledger(
        fills=[("r1", "OPEN", "yes", 100.0, 0.10, 1.0)],
        settlements=[],
        equity=[("r1", 0, 0.0), ("r1", 6, -3.0)],
    )
    run = _run(build_attribution(ledger), "r1")
    assert run["n_settled"] == 0
    assert run["realized"] is None
    assert run["open_carry"] is None
    assert run["equity_delta"] == -3.0  # still reported; only the split is unknown


def test_a_short_or_closing_sell_voids_the_basis_arithmetic():
    """`sum(qty*price)` means "cost" only for a long-only book that never
    sells to close. A negative qty makes that read wrong rather than
    imprecise, so long_only must go false and the realized split must
    withhold rather than publish a confident wrong number."""
    ledger = _ledger(
        fills=[
            ("r1", "M", "yes", 100.0, 0.10, 1.0),
            ("r1", "M2", "yes", -50.0, 0.30, 0.5),
        ],
        settlements=[("r1", "M", "yes", 0.0)],
        equity=[("r1", 0, 0.0), ("r1", 24, -20.0)],
    )
    run = _run(build_attribution(ledger), "r1")
    assert run["long_only"] is False
    assert run["open_carry"] is None


def test_band_row_carries_its_own_settlement_share():
    """The band curve is uninterpretable without knowing that the bands were
    observed at comparable rates: a cheap band settling 100% against a dear
    band settling 25% would manufacture a price curve out of censoring
    alone. Each band row must carry its own share, computed over positions,
    not over settlements."""
    ledger = _ledger(
        fills=[
            ("r1", "CHEAP1", "yes", 100.0, 0.02, 0.2),
            ("r1", "CHEAP2", "yes", 100.0, 0.02, 0.2),
            ("r1", "DEAR1", "yes", 100.0, 0.50, 2.0),
            ("r1", "DEAR2", "yes", 100.0, 0.50, 2.0),
            ("r1", "DEAR3", "yes", 100.0, 0.50, 2.0),
            ("r1", "DEAR4", "yes", 100.0, 0.50, 2.0),
        ],
        settlements=[
            ("r1", "CHEAP1", "yes", 0.0),
            ("r1", "CHEAP2", "yes", 0.0),
            ("r1", "DEAR1", "yes", 100.0),
        ],
        equity=[("r1", 0, 0.0), ("r1", 24, -10.0)],
    )
    run = _run(build_attribution(ledger), "r1")
    assert _band(run, "a")["settled_share"] == 1.0
    assert _band(run, "f")["settled_share"] == 0.25
    assert _band(run, "a")["n_positions"] == 2
    assert _band(run, "f")["n_positions"] == 4


def test_bias_is_entry_price_minus_realised_win_rate():
    """The band curve's robust column. Four positions entered at 10c of which
    one wins: the long-only taker paid 0.10 for something worth 0.25, so the
    bias it PAID is 0.10 - 0.25 = -0.15 (it was underpaying). Sign convention
    is load-bearing -- positive means overpaying, which is what the longshot
    band does."""
    ledger = _ledger(
        fills=[("r1", f"M{i}", "yes", 100.0, 0.10, 0.0) for i in range(4)],
        settlements=[("r1", "M0", "yes", 100.0)] + [("r1", f"M{i}", "yes", 0.0) for i in (1, 2, 3)],
        equity=[("r1", 0, 0.0), ("r1", 24, 60.0)],
    )
    band = _band(_run(build_attribution(ledger), "r1"), "b")
    assert band["avg_entry"] == 0.1
    assert band["win_rate"] == 0.25
    assert band["bias"] == -0.15
    assert band["gross"] == 60.0  # payout 100 - basis 40


def test_fees_are_reported_as_a_share_of_notional():
    """Kalshi charges 0.07*p*(1-p) per contract, so the SAME fee is a trivial
    share of a 50c contract and a punitive one of a 3c contract. An
    absolute fee total hides that; the ratio is what makes a cheap book's
    economics legible."""
    ledger = _ledger(
        fills=[("r1", "M", "yes", 100.0, 0.03, 10.0)],  # basis 3.0, fee 10.0
        settlements=[("r1", "M", "yes", 0.0)],
        equity=[("r1", 0, 0.0), ("r1", 24, -13.0)],
    )
    run = _run(build_attribution(ledger), "r1")
    assert run["basis_all"] == 3.0
    assert run["fees_pct_of_notional"] == 333.33


def test_band_labels_sort_in_price_order():
    """The report prints bands in sorted order, so the labels must sort by
    price and not lexically by their cent strings ('5c' before '15c')."""
    labels = [band_label(p) for p in (0.01, 0.09, 0.20, 0.30, 0.40, 0.90)]
    assert labels == sorted(labels)
    assert labels[0].endswith("0c-5c")
    assert labels[-1].endswith("45c+")
