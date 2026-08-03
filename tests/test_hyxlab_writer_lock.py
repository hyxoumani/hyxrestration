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
import threading
import time
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

    def get_markets_ascending(*args, **kwargs):
        session.observe()
        return markets, False

    def get_candlesticks(*args, **kwargs):
        session.observe()
        return []

    def get_trades(ticker, **kwargs):
        session.observe()
        return [], False

    monkeypatch.setattr(sweep.kalshi, "get_markets", get_markets)
    monkeypatch.setattr(sweep.kalshi, "get_markets_ascending", get_markets_ascending)
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

    n_markets, _, truncated = sweep.sweep_series(db, "KXA", days=60, session=session)
    assert truncated is False

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

    assert sweep.sweep_series(db, "KXA", days=60, session=session) == (0, 0, False)

    from hyxlab.store import Store

    store = Store(db)
    try:
        assert store.conn.execute("SELECT count(*) FROM sweep_log").fetchone()[0] == 1
    finally:
        store.close()


def test_burst_open_budget_outlasts_a_long_reader(tmp_path, monkeypatch):
    """Releasing between series lets a READER in, and DuckDB excludes a
    writer while one is attached. The burst must wait out a multi-minute
    report rather than dying at its next flush — a cost the old whole-run
    hold did not have."""
    assert sweep.BURST_OPEN_RETRIES * sweep.BURST_OPEN_DELAY_S >= 300

    seen = {}

    def fake_open_retry(db, *, read_only=False, retries=30, delay=2.0):
        seen["retries"], seen["delay"] = retries, delay
        from hyxlab.store import Store

        return Store(db)

    monkeypatch.setattr(sweep, "open_retry", fake_open_retry)
    with sweep.writer_burst(str(tmp_path / "t.duckdb"), lock_file=str(tmp_path / "w.lock")):
        pass
    assert seen["retries"] * seen["delay"] >= 300, (
        "writer_burst fell back to open_retry's 60s default; a long reader "
        "would kill the sweep at its next flush"
    )


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


def test_collector_waits_out_a_writer_instead_of_dropping_the_cycle(tmp_path):
    """THE load-bearing collector test: the lock is held when the cycle
    starts and released while it waits, so ONLY an implementation that
    actually waits captures this cycle.

    Asserting `None` on a held lock and success on a free one is NOT
    enough — `flock -n` satisfies both, which is how the first version of
    this test passed against the very behaviour it was written to kill
    (caught by mutation, 2026-08-02).
    """
    lock_path = str(tmp_path / "writer.lock")
    released = threading.Event()

    def writer():
        with open(lock_path, "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            time.sleep(0.4)
            fcntl.flock(fh, fcntl.LOCK_UN)
            released.set()

    t = threading.Thread(target=writer)
    t.start()
    time.sleep(0.1)  # let the writer take it first
    try:
        got = collect.acquire_writer_lock(lock_path, wait_s=10.0)
        assert got is not None, "cycle was dropped instead of waiting for the writer"
        assert released.is_set(), "acquired before the writer released — fixture is wrong"
        got.close()
    finally:
        t.join()


def test_collector_gives_up_after_its_budget(tmp_path):
    """The wait is bounded: an unbounded one would still be running when
    the next 5-min firing arrives."""
    lock_path = str(tmp_path / "writer.lock")
    holder = open(lock_path, "a")  # noqa: SIM115 — must stay held across the wait
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        t0 = time.monotonic()
        assert collect.acquire_writer_lock(lock_path, wait_s=0.3) is None
        assert time.monotonic() - t0 >= 0.3, "returned before spending its budget"
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()


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
    """No file AND no journalled skip: no FAILURE — but see the EXP-943 tests
    below, it is not a PASS either."""
    qa.qa_collect_skips(path=str(tmp_path / "absent.jsonl"), journal_skips=0)
    assert not qa._failures


def test_qa_fails_on_the_measured_starvation_rate(tmp_path):
    """12-38 skips/day was the observed rate; it must trip the check."""
    path = str(tmp_path / "skips.jsonl")
    _write_skips(path, 12)
    qa.qa_collect_skips(path=path, journal_skips=12)
    assert qa._failures == ["collector cycles are not skipped for the lock"]


def test_qa_tolerates_occasional_contention(tmp_path):
    path = str(tmp_path / "skips.jsonl")
    _write_skips(path, qa.COLLECT_SKIP_MAX_24H)
    qa.qa_collect_skips(path=path, journal_skips=qa.COLLECT_SKIP_MAX_24H)
    assert not qa._failures


def test_qa_skip_check_is_windowed(tmp_path):
    """Old skips must age out, or the check stays red forever after one
    incident and gets ignored — the failure mode it exists to prevent."""
    path = str(tmp_path / "skips.jsonl")
    _write_skips(path, 50, age_h=48.0)
    qa.qa_collect_skips(path=path, journal_skips=0)
    assert not qa._failures


def test_qa_skip_check_survives_a_malformed_row(tmp_path):
    path = str(tmp_path / "skips.jsonl")
    Path(path).write_text("not json\n")
    _write_skips(path, 12)
    qa.qa_collect_skips(path=path, journal_skips=12)
    assert qa._failures, "a truncated row must not silence the count"


# --------------------------------------------------------------------------
# 3b. EXP-943 — DETECTOR LIVENESS. The check above is correct and, for 14
# days, could not physically fire: its sidecar had never been created, and
# "absent" rendered exactly like "clean". These assert that the two are now
# told apart, using systemd's independent record of exit-75 cycles.
# --------------------------------------------------------------------------


def test_absent_sidecar_with_journalled_skips_is_a_dead_producer(tmp_path):
    """The 2026-07-20..08-02 shape: flock killed the process before python ran."""
    qa.qa_collect_skips(path=str(tmp_path / "absent.jsonl"), journal_skips=8)
    assert qa._failures == ["collector cycles are not skipped for the lock"]


def test_stale_sidecar_with_journalled_skips_is_also_a_dead_producer(tmp_path):
    """"Present and clean" must not read OK either."""
    path = str(tmp_path / "skips.jsonl")
    _write_skips(path, 5, age_h=200.0)  # rows, but all outside the window
    qa.qa_collect_skips(path=path, journal_skips=4)
    assert qa._failures


def test_absent_sidecar_with_no_journalled_skips_is_unverified_not_a_pass(tmp_path):
    """2026-08-03's state: same bytes on disk, opposite meaning."""
    before = qa._passes
    qa.qa_collect_skips(path=str(tmp_path / "absent.jsonl"), journal_skips=0)
    assert not qa._failures
    assert qa._skipped == ["collect-skips"], "must not count as a pass"
    assert qa._passes == before


def test_unreadable_journal_leaves_the_sidecar_unverified(tmp_path):
    """None is not zero: an unreadable journal cannot testify to anything."""
    qa.qa_collect_skips(path=str(tmp_path / "absent.jsonl"), journal_skips=None)
    assert not qa._failures and qa._skipped == ["collect-skips"]


def test_a_produced_sidecar_within_budget_is_a_real_pass(tmp_path):
    path = str(tmp_path / "skips.jsonl")
    _write_skips(path, 2)
    before = qa._passes
    qa.qa_collect_skips(path=path, journal_skips=2)
    assert not qa._failures and not qa._skipped
    assert qa._passes == before + 1


def test_journal_witness_query_is_read_only(monkeypatch):
    """The witness must never start, stop or reload anything."""
    seen = {}

    class _P:
        returncode, stdout = 0, "status=75\nstatus=75\nstatus=1\n"

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return _P()

    monkeypatch.setattr(qa.subprocess, "run", fake_run)
    assert qa.journal_skip_exits() == 2
    assert seen["cmd"][0] == "journalctl"
    assert not ({"restart", "start", "stop", "daemon-reload"} & set(seen["cmd"]))


def test_journal_witness_returns_none_when_journalctl_is_unusable(monkeypatch):
    monkeypatch.setattr(qa.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    assert qa.journal_skip_exits() is None


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


def test_a_truncated_sweep_is_logged_non_ok(tmp_path, monkeypatch):
    """EXP-931: truncation used to be a print and a sweep_log status of 'ok'.
    That is how "our crypto archive is BNB-only" reached an experiment's
    premises as a finding rather than a defect. It must be queryable."""
    lock_path = str(tmp_path / "writer.lock")
    db = str(tmp_path / "t.duckdb")
    monkeypatch.setattr(sweep, "LOCK_FILE", lock_path)
    markets = [_market("KXA-1", 10), _market("KXA-2", 11)]
    session = _FakeSession(lock_path, markets)
    _patch_kalshi(monkeypatch, session, markets)
    monkeypatch.setattr(
        sweep.kalshi, "get_markets_ascending", lambda *a, **k: (markets, True)
    )

    n_markets, _, truncated = sweep.sweep_series(db, "KXA", days=60, session=session)
    assert (n_markets, truncated) == (2, True)

    from hyxlab.store import Store

    store = Store(db)
    try:
        status, note = store.conn.execute(
            "SELECT status, note FROM sweep_log WHERE series = 'KXA'"
        ).fetchone()
        assert status == "truncated", "a truncated sweep must not read as ok"
        assert "resume" in note
        # ...and the data it DID capture is still persisted: the point is to
        # mark the hole, not to throw away a partial capture.
        assert store.conn.execute("SELECT count(*) FROM markets").fetchone()[0] == 2
    finally:
        store.close()


def test_per_series_budget_is_bounded_and_wired_by_default():
    """A series with no budget can hold the whole run (three BNB series ate
    a 3.5h sweep and starved every KXBTC*/KXETH*/KXSOL* series behind them)."""
    import inspect

    assert 0 < sweep.MAX_MARKETS_PER_SERIES <= 10_000
    sig = inspect.signature(sweep.sweep_series)
    assert sig.parameters["max_markets"].default == sweep.MAX_MARKETS_PER_SERIES
