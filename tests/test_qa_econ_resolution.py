"""`qa_econ_resolution`: the market settles on the FIRST print, as the
vintage table holds it.

Measured 2026-10-14 on the 10-13 backup: 14 KXJOBLESSCLAIMS events / 146
strikes agree with `first_print >= floor_strike`, 0 mismatches, while the 9
events that settled before the ICSA pull existed carry only the REVISED value
and 4 of them would read as one crossed strike each. The pre-registration
queued for ~26 quoted events rests on this invariant. These arms pin: a
consistent archive passes; one crossed strike fails and names the event; an
event whose first print was NOT captured (pull started later, so its earliest
vintage is a revision) is excluded, and with nothing left the line is an
UNVERIFIED watch, never a pass.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from collector import qa
from hyxlab.store import Store

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
CLOSE = datetime(2026, 10, 8, 12, 25)  # Thursday; obs week ends Sat 10-03
OBS = CLOSE.date() - timedelta(days=5)


def _markets(
    store, print_value: float, strikes=(190000, 195000, 200000), flip: float | None = None
):
    rows = []
    for k in strikes:
        yes = print_value >= k
        if flip == k:
            yes = not yes
        rows.append(
            (
                "kalshi",
                f"KXJOBLESSCLAIMS-26OCT08-{k}",
                "t",
                "KXJOBLESSCLAIMS",
                CLOSE,
                "greater_or_equal",
                float(k),
                None,
                "yes" if yes else "no",
                CLOSE.date(),
                CLOSE,
                None,
            )
        )
    store.conn.executemany("INSERT INTO markets VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)


def _vintages(store, values_and_knowable):
    store.conn.executemany(
        "INSERT INTO econ_vintages VALUES (?, ?, ?, ?)",
        [("ICSA", OBS, v, k) for v, k in values_and_knowable],
    )


def _run(db, capsys):
    qa._failures.clear()
    store = Store(db)
    try:
        qa.qa_econ_resolution(store.conn, NOW.replace(tzinfo=None))
    finally:
        store.close()
    failed, out = set(qa._failures), capsys.readouterr().out
    qa._failures.clear()
    return failed, out


NAME = "econ market settles on the first print (KXJOBLESSCLAIMS vs ICSA)"
FIRST_AT = CLOSE + timedelta(hours=15, minutes=34)  # next day 03:59Z pull
REVISED_AT = FIRST_AT + timedelta(days=7)


def test_a_consistent_archive_passes_and_counts_events_and_strikes(tmp_path, capsys):
    db = tmp_path / "a.duckdb"
    store = Store(db)
    _markets(store, 197000.0)
    _vintages(store, [(197000.0, FIRST_AT), (199000.0, REVISED_AT)])
    store.close()
    failed, out = _run(db, capsys)
    assert NAME not in failed
    assert "1 event(s) / 3 strike(s) verified against the first print, 0 mismatches" in out


def test_one_crossed_strike_fails_and_names_the_event(tmp_path, capsys):
    db = tmp_path / "a.duckdb"
    store = Store(db)
    _markets(store, 197000.0, flip=195000)
    _vintages(store, [(197000.0, FIRST_AT)])
    store.close()
    failed, out = _run(db, capsys)
    assert NAME in failed
    assert "MISMATCH on 2026-10-08 (print 197000, 1 strike(s))" in out


def test_the_first_print_decides_not_the_revision(tmp_path, capsys):
    """The revised value crosses the 200000 strike; the first print did not.
    `arg_min(value, knowable_at)` must pick the first, or the real archive's
    13-of-13 upward revisions would read as mismatches."""
    db = tmp_path / "a.duckdb"
    store = Store(db)
    _markets(store, 199000.0)
    _vintages(store, [(199000.0, FIRST_AT), (201000.0, REVISED_AT)])
    store.close()
    failed, _ = _run(db, capsys)
    assert NAME not in failed


def test_an_event_whose_first_print_was_not_captured_is_unverified_never_a_pass(tmp_path, capsys):
    """Only a vintage from long after the close exists (the pull started
    later), so the stored value may be a revision: the event is excluded,
    and with nothing left the line is a watch, not a pass."""
    db = tmp_path / "a.duckdb"
    store = Store(db)
    _markets(store, 199000.0)
    _vintages(store, [(201000.0, CLOSE + timedelta(days=30))])
    store.close()
    failed, out = _run(db, capsys)
    assert NAME not in failed
    assert f"WATCH {NAME} — UNVERIFIED" in out
    assert "PASS" not in out


def test_the_window_is_inside_one_revision_cycle():
    assert qa.ECON_FIRST_PRINT_WINDOW_D < 7
