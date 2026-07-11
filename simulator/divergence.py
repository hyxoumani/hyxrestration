"""Shadow-vs-replay divergence report: the fill-model calibration haircut.

    python -m simulator.divergence [--run RUN_ID] [--anchor ISO_TS]

Replays the exact stream-archive window a shadow run traded — same
seeding procedure, same strategy, same latency model — and compares the
two fill streams. Shadow decided while the future didn't exist; the
replay decides over the identical recording. Any disagreement is
therefore infrastructure, not market: late-arriving archive rows,
coverage gaps unknown to the live run (e.g. retro-marked flush
failures), or fill-model asymmetries. The signed price delta per
contract is the haircut to apply to every backtest number.

Equity is deliberately NOT compared in v1: replay sees today's
settlement metadata for markets that were unresolved while shadow ran,
so P&L differences would conflate resolution knowledge with fill
quality. Fills are the honest common currency.

Output: printed summary + reports/shadow_divergence/{run_id}.json.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb

from hyxlab.store import Store
from hyxlab.streamstore import BookEvent
from simulator.bookreplay import replay_snapshots
from simulator.shadow import SHADOW_DB, STREAM_DB
from simulator.sim import Simulator
from strategies.probe import TightSpreadProbe

STRATEGIES = {"probe": TightSpreadProbe}
MATCH_TOLERANCE = timedelta(seconds=60)
CHUNK = 200_000


def _events(conn, lo: datetime, hi: datetime | None):
    """Stream kalshi book events in (lo, hi] in replay order, chunked."""
    q = (
        "SELECT venue, market_id, recv_ts, src_ts, sid, seq, kind, side, price, qty"
        " FROM book_events WHERE venue='kalshi' AND recv_ts > ?"
        + (" AND recv_ts <= ?" if hi else "")
        + " ORDER BY recv_ts, seq"
    )
    cur = conn.execute(q, [lo, hi] if hi else [lo])
    while rows := cur.fetchmany(CHUNK):
        for r in rows:
            yield BookEvent(*r)


def replay_run(
    run_id: str,
    anchor: datetime,
    end: datetime,
    latency: float,
    strategy_names: list[str],
    stream_db: str = STREAM_DB,
    archive_db: str = "data/hyxlab.duckdb",
) -> list:
    """Reproduce a shadow run offline; returns the replay's fills."""
    store = Store(archive_db, read_only=True)
    try:
        markets = store.markets()
    finally:
        store.close()
    sim = Simulator(markets, [STRATEGIES[n]() for n in strategy_names], latency=latency)

    with duckdb.connect(stream_db, read_only=True) as conn:
        # Seed books exactly as shadow does: replay history since the
        # last coverage break WITHOUT stepping the sim.
        floor = conn.execute(
            "SELECT max(ended_at) FROM stream_gaps WHERE ended_at <= ?", [anchor]
        ).fetchone()[0]
        from simulator.bookreplay import BookReplayer

        replayer = BookReplayer()
        seed = _events(conn, floor or datetime.min, anchor)
        for _ in replay_snapshots(seed, replayer=replayer):
            pass
        # Trade the window with full gap honesty (including gap rows the
        # live run never saw, e.g. flush_failure_backfill).
        gaps = conn.execute(
            "SELECT started_at, ended_at FROM stream_gaps"
            " WHERE ended_at > ? AND started_at <= ? ORDER BY started_at",
            [anchor, end],
        ).fetchall()
        for snap in replay_snapshots(_events(conn, anchor, end), gaps=gaps, replayer=replayer):
            sim.step(snap)
    return [f for f in sim.result.fills if f.ts <= end]


def compare(shadow_fills: list[tuple], replay_fills: list) -> dict:
    """Greedy per-(market, side) match by time; price deltas on matches.

    shadow_fills rows: (market_id, side, qty, price, fee, maker, ts).
    """
    from collections import defaultdict

    s_by, r_by = defaultdict(list), defaultdict(list)
    for m, side, qty, price, fee, maker, ts in shadow_fills:
        s_by[(m, side)].append([ts, qty, price, fee, maker])
    for f in replay_fills:
        r_by[(f.market_id, f.side)].append([f.ts, f.qty, f.price, f.fee, f.maker])

    matched, deltas = 0, []
    for key, s_list in s_by.items():
        r_list = sorted(r_by.get(key, []))
        for s in sorted(s_list):
            best = None
            for i, r in enumerate(r_list):
                if abs(r[0] - s[0]) <= MATCH_TOLERANCE and r[1] == s[1]:
                    best = i
                    break
            if best is not None:
                r = r_list.pop(best)
                matched += 1
                deltas.append(r[2] - s[2])  # replay price - shadow price

    n_s = sum(len(v) for v in s_by.values())
    n_r = sum(len(v) for v in r_by.values())
    deltas.sort()
    return {
        "shadow_fills": n_s,
        "replay_fills": n_r,
        "matched": matched,
        "match_rate_vs_shadow": round(matched / n_s, 4) if n_s else None,
        "match_rate_vs_replay": round(matched / n_r, 4) if n_r else None,
        "price_delta_mean": round(sum(deltas) / len(deltas), 6) if deltas else None,
        "price_delta_median": deltas[len(deltas) // 2] if deltas else None,
        "price_delta_abs_mean": (
            round(sum(abs(d) for d in deltas) / len(deltas), 6) if deltas else None
        ),
        "shadow_gross_cash": round(sum(q * p for _, _, q, p, *_ in _rows(s_by)), 2),
        "replay_gross_cash": round(sum(q * p for _, _, q, p, *_ in _rows(r_by)), 2),
        "shadow_fees": round(sum(r[3] for r in _flat(s_by)), 2),
        "replay_fees": round(sum(r[3] for r in _flat(r_by)), 2),
    }


def _flat(by):
    for v in by.values():
        yield from v


def _rows(by):
    for (m, side), v in by.items():
        for _ts, qty, price, fee, maker in v:
            yield m, side, qty, price, fee, maker


def main() -> None:
    ap = argparse.ArgumentParser(description="shadow-vs-replay divergence report")
    ap.add_argument("--run", default=None, help="run_id (default: most fills)")
    ap.add_argument("--anchor", default=None, help="ISO ts override for the trading anchor")
    ap.add_argument("--shadow-db", default=SHADOW_DB)
    ap.add_argument("--stream-db", default=STREAM_DB)
    ap.add_argument("--archive-db", default="data/hyxlab.duckdb")
    ap.add_argument("--out", default="reports/shadow_divergence")
    args = ap.parse_args()

    with duckdb.connect(args.shadow_db, read_only=True) as conn:
        run_id = (
            args.run
            or conn.execute(
                "SELECT run_id FROM shadow_fills GROUP BY 1 ORDER BY count(*) DESC LIMIT 1"
            ).fetchone()[0]
        )
        started_at, latency, strategies, anchor = conn.execute(
            "SELECT started_at, latency_s, strategies, anchor FROM shadow_runs WHERE run_id=?",
            [run_id],
        ).fetchone()
        end = conn.execute("SELECT max(ts) FROM shadow_equity WHERE run_id=?", [run_id]).fetchone()[
            0
        ]
        shadow_fills = conn.execute(
            "SELECT market_id, side, qty, price, fee, maker, ts FROM shadow_fills"
            " WHERE run_id=? ORDER BY ts",
            [run_id],
        ).fetchall()

    if args.anchor:
        anchor = datetime.fromisoformat(args.anchor)
    if anchor is None:
        raise SystemExit(f"run {run_id} has no recorded anchor; pass --anchor (see journal)")

    print(f"[divergence] run {run_id} anchor={anchor} end={end} latency={latency}s")
    replay_fills = replay_run(
        run_id,
        anchor,
        end,
        latency,
        strategies.split(","),
        stream_db=args.stream_db,
        archive_db=args.archive_db,
    )
    report = {
        "run_id": run_id,
        "anchor": str(anchor),
        "end": str(end),
        "latency_s": latency,
        "strategies": strategies,
        "generated_at": str(datetime.now(UTC).replace(tzinfo=None)),
        **compare(shadow_fills, replay_fills),
    }
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{run_id}.json"
    out.write_text(json.dumps(report, indent=2, default=str) + "\n")
    for k, v in report.items():
        print(f"  {k}: {v}")
    print(f"[divergence] written to {out}")


if __name__ == "__main__":
    main()
