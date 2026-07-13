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

from hyxlab.store import Store, connect_retry
from hyxlab.streamstore import BookEvent
from simulator.bookreplay import BOOK_GAPS, replay_snapshots
from simulator.shadow import SHADOW_DB, STREAM_DB
from simulator.sim import Simulator
from strategies.probe import TightSpreadProbe

STRATEGIES = {"probe": TightSpreadProbe}
MATCH_TOLERANCE = timedelta(seconds=60)
NEAREST_WINDOW = timedelta(seconds=2)
_QTY_EPS = 1e-9
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

    with connect_retry(stream_db) as conn:
        # Seed books exactly as shadow does: replay history since the
        # last coverage break WITHOUT stepping the sim.
        floor = conn.execute(
            f"SELECT max(ended_at) FROM stream_gaps WHERE ended_at <= ? AND {BOOK_GAPS}",
            [anchor],
        ).fetchone()[0]
        from simulator.bookreplay import BookReplayer

        replayer = BookReplayer()
        seed = _events(conn, floor or datetime.min, anchor)
        for _ in replay_snapshots(seed, replayer=replayer):
            pass
        # Trade the window with full gap honesty (including gap rows the
        # live run never saw, e.g. flush_failure_backfill).
        gaps = conn.execute(
            f"SELECT started_at, ended_at FROM stream_gaps"
            f" WHERE ended_at > ? AND started_at <= ? AND {BOOK_GAPS}"
            f" ORDER BY started_at",
            [anchor, end],
        ).fetchall()
        for snap in replay_snapshots(_events(conn, anchor, end), gaps=gaps, replayer=replayer):
            sim.step(snap)
    return [f for f in sim.result.fills if f.ts <= end]


def compare(
    shadow_fills: list[tuple], replay_fills: list, window: timedelta = NEAREST_WINDOW
) -> dict:
    """Tiered per-(market, side) matching; price deltas per tier.

    Tier priority: exact (the v1 matcher, byte-for-byte unchanged:
    equal qty within 60s, greedy in time order) claims fills FIRST, so
    a clean window reports identically to v1 — the relaxed tiers only
    ever see exact's leftovers. Split-aware runs before nearest because
    the nearest key is qty-free and would otherwise consume one leg of
    a partial-fill group. Neither relaxed tier invents agreement: split
    demands partials at the same price summing exactly to the single
    fill, nearest demands the exact same price; both are confined to
    `window` (default 2s) and counted separately in the report so a
    calibration read never silently mixes tiers.

    shadow_fills rows: (market_id, side, qty, price, fee, maker, ts).
    """
    from collections import defaultdict

    s_by, r_by = defaultdict(list), defaultdict(list)
    for m, side, qty, price, fee, maker, ts in shadow_fills:
        s_by[(m, side)].append([ts, qty, price, fee, maker])
    for f in replay_fills:
        r_by[(f.market_id, f.side)].append([f.ts, f.qty, f.price, f.fee, f.maker])

    # Tier 1 — exact (v1 semantics, unchanged): equal qty within 60s,
    # greedy over time-sorted fills. Leftovers feed the relaxed tiers.
    matched, deltas = 0, []
    s_left, r_left = defaultdict(list), defaultdict(list)
    for key in sorted(s_by.keys() | r_by.keys()):
        r_list = sorted(r_by.get(key, []))
        for s in sorted(s_by.get(key, [])):
            best = None
            for i, r in enumerate(r_list):
                if abs(r[0] - s[0]) <= MATCH_TOLERANCE and r[1] == s[1]:
                    best = i
                    break
            if best is not None:
                r = r_list.pop(best)
                matched += 1
                deltas.append(r[2] - s[2])  # replay price - shadow price
            else:
                s_left[key].append(s)
        r_left[key] = r_list

    # Tier split — N partials at one price on one side summing exactly
    # to a single leftover fill on the other, all within `window` of it.
    def _claim_group(single, cands):
        """Earliest contiguous time-sorted run (>=2 fills, same price,
        inside `window` of `single`) whose qtys sum to single's qty."""
        elig = sorted(c for c in cands if c[2] == single[2] and abs(c[0] - single[0]) <= window)
        for i in range(len(elig)):
            total = 0.0
            for j in range(i, len(elig)):
                total += elig[j][1]
                if total > single[1] + _QTY_EPS:
                    break
                if abs(total - single[1]) <= _QTY_EPS and j > i:
                    return elig[i : j + 1]
        return None

    split_groups, split_s_fills, split_r_fills, split_deltas = 0, 0, 0, []
    for key in sorted(s_left.keys() | r_left.keys()):
        for singles, parts, sign in (
            (s_left[key], r_left[key], +1),  # 1 shadow <- N replay
            (r_left[key], s_left[key], -1),  # 1 replay <- N shadow
        ):
            for single in list(singles):
                grp = _claim_group(single, parts)
                if grp is None:
                    continue
                singles.remove(single)
                for g in grp:
                    parts.remove(g)
                split_groups += 1
                split_s_fills += 1 if sign > 0 else len(grp)
                split_r_fills += len(grp) if sign > 0 else 1
                split_deltas.append(sign * (grp[0][2] - single[2]))  # 0 by construction

    # Tier nearest — one-to-one on the remainder: same (market, side,
    # price), smallest |dt| within `window` first; ties by earliest ts.
    nearest_pairs = []
    for key in sorted(s_left.keys() & r_left.keys()):
        cands = sorted(
            (abs(r[0] - s[0]), s[0], r[0], si, ri)
            for si, s in enumerate(s_left[key])
            for ri, r in enumerate(r_left[key])
            if r[2] == s[2] and abs(r[0] - s[0]) <= window
        )
        used_s, used_r = set(), set()
        for dt, _sts, _rts, si, ri in cands:
            if si in used_s or ri in used_r:
                continue
            used_s.add(si)
            used_r.add(ri)
            nearest_pairs.append((dt, s_left[key][si], r_left[key][ri]))
    n_nearest = len(nearest_pairs)
    nearest_deltas = [r[2] - s[2] for _, s, r in nearest_pairs]  # 0 by construction
    all_deltas = deltas + split_deltas + nearest_deltas

    def _abs_mean(vals):
        return round(sum(abs(v) for v in vals) / len(vals), 6) if vals else None

    # qty-weighted overlap: bucket each side's quantity by minute and
    # credit min(shadow, replay) per bucket — split fills (5 vs 3+2)
    # count as matched quantity instead of unmatched orders (v2).
    def _qty_buckets(by):
        out: dict[tuple, float] = {}
        for key, fills in by.items():
            for ts, qty, *_ in fills:
                b = (key, ts.replace(second=0, microsecond=0))
                out[b] = out.get(b, 0.0) + qty
        return out

    sq, rq = _qty_buckets(s_by), _qty_buckets(r_by)
    matched_qty = sum(min(v, rq.get(k, 0.0)) for k, v in sq.items())
    qty_s, qty_r = sum(sq.values()), sum(rq.values())

    n_s = sum(len(v) for v in s_by.values())
    n_r = sum(len(v) for v in r_by.values())
    n_all_s = matched + n_nearest + split_s_fills
    n_all_r = matched + n_nearest + split_r_fills
    deltas.sort()
    return {
        "matching_note": (
            "order-level tiers: exact (v1 floor: equal qty in a 60s"
            " window) alone decides matched/match_rate_*/price_delta_*;"
            " split (same-price partials summing exactly) and nearest"
            " (same price, smallest |dt|) claim only exact's leftovers"
            f" within {window.total_seconds()}s and are counted"
            " separately (v2); qty_match_* buckets quantity per minute"
            " and credits overlap, so split fills count"
        ),
        "matched_nearest": n_nearest,
        "matched_split_groups": split_groups,
        "matched_split_shadow_fills": split_s_fills,
        "matched_split_replay_fills": split_r_fills,
        "matched_all_vs_shadow": n_all_s,
        "matched_all_vs_replay": n_all_r,
        "match_rate_all_vs_shadow": round(n_all_s / n_s, 4) if n_s else None,
        "match_rate_all_vs_replay": round(n_all_r / n_r, 4) if n_r else None,
        "nearest_window_s": window.total_seconds(),
        "nearest_dt_abs_mean_s": (
            round(sum(dt.total_seconds() for dt, _, _ in nearest_pairs) / n_nearest, 6)
            if nearest_pairs
            else None
        ),
        "price_delta_abs_mean_nearest": _abs_mean(nearest_deltas),
        "price_delta_abs_mean_split": _abs_mean(split_deltas),
        "price_delta_mean_all": round(sum(all_deltas) / len(all_deltas), 6) if all_deltas else None,
        "price_delta_abs_mean_all": _abs_mean(all_deltas),
        "qty_match_rate_vs_shadow": round(matched_qty / qty_s, 4) if qty_s else None,
        "qty_match_rate_vs_replay": round(matched_qty / qty_r, 4) if qty_r else None,
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
    ap.add_argument(
        "--nearest-window",
        type=float,
        default=NEAREST_WINDOW.total_seconds(),
        help="seconds of |dt| tolerance for the nearest/split tiers (default 2)",
    )
    args = ap.parse_args()

    with connect_retry(args.shadow_db) as conn:
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
        **compare(shadow_fills, replay_fills, window=timedelta(seconds=args.nearest_window)),
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
