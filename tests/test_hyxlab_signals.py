"""Signals pull: vintage diffing keeps econ_vintages a true vintage log
despite the keyless endpoint restamping knowable_at every fetch day."""

import json
from datetime import date, datetime

from collector.signals import diff_vintages
from hyxlab.models import EconVintage
from hyxlab.store import Store

JUN = date(2026, 6, 1)
MAY = date(2026, 5, 1)


def _v(obs, value, knowable):
    return EconVintage("CPIAUCSL", obs, value, knowable)


def test_diff_keeps_only_new_periods_and_true_revisions(tmp_path):
    store = Store(tmp_path / "t.duckdb")
    day1 = {
        "CPIAUCSL": [_v(MAY, 330.0, datetime(2026, 7, 11)), _v(JUN, 331.0, datetime(2026, 7, 11))]
    }
    store.insert_vintages(diff_vintages(store, day1))
    assert store.conn.execute("SELECT count(*) FROM econ_vintages").fetchone()[0] == 2

    # day 2: identical values, new fetch-day knowable_at → nothing new
    day2 = {
        "CPIAUCSL": [_v(MAY, 330.0, datetime(2026, 7, 12)), _v(JUN, 331.0, datetime(2026, 7, 12))]
    }
    assert diff_vintages(store, day2) == []

    # day 3: June revised → exactly one new vintage row
    day3 = {
        "CPIAUCSL": [_v(MAY, 330.0, datetime(2026, 7, 13)), _v(JUN, 331.4, datetime(2026, 7, 13))]
    }
    new = diff_vintages(store, day3)
    assert [(v.obs_date, v.value) for v in new] == [(JUN, 331.4)]
    store.insert_vintages(new)

    # the vintage log now shows both June values with distinct releases
    rows = store.conn.execute(
        "SELECT value FROM econ_vintages WHERE obs_date = ? ORDER BY knowable_at", [JUN]
    ).fetchall()
    assert [r[0] for r in rows] == [331.0, 331.4]
    store.close()


# --- EXP-1360: the per-series fetch record. `fetch_alfred` retries three
# times and then moves on with the series simply absent from its result, so
# without this the only trace of a dropped series is a print.


def test_fetch_records_success_and_failure_per_series(tmp_path, monkeypatch):
    import requests

    from collector import signals
    from collector.venues import alfred
    from hyxlab.models import EconVintage

    monkeypatch.setattr(alfred, "SERIES", ["GOOD", "BAD"])
    monkeypatch.setattr(signals.time, "sleep", lambda _s: None)

    def fake_get(series_id, vintage_date, session=None):
        if series_id == "BAD":
            raise ValueError("unexpected ALFRED header")
        return [EconVintage("GOOD", date(2026, 8, 1), 1.0, datetime(2026, 8, 2, 3, 59))]

    monkeypatch.setattr(alfred, "get_vintage", fake_get)
    out, outcomes = signals.fetch_alfred(requests.Session(), today=date(2026, 8, 24))

    assert set(out) == {"GOOD"}  # the failed series vanishes from the result...
    assert outcomes["GOOD"] == {"ok": True, "rows": 1, "error": None}
    assert outcomes["BAD"] == {"ok": False, "rows": 0, "error": "ValueError"}  # ...but not here

    log = tmp_path / "fetch.jsonl"
    signals.record_fetch(
        date(2026, 8, 24), outcomes, at=datetime(2026, 8, 24, 4, 40), path=str(log)
    )
    row = json.loads(log.read_text().strip())
    assert row["vintage_date"] == "2026-08-24"
    assert row["series"]["BAD"]["ok"] is False


def test_record_fetch_never_breaks_a_successful_pull(tmp_path):
    """Losing the record must not fail a pull that otherwise worked — QA
    decides an absent sidecar against an independent witness instead."""
    from collector import signals

    unwritable = tmp_path / "afile"
    unwritable.write_text("")
    signals.record_fetch(date(2026, 8, 24), {}, path=str(unwritable / "nested" / "f.jsonl"))


def test_rows_counts_observations_fetched_not_rows_inserted(tmp_path, monkeypatch):
    """The diff drops everything unrevised, so an insert count of 0 is the
    normal daily case and says nothing about whether the fetch worked. A
    record keyed on inserts would call every quiet day a failure."""
    import requests

    from collector import signals
    from collector.venues import alfred
    from hyxlab.models import EconVintage

    monkeypatch.setattr(alfred, "SERIES", ["UNRATE"])
    monkeypatch.setattr(signals.time, "sleep", lambda _s: None)
    vints = [
        EconVintage("UNRATE", date(2026, 7, 1), 4.0, datetime(2026, 8, 25, 3, 59)),
        EconVintage("UNRATE", date(2026, 6, 1), 4.1, datetime(2026, 8, 25, 3, 59)),
    ]
    monkeypatch.setattr(alfred, "get_vintage", lambda *a, **k: vints)
    _out, outcomes = signals.fetch_alfred(requests.Session(), today=date(2026, 8, 24))
    assert outcomes["UNRATE"]["rows"] == 2
