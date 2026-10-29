"""Empirical trade→decrement mapping probe (simulator/prioritycheck).

Covers the complement mapping, the exact/absorb-window split, and the
no-match decomposition (late decrement, coverage gap, naive-would-match
staying zero) on a crafted in-memory stream.
"""

import re

import duckdb
import pytest

from simulator.prioritycheck import check_market, naive_level, predicted_level

T0 = "2026-07-13 11:59:00"  # strictly before the crafted events (recv_ts > since)


def test_predicted_level_is_the_complement_mapping():
    # no-taker lifts resting YES at p; yes-taker lifts resting NO at 1-p
    assert predicted_level("no", 0.40) == ("yes", 0.40)
    assert predicted_level("yes", 0.40) == ("no", 0.60)
    assert predicted_level("bogus", 0.40) is None


def test_naive_level_is_the_wrong_same_side_mapping():
    # the mapping the complement rule replaces; probe must show it never fits
    assert naive_level("yes", 0.40) == ("yes", 0.40)
    assert naive_level("no", 0.60) == ("no", 0.60)


@pytest.fixture
def conn():
    c = duckdb.connect(":memory:")
    c.execute(
        "CREATE TABLE stream_trades(venue VARCHAR, market_id VARCHAR,"
        " recv_ts TIMESTAMP, price DOUBLE, qty DOUBLE, taker_side VARCHAR)"
    )
    c.execute(
        "CREATE TABLE book_events(venue VARCHAR, market_id VARCHAR,"
        " recv_ts TIMESTAMP, kind VARCHAR, side VARCHAR, price DOUBLE, qty DOUBLE)"
    )
    return c


def _trade(c, ts, price, qty, taker, mid="M"):
    c.execute(
        "INSERT INTO stream_trades VALUES ('kalshi', ?, ?, ?, ?, ?)",
        [mid, ts, price, qty, taker],
    )


def _delta(c, ts, side, price, qty, mid="M"):
    c.execute(
        "INSERT INTO book_events VALUES ('kalshi', ?, ?, 'delta', ?, ?, ?)",
        [mid, ts, side, price, qty],
    )


def test_exact_size_decrement_at_predicted_level_matches(conn):
    _trade(conn, "2026-07-13 12:00:00.000", 0.40, 7, "no")  # → yes@0.40
    _delta(conn, "2026-07-13 12:00:00.001", "yes", 0.40, -7)
    r = check_market(conn, "M", T0)
    assert r["exact_match"] == 1
    assert r["absorb_match"] == 1
    assert r["naive_would_match"] == 0


def test_yes_taker_maps_to_no_complement_and_same_side_never_fits(conn):
    # yes-taker at 0.40 must consume no@0.60, and a same-side yes@0.40
    # decrement of the same size must NOT be counted as a match
    _trade(conn, "2026-07-13 12:00:00.000", 0.40, 5, "yes")  # → no@0.60
    _delta(conn, "2026-07-13 12:00:00.000", "yes", 0.40, -5)  # decoy same-side
    r = check_market(conn, "M", T0)
    assert r["exact_match"] == 0
    assert r["absorb_match"] == 0
    assert r["no_decrement_at_level"] == 1
    assert r["naive_would_match"] == 1  # the decoy is exactly the naive fit


def test_late_decrement_misses_exact_but_lands_in_absorb_window(conn):
    _trade(conn, "2026-07-13 12:00:00.000", 0.40, 7, "no")
    _delta(conn, "2026-07-13 12:00:01.000", "yes", 0.40, -7)  # +1s: outside 5ms, inside 2s
    r = check_market(conn, "M", T0)
    assert r["exact_match"] == 0
    assert r["absorb_match"] == 1
    assert r["late_decrement"] == 1


def test_no_decrement_at_level_is_a_coverage_gap_not_a_mapping_miss(conn):
    _trade(conn, "2026-07-13 12:00:00.000", 0.40, 7, "no")  # → yes@0.40
    _delta(conn, "2026-07-13 12:00:00.001", "yes", 0.55, -7)  # wrong price entirely
    r = check_market(conn, "M", T0)
    assert r["absorb_match"] == 0
    assert r["no_decrement_at_level"] == 1
    assert r["naive_would_match"] == 0


def test_late_decrement_contributes_its_delay_to_the_timing_sample(conn):
    """The tail that sizes ABSORB_WINDOW must be IN the timing sample.

    A sample drawn only from exact-window matches is bounded by ±5ms by
    construction, so it cannot report a delay wider than the window it is
    quoted to justify -- and the prints it drops are exactly the late
    ones. The 2026-07-13 reading dropped 159 of them and published a
    `max_ms` of 4.93, i.e. the window edge.
    """
    _trade(conn, "2026-07-13 12:00:00.000", 0.40, 7, "no")
    _delta(conn, "2026-07-13 12:00:00.001", "yes", 0.40, -7)  # +1ms: exact
    _trade(conn, "2026-07-13 12:00:10.000", 0.40, 7, "no")
    _delta(conn, "2026-07-13 12:00:11.000", "yes", 0.40, -7)  # +1s: late
    r = check_market(conn, "M", T0)
    assert r["exact_match"] == 1
    assert r["late_decrement"] == 1
    assert r["absorb_match"] == 2
    assert [round(x, 3) for x in r["dt_ms"]] == [1.0, 1000.0]


def test_timing_publishes_the_absorb_population_and_the_conditioned_one():
    from simulator.prioritycheck import EXACT_WINDOW, _timing

    dt_ms = sorted([-4.0, 0.5, 1.5, 900.0])
    full = _timing(dt_ms)
    assert full["n"] == 4
    assert full["max_ms"] == 900.0  # the tail, not the window edge
    assert full["within_1ms_frac"] == 0.25

    tight = _timing([x for x in dt_ms if abs(x) <= EXACT_WINDOW * 1000])
    assert tight["n"] == 3
    assert tight["max_ms"] == 1.5
    assert tight["within_1ms_frac"] == 0.3333  # 0.8495 on the 07-13 reading


def test_p95_is_the_nearest_rank_not_the_maximum():
    """`ordered[int(0.95 * n)]` is the maximum for every n <= 20.

    It takes the (floor(0.95n)+1)-th order statistic, so its rank is at
    or above 95% always, and exactly the maximum whenever 0.95n is an
    integer -- publishing `max_ms` a second time under the name `p95_ms`.
    """
    from simulator.prioritycheck import _p95

    xs = list(range(1, 21))  # n=20: 0.95n = 19 exactly
    assert xs[int(len(xs) * 0.95)] == 20  # the old index: the maximum
    assert _p95(xs) == 19  # nearest rank: the smallest value >= 95%

    assert _p95([7.0]) == 7.0  # n=1 must not underflow to index -1
    assert _p95(list(range(1, 101))) == 95


def test_timing_censuses_the_tail_that_sizes_the_window():
    """ "ABSORB_WINDOW=2s is generous" has to be checkable FROM the artifact.

    The 2026-09-20 reading needed 1796ms of window on its widest pairing
    -- 90% of the 2s budget -- which no statistic the old timing block
    published could have shown.
    """
    from simulator.prioritycheck import _timing

    out = _timing(sorted([-1796.2, -0.5, 0.5, 6.0, 60.0]))
    assert out["over_ms"] == {"5": 3, "50": 2, "500": 1}


def test_the_selector_is_queuescores_not_a_copy():
    """`main` carried the SAME unbounded correlated EXISTS over `book_events`
    that killed the weather width-24 bracket (#124), one file over from the
    fix. The fix is to have ONE selector: a second query string here is a
    second place for the bound to be missing."""
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "simulator/prioritycheck.py").read_text()
    assert "FROM book_events b" not in src, "a second selector query string"
    assert re.search(r"from simulator\.queuescore import .*\bselect_markets\b", src), src[:200]


def test_main_measures_the_hold_on_the_stream_archive(tmp_path, monkeypatch, capsys):
    """The check holds `hyxstream.duckdb` -- streamd's file -- across its
    whole loop and until 2026-10-16 published nothing about it. Driven
    end-to-end on an empty archive: zero markets, one measured hold, the
    line rendered through the one renderer and the block in the report."""
    import json

    from hyxlab.streamstore import StreamStore
    from simulator import prioritycheck

    db = tmp_path / "hyxstream.duckdb"
    StreamStore(db).flush()
    out_dir = tmp_path / "out"
    monkeypatch.setattr(
        "sys.argv", ["prioritycheck", "--stream-db", str(db), "--out", str(out_dir)]
    )
    prioritycheck.main()
    text = capsys.readouterr().out
    lines = [ln for ln in text.splitlines() if ln.startswith("[prioritycheck] attach_wait:")]
    assert len(lines) == 1 and "on hyxstream.duckdb 1 attaches" in lines[0], text
    assert "held 1 for" in lines[0] and "0 unmeasured" in lines[0], lines[0]
    report = json.loads(next(out_dir.glob("*.json")).read_text())
    assert report["markets"] == 0
    assert report["attach_wait"]["held_n"] == 1 and report["attach_wait"]["held_unknown_n"] == 0


def test_streamed_rows_pair_identically_to_the_one_market_read(tmp_path):
    """`main` reads every selected market in ONE scan per table
    (`queuescore.iter_market_rows`) and pairs each group as it completes;
    until 2026-10-29 it ran `check_market`'s two `WHERE market_id=?` reads
    per market -- 2N window scans on streamd's file (#147). The streamed
    rows must pair exactly as the one-market read did: three markets
    interleaved in time so the `ORDER BY market_id` grouping is exercised,
    `chunk=2` so every group spans a fetchmany boundary, and a positive
    delta per market that `negative_deltas` must drop."""
    from datetime import datetime, timedelta

    from hyxlab.streamstore import StreamStore
    from simulator.prioritycheck import check_rows, negative_deltas
    from simulator.queuescore import iter_market_rows
    from tests.test_hyxlab_queuescore import _delta as sdelta
    from tests.test_hyxlab_queuescore import _image
    from tests.test_hyxlab_queuescore import _trade as strade

    t0 = datetime(2026, 7, 11, 12, 0)
    db = tmp_path / "s.duckdb"
    store = StreamStore(db)
    for mid, base in (("A", 10), ("B", 20), ("C", 30)):
        store.append_events(_image(mid, base, t0))
        store.append_events(
            sdelta(mid, base + 1, t0 + timedelta(seconds=1), "yes", "0.1000", "1.00")
        )
    # A: exact match at the complement level; B: no-taker print, decrement 1.5s late;
    # C: yes-taker print whose decrement lands at the SAME-side level (naive fit).
    store.append_trades(strade("A", 12, t0 + timedelta(seconds=60), "0.4000", "35.00", "no"))
    store.append_events(sdelta("A", 13, t0 + timedelta(seconds=60), "yes", "0.4000", "-35.00"))
    store.append_trades(strade("B", 22, t0 + timedelta(seconds=60), "0.4000", "40.00", "no"))
    store.append_events(
        sdelta("B", 23, t0 + timedelta(seconds=61, milliseconds=500), "yes", "0.4000", "-40.00")
    )
    store.append_trades(strade("C", 32, t0 + timedelta(seconds=61), "0.4000", "10.00", "yes"))
    store.append_events(sdelta("C", 33, t0 + timedelta(seconds=61), "yes", "0.4000", "-10.00"))
    for mid, base in (("A", 14), ("B", 24), ("C", 34)):
        store.append_events(sdelta(mid, base, t0 + timedelta(seconds=90), "yes", "0.1000", "1.00"))
    store.flush()

    since = t0 - timedelta(minutes=1)
    conn = duckdb.connect(str(db), read_only=True)
    one_by_one = {m: check_market(conn, m, since) for m in ("A", "B", "C")}
    streamed = list(iter_market_rows(conn, ["A", "B", "C", "ABSENT"], since, chunk=2))
    conn.close()

    assert [m for m, _, _ in streamed] == ["A", "B", "C", "ABSENT"]
    assert all(len(ev) > 2 for _, ev, _ in streamed[:3])  # each group spans a chunk boundary
    paired = {m: check_rows(m, tr, negative_deltas(ev)) for m, ev, tr in streamed}
    assert {m: paired[m] for m in ("A", "B", "C")} == one_by_one
    assert paired["ABSENT"]["trades"] == 0 and paired["ABSENT"]["neg_deltas"] == 0
    # not vacuous: the three markets land in three different cells
    assert one_by_one["A"]["exact_match"] == 1
    assert one_by_one["B"]["late_decrement"] == 1 and one_by_one["B"]["exact_match"] == 0
    assert one_by_one["C"]["naive_would_match"] == 1 and one_by_one["C"]["absorb_match"] == 0
    # the positive deltas were dropped before pairing, not counted
    assert all(r["neg_deltas"] == 1 for r in one_by_one.values())


def test_main_publishes_the_cost_block_with_the_pairing_label(tmp_path, monkeypatch, capsys):
    """The report carries queuescore's `cost` block (held vs compute split,
    rows, rss) rendered through the ONE renderer (#95), and the line names
    the compute as pairing -- not the bracket's replay."""
    import json

    from hyxlab.streamstore import StreamStore
    from simulator import prioritycheck

    db = tmp_path / "hyxstream.duckdb"
    StreamStore(db).flush()
    out_dir = tmp_path / "out"
    monkeypatch.setattr(
        "sys.argv", ["prioritycheck", "--stream-db", str(db), "--out", str(out_dir)]
    )
    prioritycheck.main()
    text = capsys.readouterr().out
    lines = [ln for ln in text.splitlines() if ln.startswith("[prioritycheck] cost:")]
    assert len(lines) == 1 and "of which pairing 0.0s" in lines[0], text
    assert "replay" not in lines[0]
    report = json.loads(next(out_dir.glob("*.json")).read_text())
    cost = report["cost"]
    assert cost["markets"] == 0 and cost["held_s"] is not None and cost["held_unknown_n"] == 0


def _brute_force_check_rows(market_id, trades, deltas):
    """The pre-2026-10-29 pairing, verbatim in shape: a comprehension over the
    whole level per print. Kept here as the oracle for the bisect path."""
    from collections import defaultdict

    from simulator.prioritycheck import ABSORB_WINDOW, EXACT_WINDOW, PRICE_EPS

    idx = defaultdict(list)
    for ts, side, price, qty in deltas:
        idx[(side, round(price, 4))].append((ts, -qty))
    for b in idx.values():
        b.sort()
    res = dict.fromkeys(
        (
            "exact_match",
            "absorb_match",
            "naive_would_match",
            "late_decrement",
            "no_decrement_at_level",
            "batched_with_cancels",
        ),
        0,
    )
    res.update(market_id=market_id, trades=len(trades), neg_deltas=len(deltas), dt_ms=[])
    for ts, p, q, taker in trades:
        key = predicted_level(taker, p)
        near_exact = [
            (dts, dec)
            for dts, dec in idx.get(key, [])
            if abs((dts - ts).total_seconds()) <= EXACT_WINDOW
        ]
        hit = next((dts for dts, dec in near_exact if abs(dec - q) < PRICE_EPS), None)
        if hit is not None:
            res["exact_match"] += 1
            res["absorb_match"] += 1
            res["dt_ms"].append((hit - ts).total_seconds() * 1000)
            continue
        near_absorb = [
            (dts, dec)
            for dts, dec in idx.get(key, [])
            if abs((dts - ts).total_seconds()) <= ABSORB_WINDOW
        ]
        late = next((dts for dts, dec in near_absorb if abs(dec - q) < PRICE_EPS), None)
        if late is not None:
            res["absorb_match"] += 1
            res["late_decrement"] += 1
            res["dt_ms"].append((late - ts).total_seconds() * 1000)
        elif not near_absorb:
            res["no_decrement_at_level"] += 1
        else:
            res["batched_with_cancels"] += 1
        naive = naive_level(taker, p)
        if any(
            abs((dts - ts).total_seconds()) <= ABSORB_WINDOW and abs(dec - q) < PRICE_EPS
            for dts, dec in idx.get(naive, [])
        ):
            res["naive_would_match"] += 1
    return res


def test_bisect_pairing_equals_the_whole_level_scan_on_random_tapes():
    """The pairing slices each level by `bisect` instead of scanning it per
    print (2026-10-29: the scan was 206.7s of a 224.8s hold on streamd's
    file). Same answer as the scan, print for print, on random tapes dense
    enough that windows overlap, levels repeat, timestamps tie and
    decrements sit exactly ON both window edges -- the `<=` boundary."""
    import random
    from datetime import datetime, timedelta

    from simulator.prioritycheck import ABSORB_WINDOW, EXACT_WINDOW, check_rows

    rng = random.Random(1729)
    t0 = datetime(2026, 7, 11, 12, 0)
    prices = [0.40, 0.41, 0.59, 0.60]
    sizes = [5.0, 10.0, 15.0]
    nontrivial = 0
    for _ in range(40):
        trades, deltas = [], []
        for _ in range(60):
            ts = t0 + timedelta(milliseconds=rng.randrange(0, 20_000))
            trades.append((ts, rng.choice(prices), rng.choice(sizes), rng.choice(["yes", "no"])))
        for _ in range(200):
            ts = t0 + timedelta(milliseconds=rng.randrange(0, 20_000))
            deltas.append((ts, rng.choice(["yes", "no"]), rng.choice(prices), -rng.choice(sizes)))
        # edge cases pinned, not left to chance: a decrement exactly at the
        # exact-window edge and one exactly at the absorb-window edge, plus
        # two at the same instant on one level with different sizes
        ts, p, q, taker = trades[0]
        side, lvl = predicted_level(taker, p)
        deltas.append((ts + timedelta(seconds=EXACT_WINDOW), side, lvl, -q))
        ts, p, q, taker = trades[1]
        side, lvl = predicted_level(taker, p)
        deltas.append((ts - timedelta(seconds=ABSORB_WINDOW), side, lvl, -q))
        ts, p, q, taker = trades[2]
        side, lvl = predicted_level(taker, p)
        deltas.append((ts, side, lvl, -q))
        deltas.append((ts, side, lvl, -(q + 1)))
        deltas.sort()
        got = check_rows("M", trades, deltas)
        want = _brute_force_check_rows("M", trades, deltas)
        assert got == want
        nontrivial += got["exact_match"] > 0 and got["late_decrement"] > 0
        nontrivial += got["batched_with_cancels"] > 0 and got["no_decrement_at_level"] > 0
    assert nontrivial >= 40, nontrivial  # every cell of the decomposition exercised, often


def test_series_flag_reaches_the_shared_selector_and_the_report(tmp_path, monkeypatch, capsys):
    """`--series KXHIGH` is how the 2026-09-20 weather reading stays
    comparable once the top-N by prints is KXBTC15M; it must reach
    `queuescore.select_markets` as its `series` argument and be recorded."""
    import json

    from hyxlab.streamstore import StreamStore
    from simulator import prioritycheck

    db = tmp_path / "hyxstream.duckdb"
    StreamStore(db).flush()
    calls = []

    def fake_select(conn, since, top_n, series=None):
        calls.append((top_n, series))
        return []

    monkeypatch.setattr(prioritycheck, "select_markets", fake_select)
    out_dir = tmp_path / "out"
    monkeypatch.setattr(
        "sys.argv",
        [
            "prioritycheck",
            "--stream-db",
            str(db),
            "--out",
            str(out_dir),
            "--series",
            "KXHIGH, KXLOW",
        ],
    )
    prioritycheck.main()
    assert calls == [(8, ["KXHIGH", "KXLOW"])]
    report = json.loads(next(out_dir.glob("*.json")).read_text())
    assert report["series"] == ["KXHIGH", "KXLOW"]
    assert "series=['KXHIGH', 'KXLOW']" in capsys.readouterr().out
