"""Shadow harness (Tier 3): strategies run against the LIVE market with
ledger-only orders — the strongest no-lookahead tier that exists without
capital, because the future hasn't happened when decisions are made.

    python -m simulator.shadow [--latency 2.0] [--poll 20] [--duration N]

Architecture: the Simulator is already an online algorithm, so shadow =
a persistent Simulator instance fed by a live tail of the stream archive
(data/hyxstream.duckdb, read-only polls; the daemon flushes every ~15 s).
BookReplayer turns new events into ms-fidelity snapshots; sim.step()
executes with the same latency model as backtests — which makes shadow
runs directly comparable to sim replays over the same recorded window:
the divergence IS the fill-model calibration error.

The inherent tail lag (~15–35 s behind the venue) is polling-scale
realism, not a defect; decisions could not have been faster from this
box. Fills/equity persist to data/hyxshadow.duckdb (own file — sim-side
writer, never touches the collection archives' locks).

v1 scope: taker scoring via the latency model. Maker queue-position
bounds (design note in simulation-honesty.md) are the next iteration.
Restart = fresh sim state (positions reset); each run gets a run_id.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from hyxlab.store import Store
from hyxlab.streamstore import BookEvent
from simulator.bookreplay import BookReplayer, replay_snapshots
from simulator.sim import Simulator
from strategies.probe import TightSpreadProbe

STREAM_DB = "data/hyxstream.duckdb"
SHADOW_DB = "data/hyxshadow.duckdb"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow_runs (
    run_id     VARCHAR PRIMARY KEY,
    started_at TIMESTAMP,
    latency_s  DOUBLE,
    strategies VARCHAR
);
CREATE TABLE IF NOT EXISTS shadow_fills (
    run_id    VARCHAR NOT NULL,
    strategy  VARCHAR NOT NULL,
    venue     VARCHAR NOT NULL,
    market_id VARCHAR NOT NULL,
    side      VARCHAR NOT NULL,
    qty       DOUBLE NOT NULL,
    price     DOUBLE NOT NULL,
    fee       DOUBLE NOT NULL,
    maker     BOOLEAN,
    ts        TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS shadow_equity (
    run_id VARCHAR NOT NULL,
    ts     TIMESTAMP NOT NULL,
    equity DOUBLE NOT NULL
);
"""


def _naive(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt


class ShadowLedger:
    def __init__(self, path: str | Path = SHADOW_DB) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with duckdb.connect(str(self.path)) as conn:
            conn.execute(_SCHEMA)

    def start_run(self, run_id: str, latency: float, strategies: list[str]) -> None:
        with duckdb.connect(str(self.path)) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO shadow_runs VALUES (?,?,?,?)",
                [run_id, datetime.now(UTC).replace(tzinfo=None), latency, ",".join(strategies)],
            )

    def persist(self, run_id: str, fills: list, equity: tuple[datetime, float] | None) -> None:
        if not fills and equity is None:
            return
        with duckdb.connect(str(self.path)) as conn:
            if fills:
                conn.executemany(
                    "INSERT INTO shadow_fills VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [
                        (
                            run_id,
                            f.strategy,
                            f.venue,
                            f.market_id,
                            f.side,
                            f.qty,
                            f.price,
                            f.fee,
                            f.maker,
                            _naive(f.ts),
                        )
                        for f in fills
                    ],
                )
            if equity is not None:
                conn.execute(
                    "INSERT INTO shadow_equity VALUES (?,?,?)",
                    [run_id, _naive(equity[0]), equity[1]],
                )


class ShadowRunner:
    def __init__(
        self,
        strategies: list,
        latency: float = 2.0,
        stream_db: str = STREAM_DB,
        archive_db: str = "data/hyxlab.duckdb",
        ledger: ShadowLedger | None = None,
    ) -> None:
        self.stream_db = stream_db
        self.archive_db = archive_db
        self.ledger = ledger or ShadowLedger()
        self.run_id = f"{datetime.now(UTC):%Y%m%dT%H%M%S}"
        # The archive may be writer-locked for minutes (sweep/backfills).
        # Market metadata only gates settlement/expiry, so shadow starts
        # without it and keeps retrying from the poll loop.
        markets = self._try_load_markets() or {}
        self.sim = Simulator(markets, strategies, latency=latency)
        self.replayer = BookReplayer()
        self.cursor: datetime | None = None  # start from NOW (first poll sets it)
        self.gap_cursor: datetime | None = None
        self._n_fills_persisted = 0
        self.ledger.start_run(self.run_id, latency, [s.name for s in strategies])
        self.stats = {"snapshots": 0, "events": 0, "polls": 0}

    def _try_load_markets(self) -> dict | None:
        try:
            store = Store(self.archive_db, read_only=True)
        except duckdb.Error:
            return None
        try:
            return store.markets()
        finally:
            store.close()

    def _read_new(self) -> tuple[list[BookEvent], list[tuple]]:
        with duckdb.connect(self.stream_db, read_only=True) as conn:
            if self.cursor is None:
                # First poll anchors at the newest archived event: shadow
                # trades the FUTURE only. But book state must be SEEDED
                # from history (snapshot images arrive at daemon connect,
                # hours ago; post-anchor events are deltas an unseeded
                # replayer rightly refuses). Replay the archive since the
                # last coverage break into the replayer WITHOUT stepping
                # the sim — state derivation, not decision-making.
                self.cursor = conn.execute("SELECT max(recv_ts) FROM book_events").fetchone()[0]
                self.gap_cursor = self.cursor
                if self.cursor is not None:
                    floor = conn.execute("SELECT max(ended_at) FROM stream_gaps").fetchone()[0]
                    rows = conn.execute(
                        "SELECT venue, market_id, recv_ts, src_ts, sid, seq, kind, side,"
                        " price, qty FROM book_events WHERE venue = 'kalshi'"
                        " AND recv_ts >= coalesce(?, recv_ts) ORDER BY recv_ts, seq",
                        [floor],
                    ).fetchall()
                    seeded = 0
                    for _ in replay_snapshots(
                        (BookEvent(*r) for r in rows), replayer=self.replayer
                    ):
                        seeded += 1
                    print(
                        f"[shadow] seeded books from {len(rows)} archived events"
                        f" ({seeded} top states; trading starts at {self.cursor})",
                        flush=True,
                    )
                return [], []
            rows = conn.execute(
                "SELECT venue, market_id, recv_ts, src_ts, sid, seq, kind, side, price, qty"
                " FROM book_events WHERE recv_ts > ? AND venue = 'kalshi'"
                " ORDER BY recv_ts, seq",
                [self.cursor],
            ).fetchall()
            gaps = conn.execute(
                "SELECT started_at, ended_at FROM stream_gaps WHERE ended_at > ?",
                [self.gap_cursor],
            ).fetchall()
        if rows:
            self.cursor = rows[-1][2]
        if gaps:
            self.gap_cursor = max(g[1] for g in gaps)
        return [BookEvent(*r) for r in rows], gaps

    def poll_once(self) -> int:
        if not self.sim.markets:
            markets = self._try_load_markets()
            if markets:
                self.sim.markets = markets
                self.sim.ctx._markets = markets  # Context holds its own ref
                print(f"[shadow] market metadata loaded ({len(markets)})", flush=True)
        try:
            events, gaps = self._read_new()
        except duckdb.Error:
            return 0  # daemon mid-flush; next poll catches up
        n = 0
        for snap in replay_snapshots(events, gaps=gaps, replayer=self.replayer):
            self.sim.step(snap)
            n += 1
        self.stats["snapshots"] += n
        self.stats["events"] += len(events)
        self.stats["polls"] += 1
        # Persist newly produced fills + one equity point per poll.
        new_fills = self.sim.result.fills[self._n_fills_persisted :]
        equity = self.sim.result.equity_curve[-1] if self.sim.result.equity_curve else None
        self.ledger.persist(self.run_id, new_fills, equity)
        self._n_fills_persisted = len(self.sim.result.fills)
        return n


def main() -> None:
    ap = argparse.ArgumentParser(description="hyxlab shadow harness (ledger-only live trading)")
    ap.add_argument("--latency", type=float, default=2.0)
    ap.add_argument("--poll", type=float, default=20.0)
    ap.add_argument("--duration", type=float, default=None, help="seconds; default forever")
    args = ap.parse_args()

    runner = ShadowRunner([TightSpreadProbe()], latency=args.latency)
    print(
        f"[shadow] run {runner.run_id} latency={args.latency}s poll={args.poll}s "
        f"(ledger-only; anchored at stream head)",
        flush=True,
    )
    t0 = time.monotonic()
    last_report = t0
    while args.duration is None or time.monotonic() - t0 < args.duration:
        runner.poll_once()
        if time.monotonic() - last_report >= 300:
            print(
                f"[shadow] {runner.stats} fills={len(runner.sim.result.fills)}",
                flush=True,
            )
            last_report = time.monotonic()
        time.sleep(args.poll)
    result = runner.sim.finalize()
    print(f"[shadow] done: {runner.stats} fills={len(result.fills)}", flush=True)
    print(json.dumps(result.metrics, default=str), flush=True)


if __name__ == "__main__":
    main()
