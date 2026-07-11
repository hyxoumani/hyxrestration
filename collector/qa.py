"""Daily data-quality checks over both archives (scheduled: hyxlab-qa.timer).

    python -m collector.qa [--hours 26]

Read-only. Every check prints PASS/FAIL; any FAIL makes the exit code 1
so the failure lands loudly in the journal. Promoted from the one-off
2026-07-07 stream audit — the archives are now big enough that silent
rot (a wedged daemon, a schema drift, a purge racing ahead of the tape)
is the main operational threat, and each of these checks watches a
failure mode that has either happened or provably can.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import duckdb

ARCHIVE = "data/hyxlab.duckdb"
STREAM = "data/hyxstream.duckdb"

_failures: list[str] = []
_lock_holder: str | None = None  # set by _connect_ro when a live writer holds the file


def check(name: str, ok: bool, detail: str = "") -> None:
    line = f"{'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else "")
    print(line, flush=True)
    if not ok:
        _failures.append(name)


def _connect_ro(path: str, retries: int = 5) -> duckdb.DuckDBPyConnection | None:
    """read-only connect with retry. Distinguishes a live writer holding
    the lock (normal: poly sweep holds it for hours) from a genuinely
    unreachable file — alarm fatigue trains people to ignore QA."""
    global _lock_holder
    _lock_holder = None
    for attempt in range(retries):
        try:
            return duckdb.connect(path, read_only=True)
        except duckdb.Error as exc:
            m = re.search(r"Conflicting lock is held in (\S+) \(PID (\d+)\)", str(exc))
            if m and Path(f"/proc/{m.group(2)}").exists():
                _lock_holder = f"{m.group(1)} pid {m.group(2)}"
            if attempt == retries - 1:
                return None
            time.sleep(2)  # writer burst (collector/tradepass flush)
    return None


def _reachable(conn, name: str) -> bool:
    """Emit the reachability check; lock held by a live writer is a PASS
    (checks skipped), anything else unreachable is a FAIL."""
    if conn is not None:
        return True
    if _lock_holder:
        check(name, True, f"skipped: live writer holds lock ({_lock_holder})")
    else:
        check(name, False, "unreachable and no live writer holds the lock")
    return False


def qa_stream(hours: float, path: str = STREAM) -> None:
    conn = _connect_ro(path)
    if not _reachable(conn, "stream archive reachable"):
        return
    now = datetime.now(UTC).replace(tzinfo=None)

    age = conn.execute("SELECT epoch(? - max(recv_ts)) FROM stream_trades", [now]).fetchone()[0]
    check(
        "stream fresh (trades < 5 min old)",
        age is not None and age < 300,
        f"age {age:.0f}s" if age is not None else "no trades",
    )

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

    # Reconstruct each Kalshi book from its time-latest snapshot image
    # (an image is one WS frame, so its rows share recv_ts) plus the
    # signed deltas received after it. seq is NOT usable as an ordering
    # key here: it is subscription-scoped and resets on every reconnect.
    # Pairs whose delta window a coverage gap intersects are skipped —
    # the book is legitimately unknown until the next snapshot re-seeds.
    # (Polymarket deltas carry absolute sizes, never negative; excluded.)
    neg = conn.execute(
        """
        WITH pair AS (
          -- One snapshot frame carries BOTH sides, so the baseline is
          -- keyed per market: a side legitimately empty in the newest
          -- image must not stay anchored to an older image's rows.
          SELECT market_id,
                 max(recv_ts) FILTER (kind='snap') AS snap_ts,
                 max(recv_ts) AS last_ts
          FROM book_events WHERE venue='kalshi' GROUP BY market_id
        ), eligible AS (
          SELECT market_id, snap_ts FROM pair
          WHERE snap_ts IS NOT NULL
            AND NOT EXISTS (
              SELECT 1 FROM stream_gaps g
              WHERE g.venue IN ('kalshi', '*') AND g.channel IN ('books', '*')
                AND g.started_at > pair.snap_ts AND g.started_at <= pair.last_ts)
        ), levels AS (
          SELECT e.market_id, e.side, e.price,
                 sum(CASE WHEN e.kind='snap' AND e.recv_ts = el.snap_ts THEN e.qty
                          WHEN e.kind='delta' AND e.recv_ts > el.snap_ts THEN e.qty
                          ELSE 0 END) AS qty
          FROM book_events e
          JOIN eligible el ON el.market_id = e.market_id
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
    if not _reachable(conn, "main archive reachable"):
        return
    now = datetime.now(UTC).replace(tzinfo=None)

    age = conn.execute("SELECT epoch(? - max(ts)) FROM snapshots", [now]).fetchone()[0]
    check(
        "collector fresh (snapshots < 20 min old)",
        age is not None and age < 1200,
        f"age {age:.0f}s" if age is not None else "no snapshots",
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

        # Enumeration-shrink tripwire: the 2026-07-08 Gamma offset cap
        # silently dropped the swept universe from ~4600 to ~2000 markets
        # and was caught by a lucky dead probe, not by QA. Compare the
        # last completed day's distinct swept markets against the prior
        # week's peak; a sharp drop means upstream pagination broke.
        yday, prior = conn.execute(
            """
            WITH daily AS (
              SELECT date_trunc('day', ts) AS d, count(DISTINCT market_id) AS cnt
              FROM poly_prices WHERE ts > ? - INTERVAL 9 DAY GROUP BY 1
            )
            SELECT
              (SELECT cnt FROM daily WHERE d = date_trunc('day', ? - INTERVAL 1 DAY)),
              (SELECT max(cnt) FROM daily WHERE d < date_trunc('day', ? - INTERVAL 1 DAY))
            """,
            [now, now, now],
        ).fetchone()
        # 0.5: the swept universe declines organically ~5%/day as markets
        # resolve (0.66 vs peak observed 2026-07-11, benign); the failure
        # class is a step-function halving, not a drift.
        if prior and prior > 500:
            check(
                "poly swept universe not shrinking",
                yday is not None and yday >= 0.5 * prior,
                f"yesterday {yday or 0} distinct markets vs prior-week peak {prior}",
            )

    # Signal feeds (B4): once a feed has ever pulled, its cadence must
    # hold. Guarded on non-empty so pre-first-pull archives stay green.
    n_vint = conn.execute("SELECT count(*) FROM econ_vintages").fetchone()[0]
    if n_vint:
        age_d = conn.execute(
            "SELECT epoch(? - max(knowable_at)) / 86400 FROM econ_vintages", [now]
        ).fetchone()[0]
        check("econ vintages fresh (< 8 days)", age_d < 8, f"age {age_d:.1f}d")
    n_news = conn.execute("SELECT count(*) FROM news_items WHERE source='gdelt'").fetchone()[0]
    if n_news:
        age_h = conn.execute(
            "SELECT epoch(? - max(knowable_at)) / 3600 FROM news_items WHERE source='gdelt'",
            [now],
        ).fetchone()[0]
        check("gdelt news fresh (< 30h)", age_h < 30, f"age {age_h:.1f}h")

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
