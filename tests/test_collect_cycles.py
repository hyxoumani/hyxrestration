"""`collect_cycles` -- the collect cycle's own per-source account, in the
archive (mistakes #133, 2026-10-23).

The hole it closes: the cycle fetches each Kalshi series, each NWS station
and the Polymarket books under its own except, prints the exception class
and exits 0, so the row tables could not tell a series with no open market
(0 rows, no error) from one whose fetch failed (0 rows, an exception). On
2026-10-22 NWS AUS pulled nothing for 21 ticks and the only witness was a
journald print. Pinned here: the fetch accounts for every source, quiet and
failed alike; the rows land in the SAME transaction as the snapshots (a
rolled-back write leaves none); the spool carries them; and QA reads the
table in preference to the journal, falling back only when the window is
empty.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import collector.collect as collect
import collector.qa as qa
from hyxlab.models import Forecast, SourceResult
from hyxlab.store import Store

NOW = datetime(2026, 10, 23, 10, 0, tzinfo=UTC)
WATCHLIST = {"kalshi_series": ["QUIET", "BROKEN"], "nws_stations": ["NYC"], "polymarket_pairs": []}


def _market(mid):
    return {
        "ticker": mid,
        "title": "t",
        "status": "open",
        "yes_bid": 40,
        "yes_ask": 42,
        "no_bid": 58,
        "no_ask": 60,
        "volume": 1,
        "open_interest": 1,
    }


@pytest.fixture
def sources(monkeypatch):
    """QUIET returns no open market, BROKEN raises, NYC pulls two days."""

    def get_markets(series_ticker, status, session=None, max_pages=None):
        if series_ticker == "BROKEN":
            raise ConnectionError("boom")
        return []

    monkeypatch.setattr(collect.kalshi, "get_markets", get_markets)
    monkeypatch.setattr(
        collect.nws,
        "get_daily_highs",
        lambda station, session=None: [
            Forecast(station, NOW, NOW.date() + timedelta(days=d), 70) for d in (0, 1)
        ],
    )


def test_fetch_accounts_for_every_source_quiet_and_failed_alike(sources):
    cyc = collect.fetch_cycle(WATCHLIST, session=object())
    by_name = {(r.source, r.name): r for r in cyc.sources}
    assert set(by_name) == {("kalshi", "QUIET"), ("kalshi", "BROKEN"), ("nws", "NYC")}
    assert (by_name["kalshi", "QUIET"].n_rows, by_name["kalshi", "QUIET"].error) == (0, None)
    assert (by_name["kalshi", "BROKEN"].n_rows, by_name["kalshi", "BROKEN"].error) == (
        0,
        "ConnectionError",
    )
    assert (by_name["nws", "NYC"].n_rows, by_name["nws", "NYC"].error) == (2, None)
    assert cyc.errors == 1
    assert all(r.wall_s >= 0.0 for r in cyc.sources)


def test_write_lands_one_row_per_source_with_the_class_on_failure(tmp_path, sources):
    cyc = collect.fetch_cycle(WATCHLIST, session=object())
    store = Store(str(tmp_path / "a.duckdb"))
    collect.write_cycle(store, cyc)
    rows = store.conn.execute(
        "SELECT source, name, n_rows, error FROM collect_cycles ORDER BY source, name"
    ).fetchall()
    assert rows == [
        ("kalshi", "BROKEN", 0, "ConnectionError"),
        ("kalshi", "QUIET", 0, None),
        ("nws", "NYC", 2, None),
    ]


def test_a_rolled_back_write_leaves_no_source_rows(tmp_path, sources, monkeypatch):
    """The WITNESS declared in tests/test_qa_staleness_coverage.py: the
    record is inside the cycle's transaction, so a cycle has its account
    exactly when it has its snapshots."""
    cyc = collect.fetch_cycle(WATCHLIST, session=object())
    store = Store(str(tmp_path / "a.duckdb"))

    def explode(rows):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "insert_forecasts", explode)
    counts = collect.write_cycle(store, cyc)
    assert counts["errors"] == cyc.errors + 1
    assert store.conn.execute("SELECT count(*) FROM collect_cycles").fetchone()[0] == 0


def test_a_failing_source_record_rolls_the_snapshots_back_too(tmp_path, sources, monkeypatch):
    """The arm a post-commit mutant cannot pass: if the record were written
    AFTER the commit, the snapshots would already be in the archive when it
    failed. Both or neither is the whole claim (red-verified 2026-10-23)."""
    cyc = collect.fetch_cycle(WATCHLIST, session=object())
    store = Store(str(tmp_path / "a.duckdb"))

    def explode(ts, results):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "insert_collect_cycle", explode)
    counts = collect.write_cycle(store, cyc)
    assert counts["errors"] == cyc.errors + 1
    assert store.conn.execute("SELECT count(*) FROM nws_forecasts").fetchone()[0] == 0


# --------------------------------------------------------------------------
# QA: the archive is the witness, the journal the fallback
# --------------------------------------------------------------------------


def _seed(db, cycles):
    """`cycles`: [(age_min, [SourceResult, ...]), ...]."""
    store = Store(str(db))
    for age, results in cycles:
        store.insert_collect_cycle(NOW - timedelta(minutes=age), results)
    store.close()
    return Store(str(db), read_only=True).conn


def test_archive_reader_keys_like_the_journal_and_skips_quiet_sources(tmp_path):
    conn = _seed(
        tmp_path / "a.duckdb",
        [
            (
                5 * i,
                [
                    SourceResult("nws", "AUS", 0, 0.1, "TypeError"),
                    SourceResult("kalshi", "KXCPIYOY", 0, 0.3),  # no open market: not an error
                    SourceResult("polymarket", "books", 1, 0.2),
                ],
            )
            for i in range(21)
        ]
        + [(10, [SourceResult("kalshi", "KXLOWTNYC", 0, 0.4, "HTTPError")])]
        + [(30 * 60, [SourceResult("nws", "AUS", 0, 0.1, "TypeError")])],  # outside 24h
    )
    assert qa.read_archive_collect_source_errors(conn, NOW.replace(tzinfo=None)) == {
        "kalshi KXLOWTNYC": {"HTTPError": 1},
        "nws AUS": {"TypeError": 21},
    }


def test_a_table_younger_than_the_window_is_none_so_the_journal_stays_the_witness(tmp_path):
    """The transition day: the table's first row is INSIDE the window, so a
    map over it covers part of the day and would read a pre-table episode
    (the 10-22 AUS 21) as a clean one. None, and the journal judges."""
    conn = _seed(
        tmp_path / "a.duckdb",
        [(5 * i, [SourceResult("nws", "AUS", 0, 0.1, "TypeError")]) for i in range(21)],
    )
    assert qa.read_archive_collect_source_errors(conn, NOW.replace(tzinfo=None)) is None


def test_a_table_as_old_as_the_window_with_nothing_in_it_is_an_empty_map(tmp_path):
    """Older than the window and nothing errored: {} (a clean day), not None
    (no witness) -- the fallback is for coverage, never for emptiness."""
    conn = _seed(tmp_path / "a.duckdb", [(30 * 60, [SourceResult("nws", "AUS", 0, 0.1)])])
    assert qa.read_archive_collect_source_errors(conn, NOW.replace(tzinfo=None)) == {}


@pytest.fixture
def _qa_state(tmp_path, monkeypatch):
    monkeypatch.setattr(qa, "STATE", tmp_path / "sections.json")
    monkeypatch.setattr(qa, "_archive_source_errors", None)
    qa._failures.clear()
    qa._skipped.clear()
    yield
    qa._failures.clear()
    qa._skipped.clear()


def test_the_check_reads_the_archive_map_and_never_opens_the_journal(
    _qa_state, monkeypatch, capsys
):
    def no_journal(unit, since, until):
        raise AssertionError("journal consulted while the archive had the answer")

    monkeypatch.setattr(qa, "_journal", no_journal)
    monkeypatch.setattr(qa, "_archive_source_errors", {"nws AUS": {"TypeError": 21}})
    qa.qa_collect_source_errors(now=NOW)
    out = capsys.readouterr().out
    assert qa._failures == ["collect sources each failing <= 5 cycles in 24h"]
    assert "FAILING nws AUS 21 cycle(s) (TypeError) [archive]" in out


def test_the_check_falls_back_to_the_journal_when_the_archive_map_is_none(
    _qa_state, monkeypatch, capsys
):
    monkeypatch.setattr(qa, "_journal", lambda unit, since, until: "-- No entries --\n")
    qa.qa_collect_source_errors(now=NOW)
    assert qa._failures == []
    assert "no source error line [journal]" in capsys.readouterr().out


def test_qa_archive_resets_the_map_before_reading(tmp_path, _qa_state, monkeypatch):
    """A run whose attach fails must not judge yesterday's map."""
    monkeypatch.setattr(qa, "_archive_source_errors", {"stale": {"X": 99}})
    db = tmp_path / "a.duckdb"
    Store(str(db)).close()
    qa.qa_archive(26.0, path=str(db))
    assert qa._archive_source_errors is None


def test_cycle_line_renders_filesystem_free_bytes(tmp_path):
    """The cycle line is the collector's only reading of the filesystem's
    free space at its own cadence (the QA series samples once a day and
    cannot locate a step inside the day). Prove by PERTURBATION that the
    value reaches the rendered line (#95), and that the helper reads the
    filesystem the archive lives on."""
    from collector import collect

    base = collect.cycle_line({"a": 1}, {"b": 2}, 0, {"total_s": 1.0}, 123456789)
    bumped = collect.cycle_line({"a": 1}, {"b": 2}, 0, {"total_s": 1.0}, 123456790)
    assert "fs_free_b=123456789" in base
    assert base != bumped
    assert "fs_free_b=None" in collect.cycle_line({}, {}, 0, {}, None)

    db = tmp_path / "hyxlab.duckdb"
    free = collect.fs_free_bytes(str(db))
    assert isinstance(free, int) and free > 0
    assert collect.fs_free_bytes("/nonexistent-dir-for-sure/x.duckdb") is None
