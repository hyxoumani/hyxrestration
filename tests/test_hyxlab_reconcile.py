"""Archive reconciliation pass (EXP-934).

The pass exists to REPAIR holes before Kalshi's retention clock makes them
permanent, so the properties that matter are not "does it fetch" but:

  * the DIFF is right against synthetic archives (both databases),
  * work is ordered by TIME-TO-PURGE, not alphabetically (the sweep's
    alphabetical order starved KXBTC*/KXETH*/KXSOL* and all of Exotics),
  * per-ITEM budgets exist, so one huge family cannot eat a run,
  * a second run finds nothing new (idempotency),
  * requests are PACED and no HTTP happens under `data/writer.lock`,
  * an UNREPAIRABLE hole is RECORDED in the completeness ledger, not
    silently skipped — and an incomplete repair can never report complete.

No network: every HTTP call is a fixture.
"""

from __future__ import annotations

import fcntl
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

import collector.reconcile as rec
from collector.venues import kalshi
from hyxlab.store import Store

NOW = datetime(2026, 8, 3, 6, 0, tzinfo=UTC)
LEDGER_TOOL = Path(rec.LEDGER_TOOL)


# ---------------------------------------------------------------------------
# synthetic archives
# ---------------------------------------------------------------------------


def _make_lab(path: Path, archived: list[str], series: dict[str, str]) -> str:
    store = Store(str(path))
    store.conn.executemany(
        "INSERT INTO series VALUES (?,?,?,?,?,?,?,?)",
        [("kalshi", t, t, c, "", None, "daily", None) for t, c in series.items()],
    )
    if archived:
        store.conn.executemany(
            "INSERT INTO markets (venue, market_id, series) VALUES ('kalshi', ?, ?)",
            [(m, rec.series_of(m)) for m in archived],
        )
    store.close()
    return str(path)


def _make_stream(path: Path, trades: list[tuple[str, datetime]]) -> str:
    con = duckdb.connect(str(path))
    con.execute(
        """CREATE TABLE stream_trades (
            venue VARCHAR, market_id VARCHAR, recv_ts TIMESTAMP, src_ts TIMESTAMP,
            price DOUBLE, qty DOUBLE, taker_side VARCHAR, seq BIGINT)"""
    )
    con.executemany(
        "INSERT INTO stream_trades VALUES ('kalshi', ?, ?, ?, 0.5, 1.0, 'yes', 1)",
        [(m, ts.replace(tzinfo=None), ts.replace(tzinfo=None)) for m, ts in trades],
    )
    con.close()
    return str(path)


def _mkt(ticker: str) -> dict:
    return {
        "ticker": ticker,
        "event_ticker": f"{rec.series_of(ticker)}-26JUL10",
        "title": ticker,
        "status": "settled",
        "result": "no",
        "open_time": "2026-07-10T12:00:00Z",
        "close_time": "2026-07-11T04:59:00Z",
    }


class _FakeResp:
    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status
        self.headers: dict[str, str] = {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")


class _Session:
    """Serves only the tickers in `alive`; anything else is 'purged'."""

    def __init__(self, alive: set[str], lock_file: str | None = None):
        self.alive = alive
        self.calls: list[dict] = []
        self.lock_file = lock_file
        self.lock_held_during_http = False

    def _check_lock(self):
        if not self.lock_file:
            return
        with open(self.lock_file, "a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(f, fcntl.LOCK_UN)
            except OSError:
                self.lock_held_during_http = True

    def get(self, url, params=None, timeout=None):
        params = params or {}
        self.calls.append({"url": url, "params": params})
        self._check_lock()
        if url.endswith("/markets"):
            want = [t for t in params["tickers"].split(",")]
            return _FakeResp({"markets": [_mkt(t) for t in want if t in self.alive], "cursor": ""})
        if url.endswith("/trades"):
            return _FakeResp({"trades": [], "cursor": ""})
        if "candlesticks" in url:
            return _FakeResp({"candlesticks": []})
        raise AssertionError(f"unexpected URL {url}")


# ---------------------------------------------------------------------------
# 1. the diff
# ---------------------------------------------------------------------------


def test_diff_finds_traded_markets_absent_from_the_archive(tmp_path):
    lab = _make_lab(
        tmp_path / "lab.duckdb",
        archived=["KXHIGHNY-26JUL10-B80.5"],
        series={"KXHIGHNY": "Climate and Weather", "KXBTCD": "Crypto",
                "KXMLBHR": "Sports"},
    )
    old = NOW - timedelta(days=10)
    stream = _make_stream(
        tmp_path / "stream.duckdb",
        [("KXHIGHNY-26JUL10-B80.5", old),      # archived -> not missing
         ("KXHIGHNY-26JUL10-B82.5", old),      # missing, in scope
         ("KXBTCD-26JUL10-T50", old),          # missing, in scope
         ("KXMLBHR-26JUL10-T2", old),          # missing, OUT of scope (Sports)
         ("KXHIGHNY-26AUG03-B90.5", NOW)],     # missing but inside the grace
    )
    missing, census = diff = rec.diff_missing(
        db=lab, stream_db=stream,
        categories=["Climate and Weather", "Crypto"], now=NOW,
    )
    assert census["missing_total"] == 2
    assert {m.market_id for m in missing} == {
        "KXHIGHNY-26JUL10-B82.5", "KXBTCD-26JUL10-T50"}
    assert census["missing_by_category"] == {"Climate and Weather": 1, "Crypto": 1}
    assert diff[1]["archived_markets"] == 1


def test_diff_census_counts_the_whole_deficit_even_when_the_scan_is_capped(tmp_path):
    """A pass that reports only what it looked at is how a 12% hole hides."""
    lab = _make_lab(tmp_path / "lab.duckdb", archived=[],
                    series={"KXBTCD": "Crypto"})
    stream = _make_stream(
        tmp_path / "stream.duckdb",
        [(f"KXBTCD-26JUL10-T{i}", NOW - timedelta(days=10 + i)) for i in range(20)],
    )
    missing, census = rec.diff_missing(
        db=lab, stream_db=stream, categories=["Crypto"], now=NOW, scan_limit=5)
    assert census["missing_total"] == 20        # exact
    assert census["missing_scanned"] == 5 == len(missing)
    # and the 5 it kept are the 5 OLDEST, not an arbitrary 5
    assert [m.market_id for m in missing] == [f"KXBTCD-26JUL10-T{i}" for i in (19, 18, 17, 16, 15)]


def test_exclude_series_pattern(tmp_path):
    lab = _make_lab(tmp_path / "lab.duckdb", archived=[],
                    series={"KXMVECROSS": "Exotics", "KXBTCD": "Crypto"})
    stream = _make_stream(
        tmp_path / "stream.duckdb",
        [("KXMVECROSS-a-1", NOW - timedelta(days=5)),
         ("KXBTCD-26JUL10-T50", NOW - timedelta(days=5))],
    )
    _, census = rec.diff_missing(db=lab, stream_db=stream, now=NOW,
                                categories=["Exotics", "Crypto"],
                                exclude_series_like="KXMVE%")
    assert census["missing_total"] == 1


# ---------------------------------------------------------------------------
# 2. time-to-purge ordering
# ---------------------------------------------------------------------------


def _m(mid: str, days_ago: float) -> rec.Missing:
    return rec.Missing(mid, rec.series_of(mid), "Crypto", NOW - timedelta(days=days_ago))


def test_work_order_is_most_endangered_first_not_alphabetical():
    """The load-bearing property. Alphabetical order is what starved the
    sweep; here the ALPHABETICALLY-FIRST item is the SAFEST one, so a
    bug-preserving implementation fails on the order, not on a missing key."""
    items = [_m("AAA-1", 1), _m("MMM-1", 65), _m("ZZZ-1", 30)]
    order, deferred = rec.work_order(items, now=NOW, max_per_series=None)
    assert [m.market_id for m in order] == ["MMM-1", "ZZZ-1", "AAA-1"]
    assert deferred == []
    # the ordering key really is time-to-purge, and it is decreasing life
    lives = [rec.time_to_purge_days(m.last_trade_ts, NOW) for m in order]
    assert lives == sorted(lives)
    assert lives[0] == pytest.approx(rec.PURGE_HORIZON_DAYS - 65)


def test_time_to_purge_uses_last_trade_as_a_conservative_close_estimate():
    """last trade <= close, so the estimate may only OVER-state endangerment."""
    assert rec.time_to_purge_days(NOW - timedelta(days=70), NOW) == pytest.approx(0.0)
    assert rec.time_to_purge_days(NOW - timedelta(days=71), NOW) < 0
    assert rec.time_to_purge_days(NOW, NOW) == pytest.approx(rec.PURGE_HORIZON_DAYS)


def test_ordering_is_deterministic_across_runs():
    same_age = [_m("BBB-2", 10), _m("AAA-9", 10), _m("AAA-1", 10)]
    first = [m.market_id for m in rec.work_order(same_age, now=NOW, max_per_series=None)[0]]
    second = [m.market_id for m in rec.work_order(list(reversed(same_age)), now=NOW,
                                                  max_per_series=None)[0]]
    assert first == second == ["AAA-1", "AAA-9", "BBB-2"]


# ---------------------------------------------------------------------------
# 3. per-ITEM budget (the starvation lesson)
# ---------------------------------------------------------------------------


def test_per_series_cap_stops_one_family_eating_the_whole_run():
    """6M same-age parlay legs must not crowd out a 3-market weather series."""
    huge = [_m(f"KXMVE-{i}", 20) for i in range(10)]
    small = [rec.Missing(f"KXLOWTNYC-{i}", "KXLOWTNYC", "Climate and Weather",
                         NOW - timedelta(days=19)) for i in range(3)]
    order, deferred = rec.work_order(huge + small, now=NOW, max_per_series=2)
    assert [m.series for m in order] == ["KXMVE", "KXMVE", "KXLOWTNYC", "KXLOWTNYC"]
    assert len(deferred) == 9
    # deferred work is NOT a hole and NOT lost — it is simply unreached
    assert all(m.market_id.startswith(("KXMVE", "KXLOWTNYC")) for m in deferred)


def test_deferred_work_keeps_the_run_incomplete(tmp_path, monkeypatch):
    lab = _make_lab(tmp_path / "lab.duckdb", archived=[],
                    series={"KXBTCD": "Crypto"})
    stream = _make_stream(
        tmp_path / "stream.duckdb",
        [(f"KXBTCD-26JUL10-T{i}", NOW - timedelta(days=10)) for i in range(4)],
    )
    monkeypatch.setattr(rec, "LOCKFILE_UNUSED", None, raising=False)
    sess = _Session(alive=set())
    monkeypatch.setattr(rec.requests, "Session", lambda: sess)
    monkeypatch.setattr(rec, "LEDGER_STATE", str(tmp_path / "ledger.json"))
    monkeypatch.setattr("collector.sweep.LOCK_FILE", str(tmp_path / "writer.lock"))
    code = rec.main([
        "--db", lab, "--stream-db", stream, "--categories", "Crypto",
        "--max-per-series", "2", "--metadata-only",
        "--ledger-state", str(tmp_path / "ledger.json"),
        "--summary", str(tmp_path / "summary.json"),
    ])
    assert code == 2, "unreached work must never exit 0"
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["complete"] is False
    assert summary["deferred_by_series_cap"] == 2


# ---------------------------------------------------------------------------
# 4. repair, idempotency, pacing, writer-burst
# ---------------------------------------------------------------------------


def _repair_order(n: int = 3, days_ago: float = 10.0) -> list[rec.Missing]:
    return [rec.Missing(f"KXHIGHNY-26JUL10-B8{i}.5", "KXHIGHNY",
                        "Climate and Weather", NOW - timedelta(days=days_ago))
            for i in range(n)]


def test_repair_writes_markets_and_reports_a_measured_request_rate(tmp_path, monkeypatch):
    db = _make_lab(tmp_path / "lab.duckdb", archived=[], series={"KXHIGHNY": "Climate and Weather"})
    lock = str(tmp_path / "writer.lock")
    order = _repair_order()
    sess = _Session(alive={m.market_id for m in order}, lock_file=lock)
    monkeypatch.setattr("collector.sweep.LOCK_FILE", lock)
    out = rec.reconcile(order, db=db, session=sess, lock_file=lock,
                        ledger_state=str(tmp_path / "ledger.json"),
                        pause_s=0.0, candles_pause_s=0.0, now=NOW)
    assert out["repaired"] == 3 and out["purged"] == 0
    assert out["complete"] is True and out["remaining"] == 0
    assert out["requests"] == 1 + 2 * 3  # 1 metadata batch + candles+trades each
    assert out["request_rate_hz"] > 0
    assert sess.lock_held_during_http is False, "HTTP must never run under writer.lock"
    con = duckdb.connect(db, read_only=True)
    assert con.execute("SELECT count(*) FROM markets").fetchone()[0] == 3
    con.close()


def test_second_run_finds_nothing_new(tmp_path, monkeypatch):
    """Idempotency, end to end: repair, then re-diff the same archives."""
    lab = _make_lab(tmp_path / "lab.duckdb", archived=[],
                    series={"KXHIGHNY": "Climate and Weather"})
    order = _repair_order()
    stream = _make_stream(tmp_path / "stream.duckdb",
                          [(m.market_id, m.last_trade_ts) for m in order])
    lock = str(tmp_path / "writer.lock")
    monkeypatch.setattr("collector.sweep.LOCK_FILE", lock)
    sess = _Session(alive={m.market_id for m in order})

    missing, census = rec.diff_missing(db=lab, stream_db=stream, now=NOW,
                                       categories=["Climate and Weather"])
    assert census["missing_total"] == 3
    first = rec.reconcile(rec.work_order(missing, now=NOW)[0], db=lab, session=sess,
                          lock_file=lock, ledger_state=str(tmp_path / "l.json"),
                          pause_s=0.0, candles_pause_s=0.0, now=NOW)
    assert first["repaired"] == 3

    missing2, census2 = rec.diff_missing(db=lab, stream_db=stream, now=NOW,
                                         categories=["Climate and Weather"])
    assert census2["missing_total"] == 0 and missing2 == []
    second = rec.reconcile([], db=lab, session=sess, lock_file=lock,
                           ledger_state=str(tmp_path / "l.json"),
                           pause_s=0.0, candles_pause_s=0.0, now=NOW)
    assert (second["repaired"], second["purged"], second["complete"]) == (0, 0, True)


def test_requests_are_paced_between_batches(tmp_path, monkeypatch):
    db = _make_lab(tmp_path / "lab.duckdb", archived=[], series={"KXHIGHNY": "Climate and Weather"})
    lock = str(tmp_path / "writer.lock")
    monkeypatch.setattr("collector.sweep.LOCK_FILE", lock)
    order = _repair_order(4)
    sess = _Session(alive={m.market_id for m in order})
    slept: list[float] = []
    monkeypatch.setattr(rec.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(kalshi, "batch_tickers",
                        lambda t, *a, **k: [list(t[i:i + 2]) for i in range(0, len(t), 2)])
    rec.reconcile(order, db=db, session=sess, lock_file=lock,
                  ledger_state=str(tmp_path / "l.json"),
                  pause_s=0.25, candles_pause_s=0.4, now=NOW)
    assert 0.25 in slept, "batches must be paced apart"
    assert slept.count(0.4) == 4, "one candle-pause per repaired market"


def test_reconcile_never_advances_a_watermark_or_writes_sweep_log(tmp_path, monkeypatch):
    """A watermark is a coverage claim; this pass repairs holes BEHIND them."""
    db = _make_lab(tmp_path / "lab.duckdb", archived=[], series={"KXHIGHNY": "Climate and Weather"})
    lock = str(tmp_path / "writer.lock")
    monkeypatch.setattr("collector.sweep.LOCK_FILE", lock)
    order = _repair_order()
    rec.reconcile(order, db=db, session=_Session(alive={m.market_id for m in order}),
                  lock_file=lock, ledger_state=str(tmp_path / "l.json"),
                  pause_s=0.0, candles_pause_s=0.0, now=NOW)
    con = duckdb.connect(db, read_only=True)
    assert con.execute("SELECT count(*) FROM watermarks").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM sweep_log").fetchone()[0] == 0
    con.close()


# ---------------------------------------------------------------------------
# 5. the non-negotiable one: an unrepairable hole is RECORDED, not skipped
# ---------------------------------------------------------------------------


def test_ledger_findings_shape_and_growth_semantics():
    purged = [rec.Missing("KXBTCD-26MAY01-T1", "KXBTCD", "Crypto",
                          datetime(2026, 5, 1, 12, tzinfo=UTC)),
              rec.Missing("KXBTCD-26MAY01-T2", "KXBTCD", "Crypto",
                          datetime(2026, 5, 1, 18, tzinfo=UTC)),
              rec.Missing("KXHIGHNY-26MAY02-B1", "KXHIGHNY", "Climate and Weather",
                          datetime(2026, 5, 2, 4, tzinfo=UTC))]
    f = rec.ledger_findings(purged)
    assert set(f) == {"purged_market:KXBTCD@2026-05-01",
                      "purged_market:KXHIGHNY@2026-05-02"}
    assert f["purged_market:KXBTCD@2026-05-01"]["magnitude"] == 2.0
    assert f["purged_market:KXBTCD@2026-05-01"]["unit"] == "markets"
    assert "KXBTCD-26MAY01-T1" in f["purged_market:KXBTCD@2026-05-01"]["detail"]


@pytest.mark.skipif(not LEDGER_TOOL.exists(), reason="hylshi ledger tool not present")
def test_an_unrepairable_hole_is_recorded_in_the_real_ledger(tmp_path, monkeypatch):
    """NON-NEGOTIABLE: what cannot be repaired must be written down as KNOWN.

    Asserted against hylshi's OWN `completeness_ledger` module — the file is
    written by the code that reads it, so a schema drift fails here rather
    than in production. The assertion is on the ledger's CONTENT (the key,
    its magnitude and that the reader classifies it), not merely on a file
    existing.
    """
    db = _make_lab(tmp_path / "lab.duckdb", archived=[], series={"KXBTCD": "Crypto"})
    lock = str(tmp_path / "writer.lock")
    monkeypatch.setattr("collector.sweep.LOCK_FILE", lock)
    state = tmp_path / "completeness_ledger.json"
    gone = [rec.Missing("KXBTCD-26MAY01-T1", "KXBTCD", "Crypto",
                        datetime(2026, 5, 1, 12, tzinfo=UTC)),
            rec.Missing("KXBTCD-26MAY01-T2", "KXBTCD", "Crypto",
                        datetime(2026, 5, 1, 13, tzinfo=UTC))]
    alive = _repair_order(1)
    out = rec.reconcile(gone + alive, db=db,
                        session=_Session(alive={m.market_id for m in alive}),
                        lock_file=lock, ledger_state=str(state),
                        pause_s=0.0, candles_pause_s=0.0, now=NOW)

    assert out["purged"] == 2 and out["repaired"] == 1
    assert out["ledger_recorded"] == 1
    assert out["purged_keys"] == ["purged_market:KXBTCD@2026-05-01"]

    payload = json.loads(state.read_text())
    hole = payload["holes"]["purged_market:KXBTCD@2026-05-01"]
    assert hole["magnitude"] == 2.0 and hole["kind"] == "purged_market"
    assert hole["first_seen"] and hole["acknowledged_at"]

    # the ledger's own reader must accept it, and treat a re-record of the
    # SAME extent as known-quiet while a GROWN one escalates
    led = rec._load_ledger_tool()
    st = led.load_state(str(state))
    same = rec.ledger_findings(gone)
    assert led.classify(same, st)["escalating"] == []
    grown = rec.ledger_findings(gone + [rec.Missing(
        "KXBTCD-26MAY01-T3", "KXBTCD", "Crypto",
        datetime(2026, 5, 1, 14, tzinfo=UTC))])
    assert [e["key"] for e in led.classify(grown, st)["escalating"]] == [
        "purged_market:KXBTCD@2026-05-01"]


def test_a_clean_run_writes_no_ledger_entry(tmp_path, monkeypatch):
    """Discrimination control: the recorder must not be always-on."""
    db = _make_lab(tmp_path / "lab.duckdb", archived=[], series={"KXHIGHNY": "Climate and Weather"})
    lock = str(tmp_path / "writer.lock")
    monkeypatch.setattr("collector.sweep.LOCK_FILE", lock)
    state = tmp_path / "ledger.json"
    order = _repair_order()
    out = rec.reconcile(order, db=db, session=_Session(alive={m.market_id for m in order}),
                        lock_file=lock, ledger_state=str(state),
                        pause_s=0.0, candles_pause_s=0.0, now=NOW)
    assert out["purged"] == 0 and out["ledger_recorded"] == 0
    assert not state.exists(), "a clean run must not touch the trading repo's state"


def test_undetermined_is_never_recorded_as_a_permanent_hole(tmp_path, monkeypatch):
    """A batch whose cursor stayed live is UNKNOWN, not lost. Recording it
    would be EXP-931's silent-permanent-loss defect with the sign flipped."""
    db = _make_lab(tmp_path / "lab.duckdb", archived=[], series={"KXHIGHNY": "Climate and Weather"})
    lock = str(tmp_path / "writer.lock")
    monkeypatch.setattr("collector.sweep.LOCK_FILE", lock)
    state = tmp_path / "ledger.json"
    order = _repair_order(2)
    monkeypatch.setattr(
        kalshi, "get_markets_by_tickers",
        lambda batch, **kw: ({}, [], list(batch)),
    )
    out = rec.reconcile(order, db=db, session=_Session(alive=set()), lock_file=lock,
                        ledger_state=str(state), pause_s=0.0, candles_pause_s=0.0, now=NOW)
    assert out["undetermined"] == 2 and out["purged"] == 0
    assert out["complete"] is False
    assert not state.exists()


def test_budget_stop_leaves_a_reported_remainder(tmp_path, monkeypatch):
    db = _make_lab(tmp_path / "lab.duckdb", archived=[], series={"KXHIGHNY": "Climate and Weather"})
    lock = str(tmp_path / "writer.lock")
    monkeypatch.setattr("collector.sweep.LOCK_FILE", lock)
    order = _repair_order(4)
    monkeypatch.setattr(kalshi, "batch_tickers", lambda t, *a, **k: [[x] for x in t])
    out = rec.reconcile(order, db=db, session=_Session(alive={m.market_id for m in order}),
                        lock_file=lock, ledger_state=str(tmp_path / "l.json"),
                        max_markets=2, pause_s=0.0, candles_pause_s=0.0, now=NOW)
    assert out["processed"] == 2 and out["remaining"] == 2
    assert out["complete"] is False and out["stop_reason"] == "max-markets"


def test_max_markets_tail_is_unreached_work_not_a_finished_run(tmp_path, monkeypatch):
    """The order's TAIL, cut off by --max-markets, is unreached work. Without
    counting it a 5-market smoke over a fully-scanned, uncapped deficit would
    repair five markets and report `complete`."""
    lab = _make_lab(tmp_path / "lab.duckdb", archived=[], series={"KXBTCD": "Crypto"})
    ids = [f"KXBTCD-26JUL10-T{i}" for i in range(4)]
    stream = _make_stream(tmp_path / "stream.duckdb",
                          [(m, NOW - timedelta(days=10)) for m in ids])
    sess = _Session(alive=set(ids))
    monkeypatch.setattr(rec.requests, "Session", lambda: sess)
    monkeypatch.setattr("collector.sweep.LOCK_FILE", str(tmp_path / "writer.lock"))
    code = rec.main([
        "--db", lab, "--stream-db", stream, "--categories", "Crypto",
        "--max-markets", "1", "--max-per-series", "0", "--metadata-only",
        "--ledger-state", str(tmp_path / "ledger.json"),
        "--summary", str(tmp_path / "summary.json"),
    ])
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["repaired"] == 1 and summary["deferred_by_series_cap"] == 0
    assert summary["dropped_by_max_markets"] == 3
    assert summary["complete"] is False and code == 2


# ---------------------------------------------------------------------------
# 6. the batching primitive
# ---------------------------------------------------------------------------


def test_batch_tickers_bounds_url_length_not_ticker_count():
    """MEASURED: 250 tickers = 5,869 chars is served; 500 = 12,247 -> HTTP 414.
    Exotics tickers are ~2.4x longer than weather ones, so a count-based
    batcher would 414 on exactly the families with the most to repair."""
    short = [f"KX{i:03d}" for i in range(100)]           # 5 chars each
    long = [f"KXMVESPORTSMULTIGAMEEXTENDED-26JUL{i:02d}-ABCDEFGH" for i in range(100)]
    assert len(kalshi.batch_tickers(short, max_chars=60)) == 10
    assert all(len(",".join(b)) <= 60 for b in kalshi.batch_tickers(long, max_chars=60))
    # order is preserved: batches concatenate back to the priority order
    flat = [t for b in kalshi.batch_tickers(long, max_chars=200) for t in b]
    assert flat == long


def test_batch_resolution_splits_found_from_absent():
    class _S:
        def get(self, url, params=None, timeout=None):
            want = params["tickers"].split(",")
            return _FakeResp({"markets": [_mkt(t) for t in want if t.endswith("OK")],
                              "cursor": ""})

    found, absent, undet = kalshi.get_markets_by_tickers(["A-OK", "B-GONE", "C-OK"], session=_S())
    assert sorted(found) == ["A-OK", "C-OK"]
    assert absent == ["B-GONE"] and undet == []


def test_a_live_cursor_makes_unseen_tickers_undetermined_not_absent():
    class _S:
        def get(self, url, params=None, timeout=None):
            return _FakeResp({"markets": [_mkt("A-OK")], "cursor": "more"})

    found, absent, undet = kalshi.get_markets_by_tickers(
        ["A-OK", "B-?"], session=_S(), max_pages=2)
    assert sorted(found) == ["A-OK"]
    assert absent == [] and undet == ["B-?"]
