"""Daily data-quality checks over both archives (scheduled: hyxlab-qa.timer).

    python -m hyxlab.qa [--hours 26]

Read-only. Every check prints PASS/FAIL; any FAIL makes the exit code 1
so the failure lands loudly in the journal. Promoted from the one-off
2026-07-07 stream audit — the archives are now big enough that silent
rot (a wedged daemon, a schema drift, a purge racing ahead of the tape)
is the main operational threat, and each of these checks watches a
failure mode that has either happened or provably can.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import duckdb

ARCHIVE = "data/hyxlab.duckdb"
STREAM = "data/hyxstream.duckdb"

_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    line = f"{'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else "")
    print(line, flush=True)
    if not ok:
        _failures.append(name)


def _connect_ro(path: str, retries: int = 5) -> duckdb.DuckDBPyConnection | None:
    for attempt in range(retries):
        try:
            return duckdb.connect(path, read_only=True)
        except duckdb.Error:
            if attempt == retries - 1:
                return None
            time.sleep(2)  # writer burst (collector/tradepass flush)
    return None


def qa_stream(hours: float, path: str = STREAM) -> None:
    conn = _connect_ro(path)
    if conn is None:
        check("stream archive reachable", False, "writer held the file for >10s")
        return
    now = datetime.now(UTC).replace(tzinfo=None)

    age = conn.execute("SELECT epoch(? - max(recv_ts)) FROM stream_trades", [now]).fetchone()[0]
    check("stream fresh (trades < 5 min old)", age is not None and age < 300, f"age {age:.0f}s")

    bad = conn.execute(
        "SELECT count(*) FROM stream_trades WHERE recv_ts > ? - INTERVAL 1 HOUR *"
        " CAST(? AS INTEGER) AND (price <= 0 OR price >= 1 OR qty <= 0)",
        [now, int(hours)],
    ).fetchone()[0]
    check("trade price/qty domains", bad == 0, f"{bad} bad rows in window")

    # Seq continuity per sid within the window's connections. A hole the
    # SeqTracker missed = daemon bug; a hole WITH a matching gap row is fine,
    # so only alert when holes exist and no gap row covers the window.
    holes = conn.execute(
        "SELECT coalesce(sum(mx - mn + 1 - cnt), 0) FROM ("
        " SELECT sid, min(seq) mn, max(seq) mx, count(DISTINCT seq) cnt"
        " FROM book_events WHERE recv_ts > ? - INTERVAL 1 HOUR * CAST(? AS INTEGER)"
        " AND sid IS NOT NULL GROUP BY sid)",
        [now, int(hours)],
    ).fetchone()[0]
    gaps = conn.execute(
        "SELECT count(*) FROM stream_gaps WHERE ended_at > ? - INTERVAL 1 HOUR *"
        " CAST(? AS INTEGER)",
        [now, int(hours)],
    ).fetchone()[0]
    check(
        "book seq contiguous or gap-marked",
        holes == 0 or gaps > 0,
        f"{holes} seq holes, {gaps} gap rows in window",
    )

    neg = conn.execute(
        """
        WITH last_snap AS (
          SELECT market_id, side, max(seq) AS snap_seq
          FROM book_events WHERE kind='snap' GROUP BY market_id, side
        ), levels AS (
          SELECT e.market_id, e.side, e.price,
                 sum(CASE WHEN e.kind='snap' AND e.seq = ls.snap_seq THEN e.qty
                          WHEN e.kind='delta' AND e.seq > ls.snap_seq THEN e.qty
                          ELSE 0 END) AS qty
          FROM book_events e
          JOIN last_snap ls ON ls.market_id = e.market_id AND ls.side = e.side
          GROUP BY e.market_id, e.side, e.price
        )
        SELECT count(*) FROM levels WHERE qty < -1e-9
        """
    ).fetchone()[0]
    check("reconstructed book levels non-negative", neg == 0, f"{neg} negative levels")

    p99 = conn.execute(
        "SELECT quantile_cont(epoch(recv_ts - src_ts), 0.99) FROM stream_trades"
        " WHERE src_ts IS NOT NULL AND recv_ts > ? - INTERVAL 1 HOUR * CAST(? AS INTEGER)",
        [now, int(hours)],
    ).fetchone()[0]
    # 25s allows for the known ~20s box-clock skew until NTP lands.
    check(
        "trade latency p99 sane",
        p99 is not None and -2.0 < p99 < 25.0,
        f"p99 {p99 if p99 is None else round(p99, 2)}s (incl. known clock skew)",
    )

    size_gb = Path(path).stat().st_size / 1e9
    check("stream disk under 20 GB", size_gb < 20.0, f"{size_gb:.2f} GB")
    conn.close()


def qa_archive(hours: float, path: str = ARCHIVE) -> None:
    conn = _connect_ro(path)
    if conn is None:
        check("main archive reachable", False, "writer held the file for >10s")
        return
    now = datetime.now(UTC).replace(tzinfo=None)

    age = conn.execute("SELECT epoch(? - max(ts)) FROM snapshots", [now]).fetchone()[0]
    check(
        "collector fresh (snapshots < 20 min old)",
        age is not None and age < 1200,
        f"age {age:.0f}s",
    )

    ok_sweeps = conn.execute(
        "SELECT count(*) FROM sweep_log WHERE status='ok' AND swept_at > ? - INTERVAL 36 HOUR",
        [now],
    ).fetchone()[0]
    check("sweep ran in last 36h", ok_sweeps > 0, f"{ok_sweeps} ok entries")

    mv = conn.execute(
        "SELECT count(*) FROM snapshots WHERE venue='kalshi' AND ("
        " (no_ask IS NOT NULL AND yes_bid IS NOT NULL AND abs(no_ask - (1-yes_bid)) > 0.005)"
        " OR (no_bid IS NOT NULL AND yes_ask IS NOT NULL AND abs(no_bid - (1-yes_ask)) > 0.005))"
    ).fetchone()[0]
    check("kalshi mirror invariant", mv == 0, f"{mv} violations")

    # Polymarket price capture: once the poly sweep has ever run, its
    # daily cadence must hold (retention rolls off at ~60d).
    n_poly = conn.execute("SELECT count(*) FROM poly_prices").fetchone()[0]
    if n_poly:
        page = conn.execute("SELECT epoch(? - max(ts)) / 3600 FROM poly_prices", [now]).fetchone()[
            0
        ]
        check("poly prices fresh (< 30h old)", page < 30, f"age {page:.1f}h")

    # Tape coverage: settled+traded markets inside the retention window
    # (~64d, use 55 to stay ahead of the boundary) must have a tape sweep.
    uncovered = conn.execute(
        """
        SELECT count(*) FROM markets m
        WHERE m.venue='kalshi' AND m.result != ''
          AND m.close_time > ? - INTERVAL 55 DAY AND m.close_time < ? - INTERVAL 1 DAY
          AND EXISTS (SELECT 1 FROM candles c WHERE c.market_id = m.market_id AND c.volume > 0)
          AND NOT EXISTS (SELECT 1 FROM trades_swept s WHERE s.market_id = m.market_id)
        """,
        [now, now],
    ).fetchone()[0]
    check(
        "trade tape covers retention window", uncovered == 0, f"{uncovered} traded markets unswept"
    )
    conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="hyxlab daily data-quality checks")
    ap.add_argument("--hours", type=float, default=26.0, help="recency window")
    args = ap.parse_args()

    print(f"[qa] {datetime.now(UTC):%Y-%m-%d %H:%M} window={args.hours}h", flush=True)
    qa_stream(args.hours)
    qa_archive(args.hours)
    if _failures:
        print(f"[qa] {len(_failures)} FAILURES: {_failures}", flush=True)
        sys.exit(1)
    print("[qa] all checks pass", flush=True)


if __name__ == "__main__":
    main()
