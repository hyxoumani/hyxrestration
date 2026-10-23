"""QA's two book-table checks, bounded for the stream coverage expansion.

Both are pinned against the queries they replaced, kept here verbatim as
oracles: the seq check's sliced walk must find exactly the holes the
whole-window query found (holes straddling slice boundaries, resets, repeated
seqs and several sids included), and the non-negative-levels check must give
the whole-table answer for every market active in the QA window.
"""

from __future__ import annotations

import contextlib
import random
import re
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from collector import qa
from hyxlab.streamstore import BookEvent, StreamStore

NOW = datetime(2026, 10, 23, 10, 0, 0)

OLD_SEQ = "\n        WITH ev AS (\n          SELECT DISTINCT sid, seq, recv_ts FROM book_events\n          WHERE recv_ts > ? - INTERVAL 1 HOUR * CAST(? AS INTEGER)\n            AND sid IS NOT NULL AND seq IS NOT NULL\n        ), marked AS (\n          SELECT sid, seq, recv_ts, CASE WHEN seq < lag(seq)\n                   OVER (PARTITION BY sid ORDER BY recv_ts, seq)\n                 THEN 1 ELSE 0 END AS reset\n          FROM ev\n        ), runs AS (\n          SELECT sid, seq, recv_ts, sum(reset) OVER (\n                   PARTITION BY sid ORDER BY recv_ts, seq\n                   ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS run\n          FROM marked\n        ), uniq AS (\n          SELECT sid, run, seq, min(recv_ts) AS recv_ts\n          FROM runs GROUP BY sid, run, seq\n        )\n        SELECT seq - lag(seq) OVER w - 1 AS missing,\n               lag(recv_ts) OVER w AS t0, recv_ts AS t1\n        FROM uniq WINDOW w AS (PARTITION BY sid, run ORDER BY seq)\n        QUALIFY missing > 0\n        "

OLD_NEG = "\n        WITH pair AS (\n          -- One snapshot frame carries BOTH sides, so the baseline is\n          -- keyed per market: a side legitimately empty in the newest\n          -- image must not stay anchored to an older image's rows.\n          SELECT market_id,\n                 max(recv_ts) FILTER (kind='snap') AS snap_ts,\n                 max(recv_ts) AS last_ts\n          FROM book_events WHERE venue='kalshi' GROUP BY market_id\n        ), eligible AS (\n          SELECT market_id, snap_ts FROM pair\n          WHERE snap_ts IS NOT NULL\n            AND NOT EXISTS (\n              SELECT 1 FROM stream_gaps g\n              WHERE g.venue IN ('kalshi', '*') AND g.channel IN ('books', '*')\n                AND g.started_at > pair.snap_ts AND g.started_at <= pair.last_ts)\n        ), levels AS (\n          SELECT e.market_id, e.side, e.price,\n                 sum(CASE WHEN e.kind='snap' AND e.recv_ts = el.snap_ts THEN e.qty\n                          WHEN e.kind='delta' AND e.recv_ts > el.snap_ts THEN e.qty\n                          ELSE 0 END) AS qty\n          FROM book_events e\n          JOIN eligible el ON el.market_id = e.market_id\n          GROUP BY e.market_id, e.side, e.price\n        )\n        SELECT count(*) FROM levels WHERE qty < -1e-9\n        "


def _book(db, rows):
    st = StreamStore(db)
    st.append_events(rows)
    st.flush()
    return duckdb.connect(str(db), read_only=True)


def _seq_fixture(seed: int) -> list[BookEvent]:
    """Three sids over 26h: steady flow with random holes, reconnect resets,
    repeated seqs and multi-row (snapshot-like) frames."""
    rnd = random.Random(seed)
    rows = []
    for sid in (1, 1_000_001, 2_000_001):
        seq, ts = 1, NOW - timedelta(hours=26) + timedelta(seconds=rnd.random())
        while ts < NOW:
            r = rnd.random()
            if r < 0.01:
                seq += rnd.randint(2, 6)  # a hole
            elif r < 0.012:
                seq = 1  # reconnect: seq restarts
            elif r < 0.02:
                seq -= 1  # a repeated seq
            n = 3 if rnd.random() < 0.05 else 1  # snapshot frame: rows share seq/ts
            for k in range(n):
                rows.append(
                    BookEvent("kalshi", f"M{sid}", ts, None, sid, seq, "delta", "yes", 0.5, k)
                )
            seq += 1
            ts += timedelta(seconds=rnd.expovariate(1 / 40))
    return rows


@pytest.mark.parametrize("seed", [1, 2, 3])
@pytest.mark.parametrize("slice_h", [1, 2, 5])
def test_sliced_seq_walk_finds_exactly_the_whole_window_holes(tmp_path, monkeypatch, seed, slice_h):
    conn = _book(tmp_path / "s.duckdb", _seq_fixture(seed))
    old = conn.execute(OLD_SEQ, [NOW, 26]).fetchall()
    monkeypatch.setattr(qa, "SEQ_SLICE_HOURS", slice_h)
    new = qa._seq_holes(conn, 26, NOW)
    assert len(old) > 20  # non-vacuous
    assert sorted(new) == sorted(old)


def test_sliced_seq_walk_includes_rows_written_after_now(tmp_path):
    rows = [
        BookEvent("kalshi", "M", NOW - timedelta(minutes=1), None, 1, 1, "delta", "yes", 0.5, 1),
        BookEvent("kalshi", "M", NOW + timedelta(seconds=5), None, 1, 4, "delta", "yes", 0.5, 1),
    ]
    conn = _book(tmp_path / "s.duckdb", rows)
    assert [h[0] for h in qa._seq_holes(conn, 26, NOW)] == [2]
    assert [h[0] for h in conn.execute(OLD_SEQ, [NOW, 26]).fetchall()] == [2]


def _neg_rows():
    rows = []
    t = NOW - timedelta(hours=30)
    for m in range(6):
        mid = f"KX-M{m}"
        rows.append(BookEvent("kalshi", mid, t, None, 1, 1, "snap", "yes", 0.40, 10.0))
        rows.append(BookEvent("kalshi", mid, t, None, 1, 1, "snap", "no", 0.55, 10.0))
        for k in range(1, 8):  # m even: stays >= 0; m odd: driven negative
            q = -2.0 if m % 2 else -1.0
            ts = NOW - timedelta(hours=30 - 4 * k)
            rows.append(BookEvent("kalshi", mid, ts, None, 1, 1 + k, "delta", "yes", 0.40, q))
    # a market dead for weeks, negative long ago: its verdict cannot change
    old = NOW - timedelta(days=20)
    rows.append(BookEvent("kalshi", "KX-OLD", old, None, 1, 1, "snap", "yes", 0.4, 1.0))
    rows.append(
        BookEvent(
            "kalshi", "KX-OLD", old + timedelta(hours=1), None, 1, 2, "delta", "yes", 0.4, -5.0
        )
    )
    return rows


def _new_neg(conn):
    """The bounded check's two statements, as `_stream_checks` runs them."""
    src = Path(qa.__file__).read_text()
    b = src.index("        WITH recent AS NOT MATERIALIZED (")
    sql = src[b : src.index('"""', b)]
    active = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT market_id FROM book_events WHERE venue='kalshi'"
            " AND recv_ts > ? - INTERVAL 1 HOUR * CAST(? AS INTEGER)",
            [NOW, 26],
        ).fetchall()
    ]
    return conn.execute(sql, [NOW, qa.NEG_LOOKBACK_DAYS, active]).fetchone()[0]


def test_bounded_negative_levels_matches_the_whole_table_on_active_markets(tmp_path):
    rows = [r for r in _neg_rows() if r.market_id != "KX-OLD"]
    conn = _book(tmp_path / "a.duckdb", rows)
    whole = conn.execute(OLD_NEG).fetchone()[0]
    assert whole == 3  # non-vacuous: the three odd markets go negative
    assert _new_neg(conn) == whole


def test_bounded_negative_levels_skips_only_markets_with_no_row_in_the_window(tmp_path):
    conn = _book(tmp_path / "b.duckdb", _neg_rows())
    assert conn.execute(OLD_NEG).fetchone()[0] == 4  # the dead market re-counted daily
    assert _new_neg(conn) == 3  # ...which no new row could have changed


def test_stream_checks_still_report_both_lines(tmp_path, monkeypatch):
    """Names and detail shapes are what the QA journal pre-registers."""
    st = StreamStore(tmp_path / "s.duckdb")
    st.append_events(_neg_rows())
    st.flush()
    seen = {}
    monkeypatch.setattr(
        qa, "check", lambda name, ok, detail="": seen.setdefault(name, (ok, detail))
    )
    # later checks need tables this fixture leaves empty
    with (
        duckdb.connect(str(tmp_path / "s.duckdb"), read_only=True) as conn,
        contextlib.suppress(Exception),
    ):
        qa._stream_checks(conn, 26, NOW)
    ok, detail = seen["reconstructed book levels non-negative"]
    assert not ok and detail == "3 negative levels"
    ok, detail = seen["book seq contiguous or gap-marked"]
    assert re.fullmatch(
        r"\d+ missing seq in \d+ hole events, \d+ own-channel gap rows,"
        r" \d+ unexcused \(\d+ seq\)",
        detail,
    )
