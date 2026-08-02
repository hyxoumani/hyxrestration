"""Writer-lock discipline across archive writers (H1, deep review 2026-07-11).

`data/writer.lock` is the archive's single-writer gate. Three writers
(poly_sweep, trades_backfill, signals) already touched the DB only in
short open->write->close bursts; `collector.sweep` instead ran under a
unit-level `flock` for its ENTIRE multi-hour run, and `hyxlab-collect`
ran under `flock -n`. The combination dropped 421 of 3,706 five-minute
capture cycles in the 14 days to 2026-08-02 (11.4%), clustered in the
daily 06:10 sweep window — and dropped them before python started, so
nothing in the archive recorded the hole.

The load-bearing tests here assert the MECHANISM (the lock is actually
released between series; a contended cycle waits rather than vanishing;
a timed-out cycle leaves a durable record), because the harm is invisible
to any check that only reads the resulting data.
"""

import fcntl
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import collector.collect as collect
import collector.qa as qa
import collector.sweep as sweep

UNIT_DIR = Path(__file__).resolve().parent.parent / "scripts" / "systemd"


# --------------------------------------------------------------------------
# 1. The sweep must not hold the writer lock across its REST calls.
# --------------------------------------------------------------------------


class _FakeSession:
    """Records whether the writer lock was held at each HTTP call."""

    def __init__(self, lock_path, markets):
        self.lock_path = lock_path
        self.markets = markets
        self.held_during_http = []

    def observe(self):
        # A second process can take the lock iff the sweep has released it.
        with open(self.lock_path, "a") as probe:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(probe, fcntl.LOCK_UN)
                self.held_during_http.append(False)
            except OSError:
                self.held_during_http.append(True)


def _patch_kalshi(monkeypatch, session, markets):
    monkeypatch.setattr(sweep.kalshi, "get_series_list", lambda s: [])

    def get_markets(**kwargs):
        session.observe()
        return markets

    def get_candlesticks(*args, **kwargs):
        session.observe()
        return []

    def get_trades(ticker, **kwargs):
        session.observe()
        return [], False

    monkeypatch.setattr(sweep.kalshi, "get_markets", get_markets)
    monkeypatch.setattr(sweep.kalshi, "get_candlesticks", get_candlesticks)
    monkeypatch.setattr(sweep.kalshi, "get_trades", get_trades)
    monkeypatch.setattr(sweep.kalshi, "to_market_info", lambda m: _info(m))
    monkeypatch.setattr(sweep.time, "sleep", lambda s: None)


def _info(m):
    from hyxlab.models import MarketInfo

    return MarketInfo(
        market_id=m["ticker"],
        venue="kalshi",
        series=m["ticker"].split("-")[0],
        title=m["ticker"],
        close_time=datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")),
    )


def _market(ticker, day):
    return {
        "ticker": ticker,
        "open_time": f"2026-07-{day:02d}T00:00:00Z",
        "close_time": f"2026-07-{day:02d}T12:00:00Z",
    }


def test_sweep_releases_the_writer_lock_across_every_rest_call(tmp_path, monkeypatch):
    """THE load-bearing test: with the old unit-level flock the lock was
    held for the whole run, so a concurrent collector cycle could never
    acquire it. Every HTTP call must happen with the lock free."""
    lock_path = str(tmp_path / "writer.lock")
    db = str(tmp_path / "t.duckdb")
    monkeypatch.setattr(sweep, "LOCK_FILE", lock_path)
    markets = [_market("KXA-1", 10), _market("KXA-2", 11)]
    session = _FakeSession(lock_path, markets)
    _patch_kalshi(monkeypatch, session, markets)

    sweep.sweep_series(db, "KXA", days=60, session=session)

    assert session.held_during_http, "fixture made no HTTP calls"
    assert not any(session.held_during_http), (
        "the writer lock was held across a REST call — that is exactly the "
        "multi-hour hold that starved hyxlab-collect"
    )


def test_sweep_still_persists_everything_it_buffered(tmp_path, monkeypatch):
    """Discrimination control: releasing the lock must not cost writes. A
    'fix' that simply stopped writing would pass the test above."""
    lock_path = str(tmp_path / "writer.lock")
    db = str(tmp_path / "t.duckdb")
    monkeypatch.setattr(sweep, "LOCK_FILE", lock_path)
    markets = [_market("KXA-1", 10), _market("KXA-2", 11)]
    session = _FakeSession(lock_path, markets)
    _patch_kalshi(monkeypatch, session, markets)

    n_markets, _ = sweep.sweep_series(db, "KXA", days=60, session=session)

    assert n_markets == 2
    from hyxlab.store import Store

    store = Store(db)
    try:
        ids = {r[0] for r in store.conn.execute("SELECT market_id FROM markets").fetchall()}
        assert ids == {"KXA-1", "KXA-2"}
        # The watermark advances in the same burst as the data, so a crash
        # can never leave data attributed to a series that will be re-swept.
        assert store.watermark("KXA") is not None
        assert store.conn.execute("SELECT count(*) FROM sweep_log").fetchone()[0] == 1
    finally:
        store.close()


def test_sweep_series_with_no_markets_still_logs_under_a_burst(tmp_path, monkeypatch):
    lock_path = str(tmp_path / "writer.lock")
    db = str(tmp_path / "t.duckdb")
    monkeypatch.setattr(sweep, "LOCK_FILE", lock_path)
    session = _FakeSession(lock_path, [])
    _patch_kalshi(monkeypatch, session, [])

    assert sweep.sweep_series(db, "KXA", days=60, session=session) == (0, 0)

    from hyxlab.store import Store

    store = Store(db)
    try:
        assert store.conn.execute("SELECT count(*) FROM sweep_log").fetchone()[0] == 1
    finally:
        store.close()


def test_writer_burst_releases_the_lock_on_exception(tmp_path):
    """A burst that raises must not leave the lock held — otherwise one bad
    series wedges the collector for the rest of the sweep."""
    lock_path = str(tmp_path / "writer.lock")
    db = str(tmp_path / "t.duckdb")
    with pytest.raises(RuntimeError), sweep.writer_burst(db, lock_file=lock_path):
        raise RuntimeError("boom")

    with open(lock_path, "a") as probe:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)  # must not raise
        fcntl.flock(probe, fcntl.LOCK_UN)


# --------------------------------------------------------------------------
# 2. A contended collector cycle waits; a timed-out one leaves a record.
# --------------------------------------------------------------------------


def test_collector_waits_for_a_held_lock_instead_of_dropping_the_cycle(tmp_path):
    lock_path = str(tmp_path / "writer.lock")
    holder = open(lock_path, "a")  # noqa: SIM115 — must stay held across the wait
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        # Bounded wait, then give up — the old `flock -n` gave up instantly.
        assert collect.acquire_writer_lock(lock_path, wait_s=0.2) is None
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()
    got = collect.acquire_writer_lock(lock_path, wait_s=0.2)
    assert got is not None, "lock must be acquirable once the writer releases"
    got.close()


def test_collector_lock_wait_is_bounded_below_the_timer_period():
    """A wait >= the 300s timer period would let cycles stack: the next
    firing would start while this one still waits."""
    assert 0 < collect.LOCK_WAIT_S < 300


def test_a_skipped_cycle_leaves_a_durable_record(tmp_path):
    """The 14-day outage was invisible because `flock -n` failed before
    python started. A skip must now be counted somewhere."""
    path = str(tmp_path / "skips.jsonl")
    collect.record_skip("writer lock held", 240.0, path=path)
    collect.record_skip("writer lock held", 241.5, path=path)

    lines = Path(path).read_text().splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["reason"] == "writer lock held"
    assert rec["waited_s"] == 240.0
    datetime.fromisoformat(rec["at"])  # parseable, tz-aware


# --------------------------------------------------------------------------
# 3. QA reads the record and fails on a pattern.
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_qa():
    qa._failures.clear()
    qa._skipped.clear()
    yield
    qa._failures.clear()
    qa._skipped.clear()


def _write_skips(path, n, age_h=1.0):
    now = datetime.now(UTC)
    with open(path, "a") as fh:
        for _ in range(n):
            at = (now - timedelta(hours=age_h)).isoformat()
            fh.write(json.dumps({"at": at, "reason": "writer lock held"}) + "\n")


def test_qa_passes_when_no_skip_journal_exists(tmp_path):
    qa.qa_collect_skips(path=str(tmp_path / "absent.jsonl"))
    assert not qa._failures


def test_qa_fails_on_the_measured_starvation_rate(tmp_path):
    """12-38 skips/day was the observed rate; it must trip the check."""
    path = str(tmp_path / "skips.jsonl")
    _write_skips(path, 12)
    qa.qa_collect_skips(path=path)
    assert qa._failures == ["collector cycles are not skipped for the lock"]


def test_qa_tolerates_occasional_contention(tmp_path):
    path = str(tmp_path / "skips.jsonl")
    _write_skips(path, qa.COLLECT_SKIP_MAX_24H)
    qa.qa_collect_skips(path=path)
    assert not qa._failures


def test_qa_skip_check_is_windowed(tmp_path):
    """Old skips must age out, or the check stays red forever after one
    incident and gets ignored — the failure mode it exists to prevent."""
    path = str(tmp_path / "skips.jsonl")
    _write_skips(path, 50, age_h=48.0)
    qa.qa_collect_skips(path=path)
    assert not qa._failures


def test_qa_skip_check_survives_a_malformed_row(tmp_path):
    path = str(tmp_path / "skips.jsonl")
    Path(path).write_text("not json\n")
    _write_skips(path, 12)
    qa.qa_collect_skips(path=path)
    assert qa._failures, "a truncated row must not silence the count"


# --------------------------------------------------------------------------
# 4. The units must not reintroduce a process-lifetime lock.
# --------------------------------------------------------------------------


def test_no_unit_wraps_an_archive_writer_in_flock():
    """A unit-level flock is held for the whole process lifetime. Writers
    take data/writer.lock in python, per burst, so they can yield."""
    for path in UNIT_DIR.glob("*.service"):
        text = path.read_text()
        exec_lines = [ln for ln in text.splitlines() if ln.startswith("ExecStart=")]
        for ln in exec_lines:
            assert "writer.lock" not in ln, (
                f"{path.name}: ExecStart wraps the writer lock around the whole "
                "process; take it per-burst in python instead"
            )
