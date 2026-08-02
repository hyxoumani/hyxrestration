"""run_l2 one-command L2 backtest + shared strategy registry: registry
resolves every registered strategy (incl. hylshi_fade), run_l2 replays a
synthetic book_events archive end-to-end into a manifest + deterministic
fills, and shadow's default strategy is unchanged ('probe', no args)."""

import json
from datetime import UTC, datetime, timedelta

from hyxlab.models import MarketInfo
from hyxlab.store import Store
from hyxlab.streamstore import StreamStore
from simulator.registry import STRATEGIES, build
from simulator.run_l2 import run_l2
from simulator.strategy import Strategy
from strategies.hylshi_fade import HylshiFade
from strategies.probe import TightSpreadProbe
from tests.test_hyxlab_shadow import _snapshot_frame

T0 = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)


# -- registry -----------------------------------------------------------


def test_registry_resolves_all_strategies_including_hylshi_fade():
    assert "hylshi_fade" in STRATEGIES
    strats = build(sorted(STRATEGIES))
    for name, s in zip(sorted(STRATEGIES), strats):
        assert isinstance(s, Strategy)
        assert s.name == name  # registry key == strategy self-name
    assert isinstance(build(["hylshi_fade"])[0], HylshiFade)


def test_registry_unknown_name_fails_loud():
    import pytest

    with pytest.raises(KeyError, match="nope"):
        build(["nope"])


def test_divergence_uses_shared_registry():
    """Extract-refactor equivalence: divergence's STRATEGIES is the shared
    registry and still maps 'probe' to TightSpreadProbe."""
    from simulator.divergence import STRATEGIES as div_strategies

    assert div_strategies is STRATEGIES
    assert div_strategies["probe"] is TightSpreadProbe


# -- shadow default unchanged ------------------------------------------


def test_shadow_default_strategy_is_probe_with_no_args():
    """The deployed unit passes no args: parsing [] must yield the exact
    pre-flag behavior — a single TightSpreadProbe at latency 2.0."""
    from simulator.shadow import build_parser

    args = build_parser().parse_args([])
    assert args.strategy == "probe"
    assert args.latency == 2.0
    strats = build(args.strategy.split(","))
    assert len(strats) == 1 and isinstance(strats[0], TightSpreadProbe)


def test_shadow_strategy_flag_resolves_hylshi_fade():
    from simulator.shadow import build_parser

    args = build_parser().parse_args(["--strategy", "hylshi_fade"])
    assert isinstance(build(args.strategy.split(","))[0], HylshiFade)


# -- run_l2 end-to-end --------------------------------------------------


def _build_archives(tmp_path):
    """Archive: two weather markets closing T0+5h (inside the fade's 4-6h
    window at T0). Stream: pre-window history seeds books; an in-window
    top-of-book change puts both markets tight in-band (yes 0.30/0.31),
    which triggers hylshi_fade — but only KXLOWT survives the prefix
    filter."""
    archive_db = tmp_path / "archive.duckdb"
    stream_db = tmp_path / "stream.duckdb"
    store = Store(archive_db)
    store.upsert_markets(
        [
            MarketInfo(
                venue="kalshi",
                market_id="KXLOWTPHIL-26JUL08-B62.5",
                series="KXLOWTPHIL",
                close_time=T0 + timedelta(hours=5),
                result="no",
            ),
            MarketInfo(
                venue="kalshi",
                market_id="KXHIGHCHI-26JUL08-B85.5",
                series="KXHIGHCHI",
                close_time=T0 + timedelta(hours=5),
                result="no",
            ),
        ]
    )
    store.close()
    sstore = StreamStore(stream_db)
    for mid in ("KXLOWTPHIL-26JUL08-B62.5", "KXHIGHCHI-26JUL08-B85.5"):
        # History (before --from): seeds the replayer, never traded.
        sstore.append_events(_snapshot_frame(mid, 1, 29, 70, T0 - timedelta(hours=1)))
        # In-window change: yes 0.30/0.31, spread 0.01, 5h to close.
        sstore.append_events(_snapshot_frame(mid, 2, 30, 69, T0))
    sstore.flush()
    return archive_db, stream_db


def test_run_l2_end_to_end_manifest_and_deterministic_fills(tmp_path):
    archive_db, stream_db = _build_archives(tmp_path)
    start, end = T0 - timedelta(minutes=10), T0 + timedelta(minutes=10)

    manifest, result, n_snaps = run_l2(
        ["hylshi_fade"],
        start.replace(tzinfo=None),
        end.replace(tzinfo=None),
        stream_db=str(stream_db),
        archive_db=str(archive_db),
        prefix="KXLOWT",
        latency=0.0,
        runs_dir=str(tmp_path / "runs1"),
    )
    # One fill: taker NO at no_ask = 1 - yes_bid = 0.70, fade's default qty.
    assert n_snaps == 1  # only the KXLOWT in-window change (prefix filter)
    assert len(result.fills) == 1
    f = result.fills[0]
    assert f.market_id == "KXLOWTPHIL-26JUL08-B62.5"
    assert f.strategy == "hylshi_fade"
    assert f.side == "no" and f.qty == 5.0 and f.price == 0.70 and not f.maker

    # Run-dir convention: manifest + fills + equity, strategy recorded.
    body = json.loads(manifest.read_text())
    assert body["strategies"] == [{"name": "hylshi_fade", "class": "HylshiFade"}]
    assert body["data"]["n_snapshots"] == 1
    fills = json.loads((manifest.parent / "fills.json").read_text())
    assert len(fills) == 1 and fills[0]["price"] == 0.70
    assert (manifest.parent / "equity.json").exists()

    # Determinism: an identical second run produces identical fills.
    _, result2, _ = run_l2(
        ["hylshi_fade"],
        start.replace(tzinfo=None),
        end.replace(tzinfo=None),
        stream_db=str(stream_db),
        archive_db=str(archive_db),
        prefix="KXLOWT",
        latency=0.0,
        runs_dir=str(tmp_path / "runs2"),
    )
    assert [(f.market_id, f.side, f.qty, f.price, f.fee, f.ts) for f in result.fills] == [
        (f.market_id, f.side, f.qty, f.price, f.fee, f.ts) for f in result2.fills
    ]


def test_run_l2_no_prefix_trades_both_markets(tmp_path):
    archive_db, stream_db = _build_archives(tmp_path)
    _, result, n_snaps = run_l2(
        ["hylshi_fade"],
        (T0 - timedelta(minutes=10)).replace(tzinfo=None),
        (T0 + timedelta(minutes=10)).replace(tzinfo=None),
        stream_db=str(stream_db),
        archive_db=str(archive_db),
        latency=0.0,
        runs_dir=str(tmp_path / "runs"),
    )
    assert n_snaps == 2
    assert sorted(f.market_id for f in result.fills) == [
        "KXHIGHCHI-26JUL08-B85.5",
        "KXLOWTPHIL-26JUL08-B62.5",
    ]
