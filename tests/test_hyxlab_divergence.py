"""Divergence report: offline replay of a shadow run over the same
recording must reproduce its fills exactly — the zero baseline that
makes nonzero divergence on real runs attributable to infrastructure
(late archive rows, gaps unknown live) rather than method noise."""

from datetime import timedelta

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
