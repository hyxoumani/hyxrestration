"""Shadow harness: step/run equivalence, live-tail cursor semantics,
ledger persistence — all against synthetic archives, no network."""

import json
from datetime import UTC, datetime, timedelta

import duckdb

from collector.venues.kalshi_ws import parse_message
from hyxlab.models import MarketInfo, Order, Snapshot
from hyxlab.streamstore import StreamStore
from simulator.shadow import ShadowLedger, ShadowRunner
from simulator.sim import Simulator
from simulator.strategy import Strategy

T0 = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)


def snap(mid, ts, yes_bid, yes_ask):
    return Snapshot(
        venue="kalshi",
        market_id=mid,
        ts=ts,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=1 - yes_ask,
        no_ask=1 - yes_bid,
        yes_bid_size=100,
        yes_ask_size=100,
        no_bid_size=100,
        no_ask_size=100,
    )


class BuyFirst(Strategy):
    name = "buy_first"
    done = False

    def on_snapshot(self, s, ctx):
        if self.done:
            return []
        self.done = True
        return [Order(s.venue, s.market_id, "yes", 5, tif="IOC")]


def test_step_loop_equals_run():
    markets = {("kalshi", "M1"): MarketInfo(venue="kalshi", market_id="M1", result="yes")}
    snaps = [snap("M1", T0 + timedelta(seconds=i), 0.40, 0.41) for i in range(5)]

    r1 = Simulator(markets, [BuyFirst()], latency=1.0).run(snaps)
    sim2 = Simulator(markets, [BuyFirst()], latency=1.0)
    for s in snaps:
        sim2.step(s)
    r2 = sim2.finalize()
    assert json.dumps(r1.metrics, sort_keys=True, default=str) == json.dumps(
        r2.metrics, sort_keys=True, default=str
    )


def _snapshot_frame(mid, seq, bid_cents, ask_no_cents, ts):
    return parse_message(
        {
            "type": "orderbook_snapshot",
            "sid": 1,
            "seq": seq,
            "msg": {
                "market_ticker": mid,
                "yes_dollars_fp": [[f"{bid_cents / 100:.4f}", "100.00"]],
                "no_dollars_fp": [[f"{ask_no_cents / 100:.4f}", "100.00"]],
            },
        },
        ts,
    )[0]


def test_stream_reads_bound_duckdb_engine_below_cgroup_cap(tmp_path):
    """Regression (boot OOM-kills 2026-07-11 + 2026-07-12): shadow's
    stream-archive reads must cap DuckDB's memory_limit well under the
    unit's MemoryMax=1G — the engine default scales with SYSTEM RAM,
    and the seed-time ORDER BY got the daemon kernel-killed."""
    from simulator.shadow import stream_conn

    db = tmp_path / "stream.duckdb"
    StreamStore(db)  # schema, so a read-only connect succeeds
    with stream_conn(str(db)) as conn:
        settings = dict(
            conn.execute(
                "SELECT name, value FROM duckdb_settings()"
                " WHERE name IN ('memory_limit', 'threads', 'temp_directory')"
            ).fetchall()
        )
        read_only = conn.execute("SELECT current_setting('access_mode')").fetchone()[0]
    assert settings["memory_limit"] == "512.0 MiB"  # far below MemoryMax=1G
    assert settings["threads"] == "2"
    assert settings["temp_directory"] == "data/duckspill-shadow"  # spill, don't die
    assert read_only.lower() == "read_only"  # still connect_retry's read-only mode


def test_shadow_tails_only_the_future_and_persists_fills(tmp_path):
    stream_db = tmp_path / "stream.duckdb"
    archive_db = tmp_path / "archive.duckdb"
    shadow_db = tmp_path / "shadow.duckdb"

    from hyxlab.store import Store

    store = Store(archive_db)
    store.upsert_markets([MarketInfo(venue="kalshi", market_id="M1")])
    store.close()

    sstore = StreamStore(stream_db)
    # History BEFORE shadow starts: must never be traded.
    sstore.append_events(_snapshot_frame("M1", 1, 40, 59, T0))  # yes 0.40/0.41
    sstore.flush()

    runner = ShadowRunner(
        [BuyFirst()],
        latency=0.0,
        stream_db=str(stream_db),
        archive_db=str(archive_db),
        ledger=ShadowLedger(shadow_db),
    )
    assert runner.poll_once() == 0  # first poll only anchors the cursor
    assert runner.sim.result.fills == []

    # New events arrive after anchoring -> processed, filled, persisted.
    sstore.append_events(_snapshot_frame("M1", 2, 44, 55, T0 + timedelta(seconds=30)))
    sstore.flush()
    n = runner.poll_once()
    assert n == 1
    assert len(runner.sim.result.fills) == 1
    assert runner.sim.result.fills[0].price == 0.45  # 1 - 0.55 no bid
    with duckdb.connect(str(shadow_db), read_only=True) as conn:
        fills = conn.execute("SELECT strategy, price, qty FROM shadow_fills").fetchall()
        runs = conn.execute("SELECT count(*) FROM shadow_runs").fetchone()[0]
    assert fills == [("buy_first", 0.45, 5.0)]
    assert runs == 1

    # Idempotent persistence: nothing new -> no duplicate rows.
    runner.poll_once()
    with duckdb.connect(str(shadow_db), read_only=True) as conn:
        assert conn.execute("SELECT count(*) FROM shadow_fills").fetchone()[0] == 1


def test_shadow_gap_invalidates_books(tmp_path):
    stream_db = tmp_path / "stream.duckdb"
    archive_db = tmp_path / "archive.duckdb"

    from hyxlab.store import Store

    store = Store(archive_db)
    store.upsert_markets([MarketInfo(venue="kalshi", market_id="M1")])
    store.close()

    sstore = StreamStore(stream_db)
    sstore.append_events(_snapshot_frame("M1", 1, 40, 59, T0))
    sstore.flush()
    runner = ShadowRunner(
        [BuyFirst()],
        latency=0.0,
        stream_db=str(stream_db),
        archive_db=str(archive_db),
        ledger=ShadowLedger(tmp_path / "shadow.duckdb"),
    )
    runner.poll_once()  # anchor

    # A delta after a coverage gap must NOT produce a snapshot (book
    # unknown until re-seeded).
    t1 = T0 + timedelta(seconds=60)
    sstore.append_gap("kalshi", "books", t1, t1 + timedelta(seconds=1), "reconnect")
    delta = parse_message(
        {
            "type": "orderbook_delta",
            "sid": 1,
            "seq": 5,
            "msg": {
                "market_ticker": "M1",
                "price_dollars": "0.4400",
                "delta_fp": "10.00",
                "side": "yes",
                "ts_ms": int((t1 + timedelta(seconds=2)).timestamp() * 1000),
            },
        },
        t1 + timedelta(seconds=2),
    )[0]
    sstore.append_events(delta)  # parse_message returns the events LIST
    sstore.flush()
    assert runner.poll_once() == 0  # suppressed: unknown book
    # Fresh image re-seeds and flows again.
    sstore.append_events(_snapshot_frame("M1", 6, 44, 55, t1 + timedelta(seconds=10)))
    sstore.flush()
    assert runner.poll_once() == 1


def test_shadow_ignores_gaps_from_other_venues_and_channels(tmp_path):
    """A Polymarket reconnect (or a Kalshi trades-channel gap) must not
    blank Kalshi book state — books re-seed only on Kalshi reconnects,
    so an over-broad gap costs up to an hour of blindness per flap."""
    stream_db = tmp_path / "stream.duckdb"
    archive_db = tmp_path / "archive.duckdb"

    from hyxlab.store import Store

    store = Store(archive_db)
    store.upsert_markets([MarketInfo(venue="kalshi", market_id="M1")])
    store.close()

    sstore = StreamStore(stream_db)
    sstore.append_events(_snapshot_frame("M1", 1, 40, 59, T0))
    sstore.flush()
    runner = ShadowRunner(
        [BuyFirst()],
        latency=0.0,
        stream_db=str(stream_db),
        archive_db=str(archive_db),
        ledger=ShadowLedger(tmp_path / "shadow.duckdb"),
    )
    runner.poll_once()  # anchor

    t1 = T0 + timedelta(seconds=60)
    sstore.append_gap("polymarket", "market", t1, t1 + timedelta(seconds=1), "reconnect")
    sstore.append_gap("kalshi", "trades", t1, t1 + timedelta(seconds=1), "reconnect")
    delta = parse_message(
        {
            "type": "orderbook_delta",
            "sid": 1,
            "seq": 5,
            "msg": {
                "market_ticker": "M1",
                "price_dollars": "0.4000",
                "delta_fp": "10.00",
                "side": "yes",
                "ts_ms": int((t1 + timedelta(seconds=2)).timestamp() * 1000),
            },
        },
        t1 + timedelta(seconds=2),
    )[0]
    sstore.append_events(delta)
    sstore.flush()
    assert runner.poll_once() == 1  # book state survives foreign gaps


def test_shadow_bounds_in_memory_equity_curve(tmp_path):
    """Regression (mid-run OOM-kill 2026-07-18 22:03 UTC): the sim appends
    one equity point per snapshot and shadow runs forever — the in-memory
    curve reached ~800MB in 2.3 days and blew the unit's 1G cap. After each
    poll's ledger persist, at most ONE point may remain in memory; the full
    per-poll curve lives in shadow_equity."""
    stream_db = tmp_path / "stream.duckdb"
    archive_db = tmp_path / "archive.duckdb"
    shadow_db = tmp_path / "shadow.duckdb"

    from hyxlab.store import Store

    store = Store(archive_db)
    store.upsert_markets([MarketInfo(venue="kalshi", market_id="M1")])
    store.close()

    sstore = StreamStore(stream_db)
    sstore.append_events(_snapshot_frame("M1", 1, 40, 59, T0))
    sstore.flush()

    runner = ShadowRunner(
        [BuyFirst()],
        latency=0.0,
        stream_db=str(stream_db),
        archive_db=str(archive_db),
        ledger=ShadowLedger(shadow_db),
    )
    runner.poll_once()  # anchor
    for i in range(2, 8):  # 6 polls x 1 snapshot each (price moves, so none dedup)
        sstore.append_events(
            _snapshot_frame("M1", i, 40 + i, 55, T0 + timedelta(seconds=30 * i))
        )
        sstore.flush()
        runner.poll_once()
        assert len(runner.sim.result.equity_curve) <= 1
    with duckdb.connect(str(shadow_db), read_only=True) as conn:
        n_eq = conn.execute("SELECT count(*) FROM shadow_equity").fetchone()[0]
    assert n_eq == 6  # ledger still has every per-poll point


def test_max_drawdown_survives_equity_curve_trim():
    """max_drawdown is a running stat updated at append time, so trimming
    the in-memory curve mid-run (as shadow does) must not change it."""
    # Unsettled market (no result): the position marks to the bid, so the
    # mid-run price dip carves a genuine drawdown into the curve.
    markets = {("kalshi", "M1"): MarketInfo(venue="kalshi", market_id="M1")}
    prices = [(0.40, 0.41), (0.60, 0.61), (0.20, 0.21), (0.50, 0.51)]
    snaps = [
        snap("M1", T0 + timedelta(seconds=i), bid, ask) for i, (bid, ask) in enumerate(prices)
    ]

    ref = Simulator(markets, [BuyFirst()]).run(list(snaps))

    trimmed = Simulator(markets, [BuyFirst()])
    for s in snaps:
        trimmed.step(s)
        del trimmed.result.equity_curve[:-1]
    got = trimmed.finalize()

    assert ref.metrics["_portfolio"]["max_drawdown"] > 0  # non-vacuous
    assert (
        got.metrics["_portfolio"]["max_drawdown"] == ref.metrics["_portfolio"]["max_drawdown"]
    )


# -- the seed must stream, not materialize ---------------------------------

SEED_MARKETS = ["M1", "M2", "M3", "M4"]


class _NoFetchAll:
    """Connection proxy whose results refuse fetchall(). DUCK_MEM bounds
    the ENGINE; a fetchall() on the seed query builds an unbounded PYTHON
    list beside it, which is what actually crossed the 1G cgroup cap."""

    def __init__(self, inner):
        self._inner = inner

    class _Guard:
        def __init__(self, res):
            self._res = res

        def fetchall(self):
            raise AssertionError("seed materialized the whole result set via fetchall()")

        def __getattr__(self, name):
            return getattr(self._res, name)

    def execute(self, *a, **k):
        return self._Guard(self._inner.execute(*a, **k))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)


class _SqlLog:
    """Connection proxy recording every statement the seed executes.

    The seed's memory cost is a property of the PREDICATE, not of the row
    count, so it cannot be reproduced at fixture scale — assert the
    mechanism (a plain range `>=`, which DuckDB pushes into the scan)
    alongside the semantics."""

    def __init__(self, inner):
        self._inner = inner
        self.sql: list[str] = []

    def execute(self, sql, *a, **k):
        self.sql.append(sql)
        return self._inner.execute(sql, *a, **k)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)


def _seed_runner(tmp_path, n_events, monkeypatch=None, gap_after=None, sql_log=None):
    from hyxlab.store import Store

    stream_db = tmp_path / "stream.duckdb"
    archive_db = tmp_path / "archive.duckdb"
    store = Store(archive_db)
    store.upsert_markets([MarketInfo(venue="kalshi", market_id=m) for m in SEED_MARKETS])
    store.close()
    sstore = StreamStore(stream_db)
    # Spread across markets: repeated full snapshots of ONE market would
    # make every row but the last irrelevant, so a batch that silently
    # dropped rows would seed an identical book and the boundary control
    # below would be vacuous.
    for i in range(n_events):  # all pre-anchor history -> all seeded
        sstore.append_events(
            _snapshot_frame(
                SEED_MARKETS[i % len(SEED_MARKETS)],
                i + 1,
                40 + (i % 5),
                59 - (i % 5),
                T0 + timedelta(seconds=i),
            )
        )
    if gap_after is not None:
        # A book gap `gap_after` events in: the seed floor is the gap's end,
        # so only the events at or after it are replayed.
        end = T0 + timedelta(seconds=gap_after)
        sstore.append_gap("kalshi", "books", end - timedelta(seconds=1), end, "reconnect")
    sstore.flush()
    if monkeypatch is not None:
        import simulator.shadow as shadow_mod

        real = shadow_mod.stream_conn
        if sql_log is not None:
            monkeypatch.setattr(
                shadow_mod, "stream_conn", lambda p: sql_log.append(_SqlLog(real(p))) or sql_log[-1]
            )
        else:
            monkeypatch.setattr(shadow_mod, "stream_conn", lambda p: _NoFetchAll(real(p)))
    return ShadowRunner(
        [BuyFirst()],
        latency=0.0,
        stream_db=str(stream_db),
        archive_db=str(archive_db),
        ledger=ShadowLedger(tmp_path / "shadow.duckdb"),
    )


def test_seed_does_not_materialize_the_whole_result_set(tmp_path, monkeypatch):
    """Load-bearing: the seed path must never call fetchall(). The window
    is 'since the last book gap', and promote.sh restarts stream and
    shadow together — if shadow reads the floor before stream writes its
    daemon_start row it seeds from the PREVIOUS break. Measured at the
    2026-07-31 20:34 promote: 2,084,503 rows (~417MB of tuples) instead
    of 21,419, kernel-OOM-killed at boot. Pre-fix this test raises."""
    runner = _seed_runner(tmp_path, 12, monkeypatch=monkeypatch)
    assert runner.poll_once() == 0  # first poll anchors + seeds, trades nothing
    assert runner.sim.result.fills == []  # history is seeded, never traded
    assert runner.replayer.depth("M1") is not None  # and it DID seed


def test_seed_is_identical_across_batch_boundaries(tmp_path, monkeypatch):
    """Discrimination control: streaming must not drop or duplicate rows
    at a fetchmany() boundary. A batch size that does not divide the row
    count must seed the same book as one that swallows it whole."""
    import simulator.shadow as shadow_mod

    monkeypatch.setattr(shadow_mod, "SEED_BATCH", 10_000)
    whole = _seed_runner(tmp_path / "a", 12)
    whole.poll_once()
    monkeypatch.setattr(shadow_mod, "SEED_BATCH", 5)  # 12 rows -> 5 + 5 + 2
    split = _seed_runner(tmp_path / "b", 12)
    split.poll_once()
    assert {m: split.replayer.depth(m) for m in SEED_MARKETS} == {
        m: whole.replayer.depth(m) for m in SEED_MARKETS
    }
    assert all(split.replayer.depth(m) is not None for m in SEED_MARKETS)  # non-vacuous


def test_seed_floor_is_a_plain_range_predicate(tmp_path, monkeypatch):
    """Load-bearing: bounding the RESULT SET is not bounding the SCAN.
    `recv_ts >= coalesce(?, recv_ts)` reads the column on its right side,
    so DuckDB cannot push it into the scan as a min/max filter and
    evaluates it over every archived row. Measured on the live archive for
    an identical 9,387-row result: 685MB / 0.8s with the coalesce against
    157MB / 0.16s with a plain `>=` — a constant cost, invariant to the
    window, and the boot peak that left ~9% headroom under the 1G cap.

    Asserts the mechanism (the memory effect needs 170M rows) and the
    semantics: with a book gap 9 events in, only the events at or after it
    are replayed, so M1 — whose snapshots are all pre-floor — is unseeded.
    """
    log: list = []
    runner = _seed_runner(tmp_path, 12, monkeypatch=monkeypatch, gap_after=9, sql_log=log)
    assert runner.poll_once() == 0
    seed_sql = [s for s in log[0].sql if "book_events" in s and "ORDER BY" in s]
    assert seed_sql and "recv_ts >= ?" in seed_sql[0]
    assert "coalesce" not in seed_sql[0].lower()
    assert runner.replayer.depth("M1") is None  # pre-floor history NOT replayed
    assert all(runner.replayer.depth(m) is not None for m in ("M2", "M3", "M4"))


def test_seed_with_no_gap_row_reads_the_whole_archive(tmp_path, monkeypatch):
    """Discrimination control: with no book gap the floor is NULL and the
    seed must read EVERYTHING — `recv_ts` is NOT NULL, so dropping the
    predicate is exactly equivalent to the coalesce it replaces. Binding a
    NULL floor into a plain `recv_ts >= ?` instead returns zero rows and
    leaves every book unseeded, which is what this test reddens on."""
    log: list = []
    runner = _seed_runner(tmp_path, 12, monkeypatch=monkeypatch, gap_after=None, sql_log=log)
    assert runner.poll_once() == 0
    seed_sql = [s for s in log[0].sql if "book_events" in s and "ORDER BY" in s]
    assert seed_sql and "recv_ts" not in seed_sql[0].split("WHERE", 1)[1].split("ORDER BY")[0]
    assert all(runner.replayer.depth(m) is not None for m in SEED_MARKETS)


# -- settlement is reachable in the daemon ---------------------------------
#
# `_settle` is called only from `finalize()`, which sits AFTER the `while`
# loop in main(). The unit runs with no --duration, so that loop is
# `while True` and finalize is unreachable in production: the live daemon
# never credited a payout, never retired a settled contract and never
# produced a settlement record, however long it lived. At 100% unobserved
# outcome coverage that was invisible, but it is a separate and PRIOR
# cause — the path was not merely untriggered by the data, it was unwired.


def _settling_runner(tmp_path):
    """Runner holding 5 YES in M1, which the archive has not yet settled."""
    from hyxlab.store import Store

    stream_db = tmp_path / "stream.duckdb"
    archive_db = tmp_path / "archive.duckdb"
    shadow_db = tmp_path / "shadow.duckdb"

    store = Store(archive_db)
    store.upsert_markets([MarketInfo(venue="kalshi", market_id="M1")])
    store.close()

    sstore = StreamStore(stream_db)
    sstore.append_events(_snapshot_frame("M1", 1, 40, 59, T0))
    sstore.flush()

    runner = ShadowRunner(
        [BuyFirst()],
        latency=0.0,
        stream_db=str(stream_db),
        archive_db=str(archive_db),
        ledger=ShadowLedger(shadow_db),
    )
    runner.poll_once()  # anchors the cursor
    sstore.append_events(_snapshot_frame("M1", 2, 44, 55, T0 + timedelta(seconds=30)))
    sstore.flush()
    runner.poll_once()  # fills 5 yes
    assert len(runner.sim.result.fills) == 1
    return runner, archive_db, shadow_db


def _land_the_result(runner, archive_db, result="yes"):
    """The daily sweep writes `result` hours after the market closes; the
    daemon picks it up on its hourly metadata refresh. Reproduce that
    ordering rather than mutating sim.markets directly."""
    from hyxlab.store import Store

    store = Store(archive_db)
    store.upsert_markets([MarketInfo(venue="kalshi", market_id="M1", result=result)])
    store.close()
    runner._markets_loaded_at = float("-inf")  # force the hourly refresh


def test_shadow_settles_without_finalize(tmp_path):
    """Load-bearing: assert the NUMBERS after a poll — cash carries the
    payout and the position is retired — with finalize() never called, so
    a daemon that only settles at shutdown fails on arithmetic rather than
    on a missing row."""
    runner, archive_db, shadow_db = _settling_runner(tmp_path)
    cash_before = runner.sim.result.cash
    assert runner.sim.ctx._positions[("buy_first", "kalshi", "M1", "yes")] == 5.0

    _land_the_result(runner, archive_db)
    runner.poll_once()

    assert runner.sim.result.cash == cash_before + 5.0  # 5 YES pay 1.00 each
    assert runner.sim.ctx._positions[("buy_first", "kalshi", "M1", "yes")] == 0.0
    with duckdb.connect(str(shadow_db), read_only=True) as conn:
        rows = conn.execute(
            "SELECT strategy, market_id, side, qty, result, payout FROM shadow_settlements"
        ).fetchall()
    assert rows == [("buy_first", "M1", "yes", 5.0, "yes", 5.0)]


def test_shadow_settlement_persists_once_across_polls(tmp_path):
    """The daemon settles every poll and polls every 20s forever. The row
    must be written once — a per-poll duplicate would grow without bound
    and inflate any payout summed from the ledger."""
    runner, archive_db, shadow_db = _settling_runner(tmp_path)
    _land_the_result(runner, archive_db)
    for _ in range(4):
        runner.poll_once()
    with duckdb.connect(str(shadow_db), read_only=True) as conn:
        assert conn.execute("SELECT count(*) FROM shadow_settlements").fetchone()[0] == 1


def test_shadow_records_a_settled_loser(tmp_path):
    """Discrimination control: a loser moves no cash, so a settlement path
    keyed on a payout or on a cash delta records nothing here — and the
    reconstruction then carries the loser as open forever."""
    runner, archive_db, shadow_db = _settling_runner(tmp_path)
    cash_before = runner.sim.result.cash
    _land_the_result(runner, archive_db, result="no")
    runner.poll_once()
    assert runner.sim.result.cash == cash_before  # no payout
    assert runner.sim.ctx._positions[("buy_first", "kalshi", "M1", "yes")] == 0.0
    with duckdb.connect(str(shadow_db), read_only=True) as conn:
        rows = conn.execute("SELECT qty, result, payout FROM shadow_settlements").fetchall()
    assert rows == [(5.0, "no", 0.0)]


def test_shadow_writes_no_settlement_while_the_market_is_open(tmp_path):
    """Control: settling per poll must not retire UNSETTLED positions. The
    archive has no result yet, which is the state the daemon spends almost
    all of its time in."""
    runner, _, shadow_db = _settling_runner(tmp_path)
    for _ in range(3):
        runner.poll_once()
    assert runner.sim.ctx._positions[("buy_first", "kalshi", "M1", "yes")] == 5.0
    with duckdb.connect(str(shadow_db), read_only=True) as conn:
        assert conn.execute("SELECT count(*) FROM shadow_settlements").fetchone()[0] == 0


def test_metadata_reload_is_filtered_but_pins_held_markets(tmp_path):
    """Regression (2026-08-07): the unfiltered markets() dict reached
    ~430MB and the hourly reload double-holds it — the daemon sat ~35MB
    under its 1G cgroup cap. The reload must load only live-ish kalshi
    metadata, EXCEPT markets the sim still holds: a held market whose
    result lands after the recency window (the weeks-out macro cohorts)
    must stay visible to _settle or its payout is never credited."""
    from datetime import datetime as _dt

    from hyxlab.store import Store

    runner, archive_db, _ = _settling_runner(tmp_path)
    old = _dt(2026, 1, 1)
    store = Store(archive_db)
    store.upsert_markets(
        [
            # held by the sim: settled long ago, must be pinned in anyway
            MarketInfo(venue="kalshi", market_id="M1", result="yes", close_time=old),
            # not held, settled long ago: the memory the filter exists to shed
            MarketInfo(venue="kalshi", market_id="STALE", result="no", close_time=old),
            # wrong venue for the kalshi-only stream tail
            MarketInfo(venue="polymarket", market_id="P1"),
        ]
    )
    store.close()
    runner._markets_loaded_at = float("-inf")  # force the hourly refresh
    runner.poll_once()
    assert ("kalshi", "STALE") not in runner.sim.markets
    assert ("polymarket", "P1") not in runner.sim.markets
    assert ("kalshi", "M1") in runner.sim.markets  # pinned while held
    # and the pinned metadata actually settled the position
    assert runner.sim.ctx._positions[("buy_first", "kalshi", "M1", "yes")] == 0.0
