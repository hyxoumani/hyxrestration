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
    store,
    print_value: float,
    strikes=(190000, 195000, 200000),
    flip: float | None = None,
    series: str = "KXJOBLESSCLAIMS",
    unit: float = 1.0,
):
    rows = []
    for k in strikes:
        yes = print_value >= k * unit
        if flip == k:
            yes = not yes
        rows.append(
            (
                "kalshi",
                f"{series}-26OCT08-T{k}",
                "t",
                series,
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


def _vintages(store, values_and_knowable, series_id: str = "ICSA", obs=OBS):
    store.conn.executemany(
        "INSERT INTO econ_vintages VALUES (?, ?, ?, ?)",
        [(series_id, obs, v, k) for v, k in values_and_knowable],
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


# --- the continuing-claims pair: strikes in thousands, prints in persons ---

CC_NAME = "econ market settles on the first print (KXCONTCLAIMS vs CCSA)"
CC_OBS = CLOSE.date() - timedelta(days=12)  # the Saturday before ICSA's week
CC_STRIKES = (1700, 1710, 1720)  # `T1710` = 1,710K, as the venue quotes since 09-03


def test_the_continuing_claims_pair_compares_in_the_series_unit(tmp_path, capsys):
    """CCSA is held in persons (1,716,000) and the strikes in thousands
    (1710): scaled, 1716000 >= 1710000 is yes and >= 1720000 is no. Without
    the x1000 every strike reads yes against 1,716,000, and the one `no`
    market here would be a MISMATCH -- this arm is red under unit=1.0."""
    db = tmp_path / "a.duckdb"
    store = Store(db)
    _markets(store, 1716000.0, strikes=CC_STRIKES, series="KXCONTCLAIMS", unit=1000.0)
    _vintages(store, [(1716000.0, FIRST_AT), (1719000.0, REVISED_AT)], "CCSA", CC_OBS)
    store.close()
    failed, out = _run(db, capsys)
    assert CC_NAME not in failed
    assert f"PASS {CC_NAME}" in out or "1 event(s) / 3 strike(s) verified" in out
    assert "UNIT" not in out


def test_a_strike_quoted_in_the_wrong_unit_is_a_unit_failure_not_a_verdict(tmp_path, capsys):
    """The 08-27 event was quoted in millions (`T1.75`): scaled as thousands
    it is 1,750 against a 1,716,000 print, so every strike would read yes
    and the three `no` markets would be MISMATCHes of a compare that means
    nothing. The guard names the event as UNIT and keeps those strikes out
    of the mismatch count."""
    db = tmp_path / "a.duckdb"
    store = Store(db)
    # Results as the venue settled them in millions: 1.716 >= 1.70, 1.71; not 1.72.
    _markets(store, 1.716, strikes=(1.70, 1.71, 1.72), series="KXCONTCLAIMS", unit=1.0)
    _vintages(store, [(1716000.0, FIRST_AT)], "CCSA", CC_OBS)
    store.close()
    failed, out = _run(db, capsys)
    assert CC_NAME in failed
    assert "UNIT (strike x1000 not within 2x of the print) on 2026-10-08" in out
    assert "3 strike(s))" in out
    assert "MISMATCH" not in out


def test_each_pair_observes_the_saturday_its_print_reports():
    """Both claims prints report a week ending Saturday; a Thursday close
    minus the pair's offset must land on one, and the continuing-claims
    print lags the initial-claims print by exactly one week. The unit is
    the venue's quoting (persons; thousands since 09-03)."""
    assert CLOSE.weekday() == 3
    pairs = {s: (o, u) for s, _, o, u in qa.ECON_RESOLUTION_PAIRS}
    for offset, _unit in pairs.values():
        assert (CLOSE.date() - timedelta(days=offset)).weekday() == 5
    assert pairs["KXCONTCLAIMS"][0] - pairs["KXJOBLESSCLAIMS"][0] == 7
    assert pairs["KXJOBLESSCLAIMS"][1] == 1.0
    assert pairs["KXCONTCLAIMS"][1] == 1000.0
    assert qa.ECON_STRIKE_UNIT_FACTOR < 1000  # a factor that admits the wrong unit guards nothing
