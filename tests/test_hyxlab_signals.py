"""Signals pull: vintage diffing keeps econ_vintages a true vintage log
despite the keyless endpoint restamping knowable_at every fetch day."""

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
