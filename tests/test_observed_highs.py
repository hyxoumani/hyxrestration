"""The weather series' settlement truth has a live writer and a reader.

Mistakes #251: `observations` (the NWS climate-report daily high each
KXHIGH* series settles on) was written once, by the hand-run backfill on
2026-07-06, and both QA coverage tests carried it as a declared exemption
for twelve weeks while the 5-min collector archived 144 days of forecasts
with nothing to score them against. `collector.signals` now pulls it
daily and QA asks every owed station its age.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
import requests

from collector import qa, signals
from collector.venues import iem
from hyxlab.store import Store

TODAY = date(2026, 11, 27)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(signals.time, "sleep", lambda _s: None)


def _year_of_highs(station: str, year: int, session=None):
    """What the archive serves: one row per day of the year up to TODAY - 1
    (the day in progress has, at best, a preliminary reading)."""
    out = []
    d = date(year, 1, 1)
    while d.year == year and d < TODAY:
        out.append((station, d, 50 + d.day))
        d += timedelta(days=1)
    return out


def test_pull_resumes_from_each_stations_newest_high_and_stops_short_of_today(monkeypatch):
    monkeypatch.setattr(iem, "STATION_ICAO", {"NYC": "KNYC", "DEN": "KDEN"})
    monkeypatch.setattr(iem, "get_observed_highs", _year_of_highs)
    newest = {"NYC": date(2026, 7, 6), "DEN": date(2026, 11, 24)}
    rows, outcomes = signals.fetch_cli_highs(requests.Session(), TODAY, newest)
    last = TODAY - timedelta(days=signals.CLI_SETTLE_DAYS)
    for st, have in newest.items():
        days = sorted(d for s, d, _h in rows if s == st)
        first = have - timedelta(days=signals.CLI_REFETCH_DAYS - 1)
        assert days[0] == first and days[-1] == last, (st, days[0], days[-1])
        assert len(days) == (last - first).days + 1  # no hole, no duplicate
        assert outcomes[st] == {"ok": True, "rows": len(days), "newest": last, "error": None}


def test_cold_start_reaches_back_across_the_year_boundary(monkeypatch):
    monkeypatch.setattr(iem, "STATION_ICAO", {"NYC": "KNYC"})
    asked: list[int] = []

    def fake(station, year, session=None):
        asked.append(year)
        return _year_of_highs(station, year)

    monkeypatch.setattr(iem, "get_observed_highs", fake)
    rows, _ = signals.fetch_cli_highs(requests.Session(), TODAY, {})
    assert asked == [2025, 2026]
    assert min(d for _s, d, _h in rows) == TODAY - timedelta(days=signals.CLI_COLD_START_DAYS - 1)


def test_a_day_served_without_a_high_is_not_stored(monkeypatch):
    monkeypatch.setattr(iem, "STATION_ICAO", {"MIA": "KMIA"})
    hole = date(2026, 11, 22)
    monkeypatch.setattr(
        iem,
        "get_observed_highs",
        lambda st, y, session=None: [
            (s, d, None if d == hole else h) for s, d, h in _year_of_highs(st, y)
        ],
    )
    rows, outcomes = signals.fetch_cli_highs(requests.Session(), TODAY, {"MIA": date(2026, 11, 24)})
    assert hole not in {d for _s, d, _h in rows}
    assert all(h is not None for _s, _d, h in rows)
    assert outcomes["MIA"]["ok"] and outcomes["MIA"]["rows"] == len(rows)


def test_one_failing_station_is_named_and_the_others_still_land(monkeypatch, capsys):
    monkeypatch.setattr(iem, "STATION_ICAO", {"NYC": "KNYC", "CHI": "KMDW"})

    def fake(station, year, session=None):
        if station == "CHI":
            raise requests.ConnectionError("down")
        return _year_of_highs(station, year)

    monkeypatch.setattr(iem, "get_observed_highs", fake)
    newest = {"NYC": date(2026, 11, 24), "CHI": date(2026, 11, 24)}
    rows, outcomes = signals.fetch_cli_highs(requests.Session(), TODAY, newest)
    assert {s for s, _d, _h in rows} == {"NYC"}
    assert outcomes["CHI"] == {"ok": False, "rows": 0, "newest": None, "error": "ConnectionError"}
    line = signals.cli_line(outcomes, TODAY)
    assert "CHI FAILED (ConnectionError)" in line
    assert "NYC 8 row(s) through 11-25" in line


def test_line_tells_nothing_new_from_a_failure_and_from_a_skip():
    quiet = {"AUS": {"ok": True, "rows": 0, "newest": None, "error": None}}
    assert "AUS nothing in the window" in signals.cli_line(quiet, TODAY)
    assert signals.cli_line({}, TODAY).endswith("skipped")


def _run_main(tmp_path, monkeypatch, capsys, highs_by_call):
    db = tmp_path / "hyxlab.duckdb"
    if not db.exists():
        Store(db).close()
    monkeypatch.setattr(signals, "LOCK_FILE", str(tmp_path / "writer.lock"))
    monkeypatch.setattr(signals, "fetch_alfred", lambda sess, today=None: ({}, {}))
    monkeypatch.setattr(signals, "record_fetch", lambda *a, **k: None)
    monkeypatch.setattr(iem, "STATION_ICAO", {"NYC": "KNYC"})
    monkeypatch.setattr(iem, "get_observed_highs", highs_by_call)
    monkeypatch.setattr("sys.argv", ["signals", "--db", str(db), "--skip-gdelt"])
    signals.main()
    out = capsys.readouterr().out
    store = Store(db, read_only=True)
    try:
        return out, store.observations()
    finally:
        store.close()


def test_main_lands_the_highs_in_the_archive_and_a_corrected_report_replaces_its_row(
    tmp_path, monkeypatch, capsys
):
    real_today = datetime.now(UTC).date()  # main reads the real clock
    last = real_today - timedelta(days=signals.CLI_SETTLE_DAYS)

    def served(high):
        def fake(station, year, session=None):
            d0 = real_today - timedelta(days=20)
            return [
                (station, d0 + timedelta(days=i), high)
                for i in range(20)
                if (d0 + timedelta(days=i)).year == year
            ]

        return fake

    out, obs = _run_main(tmp_path, monkeypatch, capsys, served(61))
    assert obs[("NYC", last)] == 61
    assert max(d for _s, d in obs) == last  # the day in progress is not stored
    assert "[signals] cli highs" in out and "NYC" in out
    # Next pull: the archive now serves a corrected value for the same days.
    out, obs2 = _run_main(tmp_path, monkeypatch, capsys, served(63))
    assert obs2[("NYC", last)] == 63
    assert len(obs2) == len(obs)  # replaced in place, not appended
    # ... and only the trailing window was re-read: older rows keep 61.
    assert obs2[("NYC", min(d for _s, d in obs))] == 61


# -- QA ----------------------------------------------------------------------


def _qa_failed(tmp_path, rows, stations, now):
    store = Store(tmp_path / "a.duckdb")
    if rows:
        store.upsert_observations(rows)
    qa._failures.clear()
    try:
        qa._check_observed_highs(store.conn, now, stations=stations)
        return set(qa._failures)
    finally:
        qa._failures.clear()
        store.close()


NOW = datetime(2026, 11, 27, 10, 0)
NAME = f"observed highs each fresh (<= {qa.OBS_HIGH_MAX_AGE_DAYS} d old)"


def test_qa_passes_on_the_daily_pulls_healthy_lag_and_one_missed_pull(tmp_path):
    rows = [("NYC", date(2026, 11, 25), 52), ("CHI", date(2026, 11, 24), 45)]
    assert _qa_failed(tmp_path, rows, ("NYC", "CHI"), NOW) == set()


def test_qa_fails_and_names_the_one_stale_station_behind_fresh_ones(tmp_path, capsys):
    rows = [("NYC", date(2026, 11, 25), 52), ("CHI", date(2026, 7, 6), 88)]
    assert _qa_failed(tmp_path, rows, ("NYC", "CHI"), NOW) == {NAME}
    out = capsys.readouterr().out
    assert "STALE CHI" in out and "CHI 07-06 (144 d)" in out and "NYC 11-25 (2 d)" in out


def test_qa_fails_for_an_owed_station_that_never_landed_a_row(tmp_path, capsys):
    rows = [("NYC", date(2026, 11, 25), 52)]
    assert _qa_failed(tmp_path, rows, ("NYC", "DEN"), NOW) == {NAME}
    assert "DEN never" in capsys.readouterr().out


def test_qa_does_not_count_a_row_with_no_high_as_fresh(tmp_path):
    rows = [("NYC", date(2026, 7, 6), 88), ("NYC", date(2026, 11, 25), None)]
    assert _qa_failed(tmp_path, rows, ("NYC",), NOW) == {NAME}


def test_qa_owes_every_station_the_pull_walks():
    """The owed set is the pull's own station map, so a station added to
    the pull is owed a row without a second list to remember."""
    import inspect

    src = inspect.getsource(qa._check_observed_highs)
    assert "iem.STATION_ICAO" in src
    assert "_check_observed_highs(conn, now)" in inspect.getsource(qa)
