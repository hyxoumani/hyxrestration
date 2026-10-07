"""`qa._poly_replay` is the range-join replay, made linear -- and nothing else.

The range form (kept below VERBATIM as the oracle) cost sum(intervals_m x
rows_m), so one market pushing a full-book snapshot every ~1s took the
2026-10-07 QA run from ~30s to 62 minutes, holding streamd's archive the
whole time; the 1.69M rows that spilled during it then OOM-looped streamd
for 1h53m (mistakes #112). The ASOF form must return the same six numbers on
any input. The randomized fixture is built to hit the joins' edges:
a delta at exactly a frame's timestamp (in no interval: `> a` and `< b` are
both strict), deltas before the first frame and after the last, gaps that
excuse the containing interval but not its neighbours, and the
venue-wide `*` gap row. (`stream_gaps.ended_at` is NOT NULL, so the
queries' `coalesce` over it has no input that reaches it.)
"""

from __future__ import annotations

import random
import time
from datetime import UTC, datetime, timedelta

import duckdb
import pytest

from collector import qa
from hyxlab.streamstore import BookEvent, StreamStore

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)

ORACLE_REPLAY = """
            WITH frame AS (
              SELECT DISTINCT market_id, recv_ts FROM book_events
              WHERE venue='polymarket' AND kind='snap'
                AND recv_ts > ? - INTERVAL 1 HOUR * CAST(? AS INTEGER)
            ), iv AS (
              SELECT market_id, recv_ts AS a,
                     lead(recv_ts) OVER (PARTITION BY market_id ORDER BY recv_ts) AS b
              FROM frame QUALIFY b IS NOT NULL
            ), clean AS (
              SELECT * FROM iv WHERE NOT EXISTS (
                SELECT 1 FROM stream_gaps g
                WHERE g.venue IN ('polymarket', '*') AND g.channel IN ('market', '*')
                  AND g.started_at <= iv.b AND coalesce(g.ended_at, iv.b) >= iv.a)
            ), src AS (
              SELECT c.market_id, c.a, c.b, e.side, e.price, e.qty, e.recv_ts, e.kind
              FROM clean c JOIN book_events e
                ON e.venue='polymarket' AND e.market_id = c.market_id
               AND ((e.kind='snap' AND e.recv_ts = c.a)
                 OR (e.kind='delta' AND e.recv_ts > c.a AND e.recv_ts < c.b))
            ), tested AS (
              SELECT market_id, a, b FROM src WHERE kind='delta' GROUP BY 1, 2, 3
            ), recon AS (
              SELECT s.market_id, s.a, s.side, s.price, arg_max(s.qty, s.recv_ts) AS qty
              FROM src s JOIN tested t USING (market_id, a, b)
              GROUP BY 1, 2, 3, 4
            ), truth AS (
              SELECT t.market_id, t.a, e.side, e.price, e.qty
              FROM tested t JOIN book_events e
                ON e.venue='polymarket' AND e.market_id = t.market_id
               AND e.kind='snap' AND e.recv_ts = t.b
            ), cmp AS (
              SELECT coalesce(r.market_id, u.market_id) AS market_id,
                     coalesce(r.a, u.a) AS a,
                     CASE WHEN abs(coalesce(r.qty, 0) - coalesce(u.qty, 0)) < 1e-6
                          THEN 1 ELSE 0 END AS ok
              FROM (SELECT * FROM recon WHERE qty > 0) r
              FULL OUTER JOIN (SELECT * FROM truth WHERE qty > 0) u
                ON r.market_id = u.market_id AND r.a = u.a AND r.side = u.side
               AND abs(r.price - u.price) < 1e-9
            )
            SELECT count(*), coalesce(sum(ok), 0), count(DISTINCT (market_id, a)),
                   count(DISTINCT CASE WHEN ok = 0 THEN (market_id, a) END)
            FROM cmp

"""

ORACLE_PAIRS = """
            WITH d AS (
              SELECT count(*) AS n FROM book_events
              WHERE venue='polymarket' AND kind='delta'
                AND recv_ts > ? - INTERVAL 1 HOUR * CAST(? AS INTEGER)
            ), frame AS (
              SELECT DISTINCT market_id, recv_ts FROM book_events
              WHERE venue='polymarket' AND kind='snap'
                AND recv_ts > ? - INTERVAL 1 HOUR * CAST(? AS INTEGER)
            ), iv AS (
              SELECT market_id, recv_ts AS a,
                     lead(recv_ts) OVER (PARTITION BY market_id ORDER BY recv_ts) AS b
              FROM frame QUALIFY b IS NOT NULL
            )
            SELECT d.n, (SELECT count(*) FROM iv WHERE EXISTS (
                     SELECT 1 FROM book_events e
                     WHERE e.venue='polymarket' AND e.kind='delta'
                       AND e.market_id = iv.market_id
                       AND e.recv_ts > iv.a AND e.recv_ts < iv.b)) FROM d

"""


def _oracle(conn, now, hours):
    h = int(hours)
    levels, agree, ivals, bad = conn.execute(ORACLE_REPLAY, [now, h]).fetchone()
    deltas, pairs = conn.execute(ORACLE_PAIRS, [now, h, now, h]).fetchone()
    return levels, agree, ivals, bad, deltas, pairs


def _random_stream(path, seed):
    rng = random.Random(seed)
    store = StreamStore(path)
    events = []
    for m in range(rng.randint(1, 4)):
        market = f"tok{m}"
        t = NOW - timedelta(hours=rng.choice([2, 30]))  # some frames fall outside 26h
        frames = []
        for _ in range(rng.randint(2, 12)):
            t += timedelta(seconds=rng.randint(1, 600))
            frames.append(t)
            for side in ("bid", "ask"):
                for p in rng.sample([0.1, 0.2, 0.3, 0.4], rng.randint(0, 3)):
                    events.append(
                        BookEvent(
                            "polymarket",
                            market,
                            t,
                            None,
                            None,
                            None,
                            "snap",
                            side,
                            p,
                            float(rng.randint(0, 5)),
                        )
                    )
        lo, hi = frames[0] - timedelta(seconds=300), frames[-1] + timedelta(seconds=300)
        for _ in range(rng.randint(0, 25)):
            if rng.random() < 0.2:
                ts = rng.choice(frames)  # a delta ON a frame boundary
            else:
                ts = lo + timedelta(seconds=rng.uniform(0, (hi - lo).total_seconds()))
            events.append(
                BookEvent(
                    "polymarket",
                    market,
                    ts,
                    None,
                    None,
                    None,
                    "delta",
                    rng.choice(("bid", "ask")),
                    rng.choice([0.1, 0.2, 0.3, 0.4]),
                    float(rng.randint(0, 5)),
                )
            )
        for _ in range(rng.randint(0, 2)):
            a = rng.choice(frames) + timedelta(seconds=rng.randint(-30, 30))
            b = a + timedelta(seconds=rng.randint(1, 200))
            store.append_gap(
                rng.choice(("polymarket", "*")), rng.choice(("market", "*")), a, b, "test"
            )
    store.append_events(events)
    store.flush()


@pytest.mark.parametrize("seed", range(40))
def test_asof_replay_matches_the_range_join_oracle(tmp_path, seed):
    path = tmp_path / "s.duckdb"
    _random_stream(path, seed)
    with duckdb.connect(str(path), read_only=True) as conn:
        assert qa._poly_replay(conn, NOW, 26.0) == _oracle(conn, NOW, 26.0)


def test_the_oracle_fixture_is_not_vacuous(tmp_path):
    """Equality of two zeros proves nothing: across the seeds the fixture
    must produce tested intervals, inexact ones, and gap-excused pairs."""
    seen = {"ivals": 0, "bad": 0, "excused": 0}
    for seed in range(40):
        path = tmp_path / f"s{seed}.duckdb"
        _random_stream(path, seed)
        with duckdb.connect(str(path), read_only=True) as conn:
            levels, agree, ivals, bad, deltas, pairs = _oracle(conn, NOW, 26.0)
        seen["ivals"] += ivals
        seen["bad"] += bad
        seen["excused"] += pairs - ivals
    assert all(v > 0 for v in seen.values()), seen


def test_one_market_snapshotting_every_second_stays_linear(tmp_path):
    """The 10-07 shape, scaled down: one market, a full ~80-level book every
    second for an hour, one delta per interval. The range form is quadratic
    in exactly this; the ASOF form must not care."""
    path = tmp_path / "s.duckdb"
    store = StreamStore(path)
    t0 = NOW - timedelta(hours=1)
    events = []
    for i in range(3600):
        t = t0 + timedelta(seconds=i)
        events.extend(
            BookEvent("polymarket", "hot", t, None, None, None, "snap", side, k / 100, 10.0)
            for side in ("bid", "ask")
            for k in range(1, 41)
        )
        events.append(
            BookEvent(
                "polymarket",
                "hot",
                t + timedelta(milliseconds=500),
                None,
                None,
                None,
                "delta",
                "bid",
                0.01,
                10.0,
            )
        )
    store.append_events(events)
    store.flush()
    with duckdb.connect(str(path), read_only=True) as conn:
        started = time.monotonic()
        levels, agree, ivals, bad, deltas, pairs = qa._poly_replay(conn, NOW, 26.0)
        took = time.monotonic() - started
    assert (ivals, bad, deltas, pairs) == (3599, 0, 3600, 3599)
    assert took < 1.0, f"{took:.2f}s"  # measured: 0.04s; the range form 7.99s
