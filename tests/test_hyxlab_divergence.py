"""Divergence report: offline replay of a shadow run over the same
recording must reproduce its fills exactly — the zero baseline that
makes nonzero divergence on real runs attributable to infrastructure
(late archive rows, gaps unknown live) rather than method noise."""

import json
import os
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

import duckdb

import simulator.divergence as mod
from hyxlab import importclosure
from hyxlab.models import MarketInfo
from hyxlab.store import Store
from hyxlab.streamstore import StreamStore
from simulator.divergence import compare, latest_complete_run, replay_run
from simulator.shadow import ShadowLedger, ShadowRunner
from simulator.sim import Simulator
from strategies.probe import TightSpreadProbe
from tests.test_hyxlab_shadow import T0, _snapshot_frame


def test_replay_reproduces_shadow_run_exactly(tmp_path):
    stream_db = tmp_path / "stream.duckdb"
    archive_db = tmp_path / "archive.duckdb"
    shadow_db = tmp_path / "shadow.duckdb"

    store = Store(archive_db)
    store.upsert_markets([MarketInfo(venue="kalshi", market_id="M1")])
    store.close()

    sstore = StreamStore(stream_db)
    sstore.append_events(_snapshot_frame("M1", 1, 40, 59, T0))  # history: never traded
    sstore.flush()

    runner = ShadowRunner(
        [TightSpreadProbe()],  # defaults — divergence replays the same
        latency=0.0,
        stream_db=str(stream_db),
        archive_db=str(archive_db),
        ledger=ShadowLedger(shadow_db),
    )
    runner.poll_once()  # anchors at T0 and persists the anchor

    # Tight books 11 min apart (past the probe cooldown) → three fills,
    # with one wide-spread batch in between that must not trade.
    batches = [
        (2, 44, 55, 11),  # yes 0.44/0.45 → fill
        (3, 30, 60, 17),  # yes 0.30/0.40 → too wide, no fill
        (4, 45, 54, 22),  # → fill
        (5, 44, 55, 33),  # → fill
    ]
    for seq, bid, ask_no, minutes in batches:
        sstore.append_events(
            _snapshot_frame("M1", seq, bid, ask_no, T0 + timedelta(minutes=minutes))
        )
        sstore.flush()
        runner.poll_once()

    with duckdb.connect(str(shadow_db), read_only=True) as conn:
        anchor = conn.execute("SELECT anchor FROM shadow_runs").fetchone()[0]
        end = conn.execute("SELECT max(ts) FROM shadow_equity").fetchone()[0]
        shadow_fills = conn.execute(
            "SELECT market_id, side, qty, price, fee, maker, ts FROM shadow_fills ORDER BY ts"
        ).fetchall()
    assert anchor == T0.replace(tzinfo=None)  # first poll recorded the anchor
    assert len(shadow_fills) == 3

    replay_fills = replay_run(
        runner.run_id,
        anchor,
        end,
        latency=0.0,
        strategy_names=["probe"],
        stream_db=str(stream_db),
        archive_db=str(archive_db),
    )
    assert [(f.market_id, f.side, f.qty, f.price, f.ts) for f in replay_fills] == [
        (m, s, q, p, ts) for m, s, q, p, _, _, ts in shadow_fills
    ]

    report = compare(shadow_fills, replay_fills)
    assert report["matched"] == 3
    assert report["match_rate_vs_shadow"] == 1.0
    assert report["match_rate_vs_replay"] == 1.0
    assert report["price_delta_abs_mean"] == 0.0


def test_split_fills_count_in_qty_match_rate():
    """5 vs 3+2 in the same minute: order-level match fails (floor),
    qty-level overlap credits it fully (v2)."""
    from datetime import datetime

    from simulator.divergence import compare

    class F:
        def __init__(self, qty, ts, price=0.4):
            self.market_id, self.side = "M1", "yes"
            self.qty, self.price, self.fee, self.maker = qty, price, 0.0, False
            self.ts = ts

    t = datetime(2026, 7, 12, 1, 0, 30)
    shadow = [("M1", "yes", 5.0, 0.4, 0.0, False, t)]
    replay = [F(3.0, t), F(2.0, t.replace(second=45))]
    rep = compare(shadow, replay)
    assert rep["matched"] == 0  # order-level: qty mismatch
    assert rep["qty_match_rate_vs_shadow"] == 1.0
    assert rep["qty_match_rate_vs_replay"] == 1.0


# ---- tiered matching (v2: exact / nearest / split, EXP-004) ----------------


class _RF:
    """Minimal replay-fill stand-in for compare()."""

    def __init__(self, qty, ts, price=0.4, market_id="M1", side="yes"):
        self.market_id, self.side = market_id, side
        self.qty, self.price, self.fee, self.maker = qty, price, 0.0, False
        self.ts = ts


def _shadow(qty, ts, price=0.4, market_id="M1", side="yes"):
    return (market_id, side, qty, price, 0.0, False, ts)


T = datetime(2026, 7, 12, 1, 0, 0)


def test_exact_only_dataset_reports_zero_relaxed_matches():
    """Identical fill streams: every match is tier-exact; nearest and
    split stay at zero and the v1 headline fields are untouched."""
    shadow = [_shadow(5.0, T), _shadow(2.0, T + timedelta(minutes=11), price=0.3)]
    replay = [_RF(5.0, T), _RF(2.0, T + timedelta(minutes=11), price=0.3)]
    rep = compare(shadow, replay)
    assert rep["matched"] == 2
    assert rep["match_rate_vs_shadow"] == 1.0
    assert rep["price_delta_abs_mean"] == 0.0
    assert rep["matched_nearest"] == 0
    assert rep["matched_split_groups"] == 0
    assert rep["matched_all_vs_shadow"] == 2
    assert rep["match_rate_all_vs_replay"] == 1.0
    assert rep["price_delta_abs_mean_all"] == 0.0


def test_same_fill_shifted_300ms_matches_at_exact_tier():
    """v1's exact tier already tolerates pure time offsets (60s window),
    so a 300ms-shifted identical fill is exact — NOT a relaxed match.
    This is why v2 cannot perturb the shipped convergence result."""
    rep = compare([_shadow(5.0, T)], [_RF(5.0, T + timedelta(milliseconds=300))])
    assert rep["matched"] == 1
    assert rep["matched_nearest"] == 0
    assert rep["matched_split_groups"] == 0


def test_offset_qty_perturbed_fill_matches_nearest_tier_with_dt():
    """Same market/side/price, 300ms apart, qty 5 vs 4: exact refuses
    (qty), nearest claims it and reports the |dt|; its price delta is
    zero by construction and stays out of the exact-tier stats."""
    rep = compare([_shadow(5.0, T)], [_RF(4.0, T + timedelta(milliseconds=300))])
    assert rep["matched"] == 0
    assert rep["matched_nearest"] == 1
    assert rep["nearest_dt_abs_mean_s"] == 0.3
    assert rep["price_delta_abs_mean_nearest"] == 0.0
    assert rep["price_delta_abs_mean"] is None  # exact tier saw nothing
    assert rep["matched_all_vs_shadow"] == 1


def test_three_partials_summing_to_one_fill_match_split_tier():
    """Replay fills 1+2+3 at one price within the window sum to the
    shadow fill's 6: matched as one split group, not nearest/exact."""
    shadow = [_shadow(6.0, T)]
    replay = [
        _RF(1.0, T),
        _RF(2.0, T + timedelta(milliseconds=200)),
        _RF(3.0, T + timedelta(milliseconds=400)),
    ]
    rep = compare(shadow, replay)
    assert rep["matched"] == 0
    assert rep["matched_nearest"] == 0
    assert rep["matched_split_groups"] == 1
    assert rep["matched_split_shadow_fills"] == 1
    assert rep["matched_split_replay_fills"] == 3
    assert rep["matched_all_vs_shadow"] == 1
    assert rep["matched_all_vs_replay"] == 3
    assert rep["price_delta_abs_mean_split"] == 0.0


def test_shadow_partials_matching_one_replay_fill_split_reverse_direction():
    """Split grouping is symmetric: 2+3 shadow partials vs one 5-qty
    replay fill also match as a group."""
    shadow = [_shadow(2.0, T), _shadow(3.0, T + timedelta(milliseconds=500))]
    rep = compare(shadow, [_RF(5.0, T)])
    assert rep["matched"] == 0
    assert rep["matched_split_groups"] == 1
    assert rep["matched_split_shadow_fills"] == 2
    assert rep["matched_split_replay_fills"] == 1


def test_fill_outside_nearest_window_stays_unmatched():
    """Same price but 10s apart (> 2s window) and qty-mismatched: no
    tier may claim it — relaxation must not rescue gap-window fills."""
    rep = compare([_shadow(5.0, T)], [_RF(4.0, T + timedelta(seconds=10))])
    assert rep["matched"] == 0
    assert rep["matched_nearest"] == 0
    assert rep["matched_split_groups"] == 0
    assert rep["matched_all_vs_shadow"] == 0
    assert rep["matched_all_vs_replay"] == 0


def test_unmatched_fills_classified_by_cause():
    """The leftover fills no tier could pair are labelled by cause, so
    the 'boundary/coverage, not price disagreement' reading is verified
    rather than inferred from the count gap. A fill within 60s of the
    window edge is `boundary`, one inside a coverage break is `gap`, and
    a mid-window fill with no gap is `unexplained` — the only class that
    would signal a hidden fill-model discrepancy."""
    anchor = datetime(2026, 7, 12, 1, 0, 0)
    end = anchor + timedelta(hours=2)
    boundary = _RF(5.0, end - timedelta(seconds=10))  # near the window edge
    gap = _RF(5.0, anchor + timedelta(minutes=30))  # inside a coverage break
    lonely = _RF(5.0, anchor + timedelta(minutes=60), market_id="M2")  # clear window
    gaps = [(anchor + timedelta(minutes=29), anchor + timedelta(minutes=31))]

    rep = compare([], [boundary, gap, lonely], anchor=anchor, end=end, gaps=gaps)

    assert rep["unmatched_replay"] == 3
    assert rep["unmatched_replay_by_cause"] == {
        "boundary": 1,
        "gap": 1,
        "reseed_twin": 0,
        "unexplained": 1,
    }
    assert rep["unmatched_shadow"] == 0
    samples = rep["unmatched_unexplained_samples"]
    assert len(samples) == 1 and samples[0]["market"] == "M2"


def test_unmatched_fill_with_identical_twin_is_reseed_twin():
    """A leftover fill whose exact (market, side, qty, price) also occurs
    in the opposite stream — just time-shifted beyond the match window —
    is `reseed_twin`, the start-of-run seed-settling signature, NOT
    `unexplained`. This is the real cause of the 07-13 shadow run's
    leftover fills (probed: same qty/price fills fire minutes apart while
    the seeded books converge), which the first classifier lumped into
    `unexplained`."""
    anchor = datetime(2026, 7, 12, 1, 0, 0)
    end = anchor + timedelta(hours=2)
    # Same market/side/qty/price, 5 min apart: past the 60s exact and 2s
    # nearest windows, both mid-window (neither boundary nor gap).
    t = anchor + timedelta(minutes=40)
    shadow = [_shadow(5.0, t)]
    replay = [_RF(5.0, t + timedelta(minutes=5))]
    rep = compare(shadow, replay, anchor=anchor, end=end, gaps=[])
    assert rep["matched"] == 0
    assert rep["unmatched_shadow_by_cause"]["reseed_twin"] == 1
    assert rep["unmatched_replay_by_cause"]["reseed_twin"] == 1
    assert rep["unmatched_shadow_by_cause"]["unexplained"] == 0
    assert rep["unmatched_unexplained_samples"] == []


def test_unmatched_fill_without_twin_stays_unexplained():
    """A leftover with a DIFFERENT price than anything opposite has no
    twin and remains `unexplained` — the twin refinement never rescues a
    genuine fill-model residual."""
    anchor = datetime(2026, 7, 12, 1, 0, 0)
    end = anchor + timedelta(hours=2)
    t = anchor + timedelta(minutes=40)
    shadow = [_shadow(5.0, t, price=0.40)]
    replay = [_RF(5.0, t + timedelta(minutes=5), price=0.55)]  # different level
    rep = compare(shadow, replay, anchor=anchor, end=end, gaps=[])
    assert rep["unmatched_shadow_by_cause"]["reseed_twin"] == 0
    assert rep["unmatched_shadow_by_cause"]["unexplained"] == 1
    assert rep["unmatched_replay_by_cause"]["unexplained"] == 1


def test_unmatched_without_context_defaults_to_unexplained():
    """Called context-free (the many unit tests do this), an unpaired
    fill can't be excused as boundary/gap and is honestly `unexplained`
    — a nonzero count is never silently absorbed."""
    rep = compare([_shadow(5.0, T)], [_RF(4.0, T + timedelta(seconds=10))])
    assert rep["unmatched_shadow"] == 1
    assert rep["unmatched_replay"] == 1
    assert rep["unmatched_shadow_by_cause"]["unexplained"] == 1
    assert rep["unmatched_replay_by_cause"]["unexplained"] == 1


def test_shuffled_input_order_produces_identical_report():
    """Determinism: the report is a pure function of the fill sets,
    not of their arrival order."""
    import random

    shadow = [
        _shadow(5.0, T),  # exact pair
        _shadow(4.0, T + timedelta(minutes=5)),  # nearest pair (qty differs)
        _shadow(6.0, T + timedelta(minutes=10)),  # split single
        _shadow(9.0, T + timedelta(minutes=20), market_id="M2", side="no"),  # unmatched
    ]
    replay = [
        _RF(5.0, T + timedelta(milliseconds=100)),
        _RF(3.0, T + timedelta(minutes=5, milliseconds=300)),
        _RF(2.0, T + timedelta(minutes=10)),
        _RF(4.0, T + timedelta(minutes=10, milliseconds=400)),
    ]
    baseline = compare(shadow, replay)
    rng = random.Random(42)
    for _ in range(5):
        s, r = shadow[:], replay[:]
        rng.shuffle(s)
        rng.shuffle(r)
        assert compare(s, r) == baseline


def _ev(market_id, seq, recv_ts, price):
    from hyxlab.streamstore import BookEvent

    return BookEvent(
        venue="kalshi",
        market_id=market_id,
        recv_ts=recv_ts,
        src_ts=None,
        sid=1,
        seq=seq,
        kind="snap",
        side="yes",
        price=price,
        qty=10.0,
    )


def _seeded_stream(tmp_path):
    """A stream spanning many slice widths, with recv_ts ties that would
    straddle a slice boundary if the walk were not half-open."""
    from hyxlab.streamstore import StreamStore

    db = tmp_path / "slices.duckdb"
    sstore = StreamStore(db)
    events, base = [], datetime(2026, 8, 10, 0, 0, 0)
    stamps = [base + timedelta(hours=h) for h in range(48)]
    # The walk starts at `first - 1us`, so its 6h boundaries land on
    # `base + k*6h - 1us`. Put a tie group on each of those instants:
    # without a half-open bound those rows appear in BOTH neighbouring
    # slices, and nothing else in this fixture would notice.
    stamps += [base + timedelta(hours=6 * k) - timedelta(microseconds=1) for k in range(1, 8)]
    for i, ts in enumerate(sorted(stamps)):
        # DESCENDING seq within each tie group: insertion order is the
        # reverse of replay order, so a walk that forgets `ORDER BY seq`
        # cannot pass by accident.
        for seq in (2, 1, 0):
            events.append(_ev("M1", i * 10 + seq, ts, 0.40 + seq / 100))
    sstore.append_events(events)
    sstore.flush()
    return db, len(stamps) * 3


def test_sliced_event_walk_is_byte_identical_to_one_cursor(tmp_path):
    """Slicing the replay window is a memory fix, not a semantic one:
    the event sequence must be indistinguishable from the single-cursor
    walk, including `recv_ts` ties that land on a slice boundary."""
    import duckdb

    from simulator.bookreplay import stream_events as _events

    db, n = _seeded_stream(tmp_path)
    lo, hi = datetime(2026, 8, 9), datetime(2026, 8, 13)
    with duckdb.connect(str(db), read_only=True) as conn:
        one = list(_events(conn, lo, hi, slice_hours=10_000))  # one slice
        many = list(_events(conn, lo, hi, slice_hours=6.0))
        finer = list(_events(conn, lo, hi, slice_hours=0.25))

    assert len(one) == n
    assert many == one
    assert finer == one
    # The ordering the replay depends on is actually exercised.
    assert [(e.recv_ts, e.seq) for e in one] == sorted((e.recv_ts, e.seq) for e in one)


def test_sliced_walk_handles_open_and_unbounded_edges(tmp_path):
    """`lo` is `datetime.min` when no coverage break precedes the anchor
    and `hi` may be open; neither may make the walk iterate empty
    millennia or drop rows."""
    import duckdb

    from simulator.bookreplay import stream_events as _events

    db, n = _seeded_stream(tmp_path)
    with duckdb.connect(str(db), read_only=True) as conn:
        from_min = list(_events(conn, datetime.min, None, slice_hours=6.0))
        empty = list(_events(conn, datetime(2027, 1, 1), None, slice_hours=6.0))

    assert len(from_min) == n
    assert empty == []


def test_lo_inclusive_keeps_the_row_at_the_bound(tmp_path):
    """`lo_inclusive` is for SEED callers: the floor they pass is a gap's
    `ended_at`, and `streamd` stamps a seq_reset gap's end with the
    recv_ts of the first post-reset frame — the reconnect image that
    re-seeds the book. The default half-open `(lo, hi]` drops every row of
    that image (they share one recv_ts), so a seed from a raw floor
    replays a hole the daemon never had.

    The fixture's tie groups are three rows deep, so the shift must
    recover the WHOLE group, not just one row. `datetime.min` — the
    no-prior-break sentinel — has no predecessor and must be left alone
    rather than stepped back into an OverflowError."""
    import duckdb

    from simulator.bookreplay import stream_events as _events

    db, n = _seeded_stream(tmp_path)
    with duckdb.connect(str(db), read_only=True) as conn:
        first = conn.execute("SELECT min(recv_ts) FROM book_events").fetchone()[0]
        excl = list(_events(conn, first, None))
        incl = list(_events(conn, first, None, lo_inclusive=True))
        sentinel = list(_events(conn, datetime.min, None, lo_inclusive=True))

    assert len(excl) == n - 3  # the bound's whole tie group is dropped
    assert len(incl) == n
    assert incl[0].recv_ts == first
    assert excl == incl[3:]  # nothing else moved
    assert len(sentinel) == n  # the sentinel is already inclusive of everything


def test_every_seed_site_asks_for_an_inclusive_floor(tmp_path):
    """Coupling guard. `simulator.divergence` documents its seed as
    replaying history "exactly as shadow does", and the two drifting apart
    at the boundary is exactly the class of bug that made
    `stream_events` THE ONE walk in the first place. Each module seeds
    once, and each seed passes `lo_inclusive=True`; the report walks that
    follow it must NOT (a report's `lo` is an anchor, not a gap end).

    THREE modules, not two. This guard shipped naming shadow and
    divergence as "both" seed sites while `simulator.run_l2` — which
    documents itself as using "the divergence runner's exact seeding
    discipline" — seeded from the same gap floor, exclusively, and kept
    the hole for two rungs (EXP-1378). "Both" was a count of the sites
    the fix had touched, not of the sites that seed; mistake #37 again.
    Sweep by ROLE.

    TWO walk entry points, for the same reason there are three modules.
    `simulator.divergence` now opens its window with `export_events`
    (the walk written to parquet, so the archive is released before the
    replay) and this guard, keyed on the NAME `stream_events`, went from
    checking divergence's seed to checking nothing — silently, because
    `assert calls` is the only thing between "seeds inclusively" and
    "does not seed here at all". Both entry points take the same
    `lo_inclusive`, so the guard is keyed on the argument's role, not on
    which of the two carries it."""
    import ast
    from pathlib import Path

    walks = {"stream_events", "export_events"}
    for module in ("simulator/shadow.py", "simulator/divergence.py", "simulator/run_l2.py"):
        tree = ast.parse(Path(module).read_text())
        calls = [
            c
            for c in ast.walk(tree)
            if isinstance(c, ast.Call)
            and getattr(c.func, "id", getattr(c.func, "attr", None)) in walks
        ]
        inclusive = [c for c in calls if any(k.arg == "lo_inclusive" for k in c.keywords)]
        assert calls, f"{module} no longer calls the one walk"
        assert len(inclusive) == 1, f"{module} seeds {len(inclusive)} times, expected 1"


def test_replay_bounds_in_memory_equity_curve(tmp_path, monkeypatch):
    """The offline replay is the SECOND site of the shadow OOM fix
    (simulator/shadow.py, mid-run kill 2026-07-18): one equity point per
    snapshot, over a window as long as the longest shadow run. Divergence
    compares fills and never calls `finalize()`, so the curve is pure
    ballast — at most one point may survive, and the fills must not move.
    """
    stream_db = tmp_path / "stream.duckdb"
    archive_db = tmp_path / "archive.duckdb"

    store = Store(archive_db)
    store.upsert_markets([MarketInfo(venue="kalshi", market_id="M1")])
    store.close()

    sstore = StreamStore(stream_db)
    sstore.append_events(_snapshot_frame("M1", 1, 40, 59, T0))
    # Prices must MOVE: the replayer emits a snapshot only when the book
    # changes, so a repeated quote would collapse to one step and make
    # the bound vacuous.
    for seq, minutes in enumerate([11, 22, 33, 44, 55], start=2):
        sstore.append_events(
            _snapshot_frame("M1", seq, 40 + seq, 55, T0 + timedelta(minutes=minutes))
        )
    sstore.flush()

    carried, real_step = [], Simulator.step

    def spy(self, snap):
        # Length the sim CARRIED INTO this step — i.e. what survived the
        # previous step's trim. Unbounded growth shows up here.
        carried.append(len(self.result.equity_curve))
        real_step(self, snap)

    monkeypatch.setattr(Simulator, "step", spy)

    def run():
        return replay_run(
            "R1",
            T0.replace(tzinfo=None),
            (T0 + timedelta(hours=2)).replace(tzinfo=None),
            latency=0.0,
            strategy_names=["probe"],
            stream_db=str(stream_db),
            archive_db=str(archive_db),
        )

    fills = run()
    assert len(carried) > 3, "replay must actually step the sim, or the bound is vacuous"
    assert max(carried) <= 1
    assert len(fills) > 0  # the comparison the report makes still happens
    # That the trim changes no number the report reads is pinned by
    # `test_replay_reproduces_shadow_run_exactly`, which runs this same
    # path and asserts the fills match shadow's exactly.


def test_sliced_walk_applies_market_prefix_on_every_slice(tmp_path):
    """`run_l2` walks with `--markets PREFIX`. The filter must survive
    into each slice's own query — and must not depend on `hi` being
    given, which an earlier positional-slice of the params list did.
    """
    import duckdb

    from hyxlab.streamstore import StreamStore
    from simulator.bookreplay import stream_events

    db = tmp_path / "prefixed.duckdb"
    sstore = StreamStore(db)
    base = datetime(2026, 8, 10, 0, 0, 0)
    sstore.append_events(
        [
            _ev(m, seq, base + timedelta(hours=h), 0.40)
            for h in range(24)
            for seq, m in enumerate(("KXAAA-1", "KXBBB-1"))
        ]
    )
    sstore.flush()

    lo = datetime(2026, 8, 9)
    with duckdb.connect(str(db), read_only=True) as conn:
        bounded = list(stream_events(conn, lo, datetime(2026, 8, 12), prefix="KXAAA"))
        unbounded = list(stream_events(conn, lo, None, prefix="KXAAA"))
        everything = list(stream_events(conn, lo, None))

    assert len(everything) == 48  # both markets present, so the filter is non-vacuous
    assert len(bounded) == 24
    assert {e.market_id for e in bounded} == {"KXAAA-1"}
    # The open-`hi` form must filter identically, not fall back to "all".
    assert unbounded == bounded


def test_replay_survives_a_transient_archive_lock(tmp_path, monkeypatch):
    """Regression (2026-08-25 08:58Z): `replay_run` attached the LIVE
    archive with a bare read-only `Store` and died on the first collision
    with a writer — the report was killed by `collector.poly_sweep`
    holding the lock. The attach must ride out a transient collision."""
    import hyxlab.store as store_mod

    stream_db = tmp_path / "stream.duckdb"
    archive_db = tmp_path / "archive.duckdb"

    store = Store(archive_db)
    store.upsert_markets([MarketInfo(venue="kalshi", market_id="M1")])
    store.close()

    sstore = StreamStore(stream_db)
    sstore.append_events(_snapshot_frame("M1", 1, 40, 59, T0))
    # One-tick spreads past the probe cooldown, prices moving so the
    # replayer cannot collapse them: the probe must actually trade, or
    # "the replay survived" would be vacuous.
    for seq, bid, ask_no, minutes in [(2, 44, 55, 11), (3, 45, 54, 22)]:
        sstore.append_events(
            _snapshot_frame("M1", seq, bid, ask_no, T0 + timedelta(minutes=minutes))
        )
    sstore.flush()

    real_connect, attempts = store_mod.duckdb.connect, []

    def flaky(path, **kw):
        # Only the archive collides; the stream attach is a separate lock.
        if "archive" in str(path):
            attempts.append(path)
            if len(attempts) <= 2:
                raise store_mod.duckdb.Error("Conflicting lock is held")
        return real_connect(path, **kw)

    monkeypatch.setattr(store_mod.duckdb, "connect", flaky)
    monkeypatch.setattr("time.sleep", lambda _s: None)  # no wall-clock cost

    fills = replay_run(
        "R1",
        T0.replace(tzinfo=None),
        (T0 + timedelta(hours=2)).replace(tzinfo=None),
        latency=0.0,
        strategy_names=["probe"],
        stream_db=str(stream_db),
        archive_db=str(archive_db),
    )
    assert len(attempts) == 3, "the attach must have been retried, not merely lucky"
    assert len(fills) > 0


def test_replay_loads_only_markets_its_window_can_touch(tmp_path, monkeypatch):
    """EXP-1379, the graduation of `simulator/divergence.py` off
    `UNBOUNDED_ONE_SHOTS`. This report holds the metadata table LONGEST
    of the five one-shots — a 10.5-day replay of a shadow run — and the
    table is 1.87M MarketInfo objects / 1.32 GiB, sized by the ARCHIVE
    rather than by the window asked for. Its reachable set is exactly
    the ids `book_events` carries over [seed floor, end], so:

      * a market the archive knows and the window never sees must NOT
        reach the Simulator, and
      * the bound must run to `end`, not to `anchor` — the traded window
        starts where the seed stops, so an `anchor` upper bound would
        drop metadata for every market that first appears after the
        anchor, which is most of a 10.5-day run.
    """
    import simulator.divergence as mod

    stream_db = tmp_path / "stream.duckdb"
    archive_db = tmp_path / "archive.duckdb"

    store = Store(archive_db)
    store.upsert_markets(
        [
            MarketInfo(venue="kalshi", market_id="M0", title="entirely pre-floor"),
            MarketInfo(venue="kalshi", market_id="M1", title="seeded"),
            MarketInfo(venue="kalshi", market_id="M4", title="only the reconnect image"),
            MarketInfo(venue="kalshi", market_id="M2", title="post-anchor"),
            MarketInfo(venue="kalshi", market_id="M3", title="never in this window"),
        ]
    )
    store.close()

    sstore = StreamStore(stream_db)
    # A real seed floor, so the lower bound is exercised rather than
    # collapsing to `datetime.min`: a books gap whose `ended_at` is the
    # recv_ts of the reconnect image that re-seeds M1 (rung-11).
    sstore.append_gap("kalshi", "books", T0 - timedelta(hours=1), T0, "seq_reset")
    # M0 lives entirely BEFORE the floor: known to the archive,
    # unreachable by this replay.
    sstore.append_events(_snapshot_frame("M0", 1, 40, 59, T0 - timedelta(hours=2)))
    # M1 and M4 share the reconnect image's recv_ts; M4 has NO other
    # event, so an exclusive floor loses it and only it.
    sstore.append_events(_snapshot_frame("M1", 1, 40, 59, T0))
    sstore.append_events(_snapshot_frame("M4", 1, 40, 59, T0))
    for seq, bid, ask_no, minutes in [(2, 44, 55, 11), (3, 45, 54, 22)]:
        sstore.append_events(
            _snapshot_frame("M1", seq, bid, ask_no, T0 + timedelta(minutes=minutes))
        )
    # M2 exists only INSIDE the traded window; M3 only far past `end`.
    sstore.append_events(_snapshot_frame("M2", 1, 44, 55, T0 + timedelta(minutes=30)))
    sstore.append_events(_snapshot_frame("M3", 1, 44, 55, T0 + timedelta(hours=9)))
    sstore.flush()

    seen, real = {}, mod.Simulator

    def spy(markets, *a, **kw):
        seen["markets"] = markets
        return real(markets, *a, **kw)

    monkeypatch.setattr(mod, "Simulator", spy)
    fills = mod.replay_run(
        "R1",
        T0.replace(tzinfo=None),
        (T0 + timedelta(hours=2)).replace(tzinfo=None),
        latency=0.0,
        strategy_names=["probe"],
        stream_db=str(stream_db),
        archive_db=str(archive_db),
    )
    assert fills, "the replay must still trade, or the bound is vacuous"
    assert set(seen["markets"]) == {("kalshi", "M1"), ("kalshi", "M2"), ("kalshi", "M4")}
    # Named, so a regression to `anchor` reads as itself rather than as
    # a set mismatch.
    assert ("kalshi", "M2") in seen["markets"], "the bound must run to `end`, not `anchor`"
    assert ("kalshi", "M4") in seen["markets"], "the floor must INCLUDE the reconnect image"
    assert seen["markets"][("kalshi", "M1")].title == "seeded"  # real rows, not stubs


def test_a_window_with_no_events_loads_no_metadata(tmp_path, monkeypatch):
    """The empty id set is a real bound. "No ids" degrading to "all
    markets" — `market_ids=ids or None` — is the one-character mutation
    that silently restores the 1.32 GiB at exactly the moment the
    operator asked for the cheapest possible replay, and it is invisible
    to every test whose window happens to contain events."""
    import simulator.divergence as mod

    stream_db = tmp_path / "stream.duckdb"
    archive_db = tmp_path / "archive.duckdb"

    store = Store(archive_db)
    store.upsert_markets([MarketInfo(venue="kalshi", market_id=f"M{i}") for i in range(4)])
    store.close()

    sstore = StreamStore(stream_db)
    # Every event is past `end`, so the replay's reachable set is empty
    # while the archive's metadata table is not.
    sstore.append_events(_snapshot_frame("M1", 1, 40, 59, T0 + timedelta(hours=9)))
    sstore.flush()

    seen, real = {}, mod.Simulator

    def spy(markets, *a, **kw):
        seen["markets"] = markets
        return real(markets, *a, **kw)

    monkeypatch.setattr(mod, "Simulator", spy)
    fills = mod.replay_run(
        "R1",
        T0.replace(tzinfo=None),
        (T0 + timedelta(hours=2)).replace(tzinfo=None),
        latency=0.0,
        strategy_names=["probe"],
        stream_db=str(stream_db),
        archive_db=str(archive_db),
    )
    assert fills == []
    assert seen["markets"] == {}


def _runs_db(tmp_path, runs):
    """(run_id, started_at, n_fills) -> a shadow db with just those facts.

    Schema comes from `ShadowLedger`, not a hand-copy: the selection is
    read off real columns, so a schema change must reach these tests.
    """
    db = tmp_path / f"runs{len(list(tmp_path.iterdir()))}" / "runs.duckdb"
    ShadowLedger(db)
    with duckdb.connect(str(db)) as conn:
        for run_id, started_at, n_fills in runs:
            conn.execute(
                "INSERT INTO shadow_runs VALUES (?,?,2.0,'probe',?)",
                [run_id, started_at, started_at],
            )
            conn.execute(
                "INSERT INTO shadow_fills SELECT ?,'probe','kalshi','M1','yes',1,0.5,0,false,"
                " ? + to_seconds(CAST(i AS BIGINT)) FROM range(?) t(i)",
                [run_id, started_at, n_fills],
            )
    return db


def _pick(tmp_path, runs):
    """A fresh db per call — the real schema makes run_id a primary key,
    so two scenarios in one test must not share a file."""
    db = _runs_db(tmp_path, runs)
    with duckdb.connect(str(db), read_only=True) as conn:
        return latest_complete_run(conn)


def test_default_run_is_the_newest_finished_one_not_the_biggest(tmp_path):
    # The 2026-09-09 shape: a huge old run that had held the default for
    # three weeks, and a later, smaller, never-reported one.
    assert (
        _pick(
            tmp_path,
            [
                ("big_old", datetime(2026, 8, 10), 54007),
                ("newer_smaller", datetime(2026, 8, 29), 38143),
                ("live", datetime(2026, 9, 7), 9882),
            ],
        )
        == "newer_smaller"
    )


def test_the_live_run_is_never_the_default_even_when_it_is_the_biggest(tmp_path):
    # A running daemon always owns max(started_at); replaying it would
    # race a moving `end` against an archive being written at that bound.
    assert (
        _pick(
            tmp_path,
            [
                ("finished", datetime(2026, 8, 29), 10),
                ("live", datetime(2026, 9, 7), 999999),
            ],
        )
        == "finished"
    )


def test_a_finished_run_with_no_fills_is_skipped_not_reported_as_zero(tmp_path):
    # Two probe restarts that traded nothing sit between the real runs
    # (20260826T0824xx in the live record). Zero fills is no measurement.
    assert (
        _pick(
            tmp_path,
            [
                ("real", datetime(2026, 8, 23), 14121),
                ("empty_a", datetime(2026, 8, 26, 8, 24, 31), 0),
                ("empty_b", datetime(2026, 8, 26, 8, 24, 52), 0),
                ("live", datetime(2026, 9, 7), 5),
            ],
        )
        == "real"
    )


def test_no_selectable_run_returns_none_rather_than_a_wrong_one(tmp_path):
    assert _pick(tmp_path, [("only_live", datetime(2026, 9, 7), 500)]) is None
    assert _pick(tmp_path, []) is None


def test_default_selection_advances_when_a_newer_run_finishes(tmp_path):
    # The property the old argmax lacked: re-running the report after new
    # evidence accumulates must measure the NEW evidence.
    before = [("a", datetime(2026, 8, 10), 54007), ("b", datetime(2026, 8, 29), 100)]
    after = before + [("c", datetime(2026, 9, 7), 50)]
    assert _pick(tmp_path, before) == "a"
    assert _pick(tmp_path, after) == "b"


def _main_with(monkeypatch, argv, replay=None):
    """Run `divergence.main()` with argv, counting replays."""
    calls = []

    def _replay(*a, **k):
        calls.append(a)
        return (replay if replay is not None else [], [])

    monkeypatch.setattr(mod, "replay_run", _replay)
    monkeypatch.setattr(sys, "argv", ["divergence", *argv])
    mod.main()
    return calls


def _stamped(sha):
    return json.dumps({"run_id": "done", "report_code": {"root": mod.REPORT_CODE_ROOT, "sha": sha}})


def test_if_new_skips_the_replay_when_the_run_is_reported_BY_THIS_CODE(tmp_path, monkeypatch):
    """What the daily timer runs on a day nothing changed.

    The subject only advances when the shadow daemon restarts, so on
    almost every day the report already exists. A daily unit that
    re-derived it anyway would burn 9 minutes and 1.9G beside the live
    capture daemons to rewrite a file it already had.
    """
    db = _runs_db(tmp_path, [("done", datetime(2026, 8, 29), 5), ("live", datetime(2026, 9, 7), 1)])
    out = tmp_path / "reports"
    out.mkdir()
    same = _stamped(mod.report_code()["sha"])
    (out / "done.json").write_text(same)
    calls = _main_with(monkeypatch, ["--if-new", "--shadow-db", str(db), "--out", str(out)])
    assert calls == []
    assert (out / "done.json").read_text() == same  # untouched, not rewritten


def test_if_new_re_derives_when_the_report_was_made_by_DIFFERENT_code(tmp_path, monkeypatch):
    """The whole of mistakes #76.

    Keyed on run_id alone, this branch printed `nothing to do` for ten
    consecutive days (journal 09-12 -> 09-21) while four passes shipped
    `price_delta_median` (#66), `nearest_unpaired_dt` (#68),
    `nearest_qty_delta` (#69) and `attach_wait` (#70/#71) into this
    report — none of which ever reached an artifact, because the only
    thing that advances run_id is a shadow-daemon restart.
    """
    db = _runs_db(tmp_path, [("done", datetime(2026, 8, 29), 5), ("live", datetime(2026, 9, 7), 1)])
    out = tmp_path / "reports"
    out.mkdir()
    (out / "done.json").write_text(_stamped("0" * 64))
    calls = _main_with(monkeypatch, ["--if-new", "--shadow-db", str(db), "--out", str(out)])
    assert len(calls) == 1
    fresh = json.loads((out / "done.json").read_text())
    assert fresh["report_code"]["sha"] == mod.report_code()["sha"]


def test_if_new_re_derives_an_UNSTAMPED_report(tmp_path, monkeypatch):
    """Every one of the 8 reports in the archive on 2026-09-22 predates the
    stamp. `no stamp` is `cannot prove it was made by today's code`, and an
    unknown must not take the cheap branch (mistakes #74)."""
    db = _runs_db(tmp_path, [("done", datetime(2026, 8, 29), 5), ("live", datetime(2026, 9, 7), 1)])
    out = tmp_path / "reports"
    out.mkdir()
    (out / "done.json").write_text("{}")
    assert (
        len(_main_with(monkeypatch, ["--if-new", "--shadow-db", str(db), "--out", str(out)])) == 1
    )


def test_the_stamp_covers_the_whole_closure_not_just_this_file(tmp_path):
    """`attach_wait` was added in `hyxlab/store.py`, not here. A stamp over
    the root file alone would have answered `same code` for it."""
    files, _lazy = importclosure.closure(mod.REPORT_CODE_ROOT)
    assert "hyxlab/store.py" in files and "simulator/divergence.py" in files
    assert mod.report_code()["files"] == len(files)


def test_the_stamp_moves_when_any_closure_file_moves(tmp_path):
    """Read off nothing: copy the repo, edit one non-root closure file, and
    the sha must differ. A stamp that does not move is a stamp that cannot
    trip the re-derive it exists to trip."""
    repo = tmp_path / "repo"
    shutil.copytree(importclosure.REPO_ROOT / "hyxlab", repo / "hyxlab")
    shutil.copytree(importclosure.REPO_ROOT / "simulator", repo / "simulator")
    shutil.copytree(importclosure.REPO_ROOT / "strategies", repo / "strategies")
    before = importclosure.closure_sha(mod.REPORT_CODE_ROOT, repo)["sha"]
    store = repo / "hyxlab" / "store.py"
    store.write_text(store.read_text() + "\n# one comment\n")
    after = importclosure.closure_sha(mod.REPORT_CODE_ROOT, repo)["sha"]
    assert before != after


def test_if_new_still_measures_a_run_that_has_no_report(tmp_path, monkeypatch):
    db = _runs_db(tmp_path, [("done", datetime(2026, 8, 29), 5), ("live", datetime(2026, 9, 7), 1)])
    out = tmp_path / "reports"
    calls = _main_with(monkeypatch, ["--if-new", "--shadow-db", str(db), "--out", str(out)])
    assert len(calls) == 1
    assert json.loads((out / "done.json").read_text())["run_id"] == "done"


def test_without_if_new_the_report_is_recomputed(tmp_path, monkeypatch):
    """The flag is opt-in: a hand run must still be able to refresh a
    report whose inputs changed (a late-landing archive backfill)."""
    db = _runs_db(tmp_path, [("done", datetime(2026, 8, 29), 5), ("live", datetime(2026, 9, 7), 1)])
    out = tmp_path / "reports"
    out.mkdir()
    (out / "done.json").write_text("{}")
    calls = _main_with(monkeypatch, ["--shadow-db", str(db), "--out", str(out)])
    assert len(calls) == 1
    assert json.loads((out / "done.json").read_text())["run_id"] == "done"


def test_unpaired_dt_census_reports_a_counterpart_beyond_the_nearest_window():
    """The window's own evidence must be able to fail. `nearest_dt_abs_mean_s`
    is averaged over pairs the window already admitted, so its maximum IS the
    window edge whatever the data does; the census counts, for every fill no
    tier could pair, how far away its nearest same-price counterpart actually
    sits — with no window bound (mistakes #67)."""
    rep = compare([_shadow(5.0, T)], [_RF(4.0, T + timedelta(seconds=5))])
    assert rep["matched_nearest"] == 0  # 5s > the 2s window
    assert rep["nearest_dt_abs_mean_s"] is None  # conditioned sample is empty

    census = rep["nearest_unpaired_dt"]
    assert census["n"] == 2  # one entry per unpaired fill, both directions
    assert census["no_counterpart"] == 0
    assert census["min_s"] == census["max_s"] == 5.0
    assert census["over_s"]["2"] == 2  # past the window, and visible
    assert census["over_s"]["20"] == 0


def test_conditioned_nearest_mean_stays_inside_the_window_the_census_does_not():
    """One pairing inside the window and one 30s outside it: the shipped mean
    reports only the admitted pair (and so reads reassuringly small), while the
    census carries the 30s tail that sizes whether 2s is generous."""
    shadow = [_shadow(5.0, T), _shadow(7.0, T + timedelta(minutes=10), price=0.3)]
    replay = [
        _RF(4.0, T + timedelta(milliseconds=300)),
        _RF(6.0, T + timedelta(minutes=10, seconds=30), price=0.3),
    ]
    rep = compare(shadow, replay)
    assert rep["matched_nearest"] == 1
    assert rep["nearest_dt_abs_mean_s"] == 0.3
    assert rep["nearest_unpaired_dt"]["max_s"] == 30.0
    assert rep["nearest_unpaired_dt"]["over_s"]["2"] == 2


def test_unpaired_fill_with_no_same_price_counterpart_is_counted_separately():
    """A leftover with no same-price counterpart at any dt is not a window
    problem at all — it is excluded from the dt census and counted, so a small
    `n` can never be read as a small tail."""
    rep = compare([_shadow(5.0, T, price=0.4)], [_RF(4.0, T + timedelta(seconds=5), price=0.9)])
    census = rep["nearest_unpaired_dt"]
    assert census["n"] == 0
    assert census["no_counterpart"] == 2
    assert census["max_s"] is None
    assert census["over_s"]["2"] == 0


def test_unpaired_dt_census_ignores_counterparts_another_fill_already_claimed():
    """The census asks what a WIDER window could have paired, so it searches
    only the opposite stream's still-unpaired fills — narrower than
    `reseed_twin`, which tests existence against every fill including matched
    ones. A leftover whose only price-twin is already spoken for counts as
    `no_counterpart`, not as a window near-miss."""
    shadow = [_shadow(5.0, T), _shadow(7.0, T + timedelta(hours=1))]
    rep = compare(shadow, [_RF(5.0, T)])  # the 5.0 pair matches at the exact tier
    assert rep["matched"] == 1
    assert rep["unmatched_shadow"] == 1  # the 7.0 fill, same price as the twin
    census = rep["nearest_unpaired_dt"]
    assert census["n"] == 0
    assert census["no_counterpart"] == 1


def test_nearest_tier_publishes_the_qty_gap_it_absorbs():
    """The nearest tier's price delta is 0 by selection and its |dt| is
    bounded by the window, so without a qty census a nearest match reads in
    the report exactly like an exact one while standing for a strictly weaker
    agreement. Shadow 5 vs replay 4 at one price, 300ms apart: the report
    must say the size gap is -1."""
    rep = compare([_shadow(5.0, T)], [_RF(4.0, T + timedelta(milliseconds=300))])
    assert rep["matched_nearest"] == 1
    assert rep["price_delta_abs_mean_nearest"] == 0.0  # says nothing
    census = rep["nearest_qty_delta"]
    assert census["n"] == 1
    assert census["equal_qty"] == 0
    assert census["mean"] == -1.0  # replay - shadow
    assert census["abs_mean"] == 1.0
    assert census["min"] == census["median"] == census["max"] == -1.0


def test_nearest_qty_census_median_straddles_evenly():
    """Two pairs, gaps -1 and +3: the median is the midpoint, not the upper
    straddler (mistakes #66)."""
    shadow = [_shadow(5.0, T), _shadow(2.0, T + timedelta(minutes=10), price=0.3)]
    replay = [
        _RF(4.0, T + timedelta(milliseconds=300)),
        _RF(5.0, T + timedelta(minutes=10, milliseconds=300), price=0.3),
    ]
    rep = compare(shadow, replay)
    assert rep["matched_nearest"] == 2
    census = rep["nearest_qty_delta"]
    assert census["min"] == -1.0
    assert census["max"] == 3.0
    assert census["median"] == 1.0
    assert census["mean"] == 1.0


def test_nearest_tier_can_only_ever_pair_fills_that_disagree_on_qty():
    """The structural claim behind the census, asserted against the real
    matcher rather than argued in prose. The exact tier holds ONE candidate
    list per key across its whole greedy pass and only ever pops matches from
    it, so every replay fill still in `r_left` was visible to every shadow
    fill that became a leftover; inside the nearest window (a subset of the
    60s MATCH_TOLERANCE) the only predicate that can have refused the pair is
    equal qty. Hence `equal_qty` is 0 on every reachable dataset -- which is
    also why `matched_nearest` reading 0 in all 13 archived reports is NOT
    dead code: it is the absence of same-price size disagreement inside 2s."""
    random = __import__("random").Random(11)
    fired = 0
    for _ in range(3000):

        def mk():
            return (
                random.choice([1.0, 2.0, 3.0, 5.0]),
                T + timedelta(milliseconds=random.randint(0, 4000)),
                random.choice([0.4, 0.5]),
            )

        shadow = [_shadow(q, t, price=p) for q, t, p in (mk() for _ in range(3))]
        replay = [_RF(q, t, price=p) for q, t, p in (mk() for _ in range(3))]
        census = compare(shadow, replay)["nearest_qty_delta"]
        assert census["equal_qty"] == 0, census
        fired += bool(census["n"])
    assert fired > 500, f"invariant never exercised: {fired} datasets reached the tier"


def test_widening_the_nearest_window_past_the_exact_tolerance_lapses_the_invariant():
    """`equal_qty` is not a tautology assertion -- it is the tripwire for the
    one configuration that breaks the proof. `--nearest-window` is settable,
    and above the exact tier's 60s tolerance the nearest tier starts claiming
    EQUAL-qty pairs the exact tier merely ran out of reach for, which is a
    different (and undeclared) claim than the one its docstring makes."""
    shadow = [_shadow(5.0, T)]
    replay = [_RF(5.0, T + timedelta(seconds=120))]
    assert compare(shadow, replay)["matched_nearest"] == 0  # 2s window: unreachable
    wide = compare(shadow, replay, window=timedelta(seconds=300))
    assert wide["matched_nearest"] == 1
    assert wide["nearest_qty_delta"]["equal_qty"] == 1
    assert wide["nearest_qty_delta"]["abs_mean"] == 0.0


# ---------------------------------------------------------------------------
# The copy-out: divergence must not hold the stream archive across its replay
# ---------------------------------------------------------------------------


def test_exported_window_replays_as_the_identical_event_stream(tmp_path):
    """`export_events` + `stream_exported` is `stream_events` written to
    disk, and "identical" has to mean the ROWS and the ORDER, not the
    count: the whole reason the copy-out is safe is that the slices are
    the walk's own slices, already sorted, so reading them back is a
    concatenation. Swept over the three argument shapes that change the
    walk's bounds — a prefix filter, an open `hi`, and an inclusive `lo`
    (the seed's) — because each resolves a different extent."""
    from simulator.bookreplay import export_events, stream_events, stream_exported

    db = tmp_path / "s.duckdb"
    sstore = StreamStore(db)
    # Appended in a scrambled order ON PURPOSE. The daemon appends in
    # arrival order and DuckDB scans in insertion order, so a fixture
    # written chronologically comes back sorted whether or not anything
    # sorted it -- and an export that dropped the walk's `ORDER BY`
    # passed this test before the scramble went in.
    for i in [(i * 17) % 40 for i in range(40)]:
        mkt = f"KXAAA-{i % 2}"
        sstore.append_events(
            _snapshot_frame(mkt, i + 1, 40 + i % 5, 59 - i % 5, T0 + timedelta(hours=i))
        )
    sstore.flush()

    lo = T0.replace(tzinfo=None) - timedelta(days=1)
    hi = T0.replace(tzinfo=None) + timedelta(hours=30)
    cases = [
        ("bounded", {"hi": hi}),
        ("open_hi", {"hi": None}),
        ("prefixed", {"hi": hi, "prefix": "KXAAA-1"}),
        ("seed", {"hi": hi, "lo_inclusive": True}),
        # An empty window must export nothing and read back as nothing,
        # not raise on a directory with no slices in it.
        ("empty", {"hi": lo + timedelta(seconds=1)}),
    ]
    seen_nonempty = 0
    for name, kw in cases:
        with duckdb.connect(str(db), read_only=True) as conn:
            live = list(stream_events(conn, lo, slice_hours=6.0, **kw))
            export_events(conn, lo, dest=tmp_path / name, slice_hours=6.0, **kw)
        assert list(stream_exported(tmp_path / name)) == live, name
        seen_nonempty += bool(live)
    assert seen_nonempty == 4, "the sweep must not be vacuous on four of five shapes"


def test_replay_releases_the_stream_archive_before_it_simulates(tmp_path, monkeypatch):
    """The 2026-09-25 incident, as a test. `replay_run` replayed through a
    live `fetchmany` cursor, and a streaming cursor cannot release the file
    it reads — so the report held `hyxstream.duckdb` for 45m45s while it
    SIMULATED, `collector.streamd` could not flush for 3,057s, and 387,856
    rows went to the torn-append sidecar.

    Proved from OUTSIDE the process, because that is where the victim is:
    a subprocess takes the archive read-write at the moment the simulation
    starts. It can only succeed if this process has already let go."""
    import subprocess

    stream_db = tmp_path / "stream.duckdb"
    archive_db = tmp_path / "archive.duckdb"

    store = Store(archive_db)
    store.upsert_markets([MarketInfo(venue="kalshi", market_id="M1")])
    store.close()

    sstore = StreamStore(stream_db)
    sstore.append_events(_snapshot_frame("M1", 1, 40, 59, T0))
    for seq, bid, ask_no, minutes in [(2, 44, 55, 11), (3, 45, 54, 22)]:
        sstore.append_events(
            _snapshot_frame("M1", seq, bid, ask_no, T0 + timedelta(minutes=minutes))
        )
    sstore.flush()

    real_replay, probes = mod.replay_snapshots, []

    def probing(*a, **kw):
        # Fires on the seed replay AND the traded replay: both run after
        # the release, and a fix that released only before the second
        # would still hold the archive across the seed's walk.
        probes.append(
            subprocess.run(
                [sys.executable, "-c", f"import duckdb; duckdb.connect({str(stream_db)!r})"],
                capture_output=True,
                timeout=60,
            ).returncode
        )
        return real_replay(*a, **kw)

    monkeypatch.setattr(mod, "replay_snapshots", probing)

    fills = replay_run(
        "R1",
        T0.replace(tzinfo=None),
        (T0 + timedelta(hours=2)).replace(tzinfo=None),
        latency=0.0,
        strategy_names=["probe"],
        stream_db=str(stream_db),
        archive_db=str(archive_db),
    )
    assert len(fills) > 0, "a replay that trades nothing proves nothing about its hold"
    assert len(probes) == 2, f"both replays must be probed, saw {len(probes)}"
    assert probes == [0, 0], (
        "a writer could not take hyxstream.duckdb while the replay ran:"
        f" returncodes {probes} — the archive is still held across the simulation"
    )


def test_the_export_lives_in_this_process_private_scratch_and_is_dropped(tmp_path, monkeypatch):
    """The claim `tests/test_owned_db_discipline.py` rests on, tested at
    the caller that makes it true. `stream_exported` creates a read-write
    DuckDB — allowed only because no second process can name the path —
    and the path is chosen HERE, not there. So: the export sits under
    `hyxlab.scratch`'s flock-owned `<db>.tmp/pid-<pid>` tree, and 0.89 GB
    of parquet per replay is not left behind."""
    from hyxlab.scratch import scratch_root

    stream_db = tmp_path / "stream.duckdb"
    archive_db = tmp_path / "archive.duckdb"

    store = Store(archive_db)
    store.upsert_markets([MarketInfo(venue="kalshi", market_id="M1")])
    store.close()

    sstore = StreamStore(stream_db)
    sstore.append_events(_snapshot_frame("M1", 1, 40, 59, T0))
    sstore.append_events(_snapshot_frame("M1", 2, 44, 55, T0 + timedelta(minutes=11)))
    sstore.flush()

    real_export, dirs = mod.export_events, []

    def spying(conn, lo, hi, dest, **kw):
        dirs.append(Path(dest))
        return real_export(conn, lo, hi, dest, **kw)

    monkeypatch.setattr(mod, "export_events", spying)

    replay_run(
        "R1",
        T0.replace(tzinfo=None),
        (T0 + timedelta(hours=2)).replace(tzinfo=None),
        latency=0.0,
        strategy_names=["probe"],
        stream_db=str(stream_db),
        archive_db=str(archive_db),
    )

    private = Path(scratch_root(stream_db)) / f"pid-{os.getpid()}"
    assert len(dirs) == 2, dirs
    for d in dirs:
        assert private in d.parents, f"{d} is not under this process's scratch {private}"
        assert not d.exists(), f"{d} survived the run — the export is not a cache"
