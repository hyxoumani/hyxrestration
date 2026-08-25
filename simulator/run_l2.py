"""One-command L2 backtest: any registered strategy over recorded books.

    python -m simulator.run_l2 --strategy hylshi_fade \
        --from 2026-08-02T16:00 --to 2026-08-02T20:00 \
        [--db data/hyxstream.duckdb] [--archive-db data/hyxlab.duckdb] \
        [--markets KXLOWT] [--latency 2.0] [--out data/runs]

Composes BookReplayer (ms-fidelity book_events replay, gap-honest) +
the shared strategy registry (simulator.registry) + Simulator, using the
divergence runner's exact seeding discipline: book state is seeded by
replaying the archive from the last coverage break up to --from WITHOUT
stepping the sim (state derivation, not decision-making); trading covers
(--from, --to] with every stream_gaps row honored. Each run leaves a
manifest + fills.json (simulator.harness.write_manifest, same run-dir
convention as run_favlong) plus equity.json under --out.

Read-only on both archives; the stream connection reuses shadow's
memory-bounded engine settings so an ad-hoc backtest never competes with
the live daemon for RAM.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

from hyxlab.store import open_retry
from simulator.bookreplay import BOOK_GAPS, BookReplayer, replay_snapshots, stream_events
from simulator.harness import write_manifest
from simulator.registry import STRATEGIES, build
from simulator.shadow import STREAM_DB, stream_conn
from simulator.sim import Simulator

EQUITY_MAX_POINTS = 10_000

def _naive(ts: str | datetime) -> datetime:
    dt = datetime.fromisoformat(ts) if isinstance(ts, str) else ts
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt



def run_l2(
    strategy_names: list[str],
    start: datetime,
    end: datetime,
    stream_db: str = STREAM_DB,
    archive_db: str = "data/hyxlab.duckdb",
    prefix: str | None = None,
    latency: float = 2.0,
    runs_dir: str = "data/runs",
):
    """Replay (start, end] through the named strategies; returns
    (manifest_path, SimResult, n_snapshots)."""
    store = open_retry(archive_db, read_only=True)
    try:
        markets = store.markets()
    finally:
        store.close()
    strategies = build(strategy_names)
    # L2 replay is Kalshi-only (BookReplayer refuses other venues); the
    # feed provides no independent NO book (one mirrored book), so the
    # capability guard rejects strategies this data cannot trigger.
    sim = Simulator(markets, strategies, latency=latency, data_capabilities={"kalshi": frozenset()})

    n_snaps, ts_min, ts_max = 0, None, None
    with stream_conn(stream_db) as conn:
        # Seed books exactly as shadow/divergence do: replay history since
        # the last coverage break WITHOUT stepping the sim.
        floor = conn.execute(
            f"SELECT max(ended_at) FROM stream_gaps WHERE ended_at <= ? AND {BOOK_GAPS}",
            [start],
        ).fetchone()[0]
        replayer = BookReplayer()
        for _ in replay_snapshots(
            stream_events(conn, floor or datetime.min, start, prefix=prefix), replayer=replayer
        ):
            pass
        gaps = conn.execute(
            f"SELECT started_at, ended_at FROM stream_gaps"
            f" WHERE ended_at > ? AND started_at <= ? AND {BOOK_GAPS} ORDER BY started_at",
            [start, end],
        ).fetchall()
        for snap in replay_snapshots(
            stream_events(conn, start, end, prefix=prefix), gaps=gaps, replayer=replayer
        ):
            sim.step(snap)
            n_snaps += 1
            ts_min = ts_min or snap.ts
            ts_max = snap.ts
    result = sim.finalize()

    fingerprint = {
        "n_snapshots": n_snaps,
        "ts_min": str(ts_min) if ts_min else None,
        "ts_max": str(ts_max) if ts_max else None,
        "window": [str(start), str(end)],
        "stream_db": stream_db,
        "markets_prefix": prefix,
        "latency_s": latency,
    }
    manifest = write_manifest(
        result,
        strategies=[{"name": n, "class": STRATEGIES[n].__name__} for n in strategy_names],
        fingerprint=fingerprint,
        runs_dir=runs_dir,
    )
    # Equity curve alongside the manifest (downsampled; endpoints kept).
    curve = result.equity_curve
    step = max(1, len(curve) // EQUITY_MAX_POINTS)
    sampled = curve[::step]
    if curve and (not sampled or sampled[-1] != curve[-1]):
        sampled.append(curve[-1])
    (manifest.parent / "equity.json").write_text(json.dumps([[str(ts), eq] for ts, eq in sampled]))
    return manifest, result, n_snaps


def main() -> None:
    ap = argparse.ArgumentParser(description="one-command L2 book-replay backtest")
    ap.add_argument(
        "--strategy",
        required=True,
        help=f"comma-separated registry names; registered: {sorted(STRATEGIES)}",
    )
    ap.add_argument("--from", dest="start", required=True, help="ISO ts (UTC), window start")
    ap.add_argument("--to", dest="end", required=True, help="ISO ts (UTC), window end")
    ap.add_argument("--db", default=STREAM_DB, help="stream archive (book_events)")
    ap.add_argument("--archive-db", default="data/hyxlab.duckdb", help="market metadata")
    ap.add_argument("--markets", default=None, help="market_id prefix filter, e.g. KXLOWT")
    ap.add_argument("--latency", type=float, default=2.0)
    ap.add_argument("--out", default="data/runs", help="run-dir root")
    args = ap.parse_args()

    names = args.strategy.split(",")
    start, end = _naive(args.start), _naive(args.end)
    print(f"[run_l2] {names} over ({start}, {end}] prefix={args.markets} latency={args.latency}s")
    manifest, result, n_snaps = run_l2(
        names,
        start,
        end,
        stream_db=args.db,
        archive_db=args.archive_db,
        prefix=args.markets,
        latency=args.latency,
        runs_dir=args.out,
    )
    print(f"[run_l2] {n_snaps} snapshots -> {len(result.fills)} fills; run dir {manifest.parent}")
    for f in result.fills:
        print(
            f"  fill {f.ts} {f.strategy} {f.market_id} {f.side} qty={f.qty}"
            f" @ {f.price} fee={f.fee} maker={f.maker}"
        )
    print(json.dumps(result.metrics, indent=1, default=str))


if __name__ == "__main__":
    main()
