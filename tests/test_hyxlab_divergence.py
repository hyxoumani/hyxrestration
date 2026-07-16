"""Divergence report: offline replay of a shadow run over the same
recording must reproduce its fills exactly — the zero baseline that
makes nonzero divergence on real runs attributable to infrastructure
(late archive rows, gaps unknown live) rather than method noise."""

from datetime import datetime, timedelta

import duckdb

from hyxlab.models import MarketInfo
from hyxlab.store import Store
from hyxlab.streamstore import StreamStore
from simulator.divergence import compare, replay_run
from simulator.shadow import ShadowLedger, ShadowRunner
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
    assert rep["unmatched_replay_by_cause"] == {"boundary": 1, "gap": 1, "unexplained": 1}
    assert rep["unmatched_shadow"] == 0
    samples = rep["unmatched_unexplained_samples"]
    assert len(samples) == 1 and samples[0]["market"] == "M2"


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
