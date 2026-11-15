"""QA's sliced walks report their busiest slice, and fail past the rows the
slice width was measured at.

Mistakes #199: the seq check's 2 h slice was sized as "~24M rows", carried
80-95M on a Saturday evening, and the first instrument to read that was the
kernel -- the 2026-11-15 run was cgroup-killed inside the check and every
check after it went unread. A slice is bounded in time, so its rows follow
the venue; the bound that matters is the one the width was MEASURED at.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import duckdb
import pytest

from collector import qa
from hyxlab.streamstore import BookEvent, StreamStore
from tests.test_qa_holds import _stream

NOW = datetime(2026, 11, 15, 10, 0, 0)


def _ev(venue, ts, sid, seq, kind="delta"):
    return BookEvent(venue, f"M{sid}", ts, None, sid, seq, kind, "yes", 0.5, 1.0)


def _book(db, rows):
    st = StreamStore(db)
    st.append_events(rows)
    st.flush()
    return duckdb.connect(str(db), read_only=True)


def test_seq_walk_names_its_busiest_slice_and_counts_only_its_own_rows(tmp_path):
    quiet = NOW - timedelta(hours=10) + timedelta(minutes=5)
    busy = NOW - timedelta(hours=3) + timedelta(minutes=10)
    rows = [_ev("kalshi", quiet + timedelta(seconds=i), 1, i + 1) for i in range(3)]
    rows += [_ev("kalshi", busy + timedelta(seconds=i), 1, i + 4) for i in range(7)]
    # No sid/seq: not the seq walk's input, in the busy slice or anywhere.
    rows += [_ev("polymarket", busy + timedelta(seconds=i), None, None) for i in range(50)]
    conn = _book(tmp_path / "s.duckdb", rows)
    stats: dict = {}
    assert qa._seq_holes(conn, 26, NOW, stats) == qa._seq_holes(conn, 26, NOW)
    assert stats == {"rows": 7, "lo": NOW - timedelta(hours=3)}


def test_poly_walk_names_its_busiest_slice_and_counts_only_polymarket(tmp_path):
    busy = NOW - timedelta(hours=5) + timedelta(minutes=20)
    rows = [_ev("polymarket", busy + timedelta(seconds=i), None, None, "snap") for i in range(5)]
    rows += [_ev("polymarket", NOW - timedelta(hours=20), None, None, "snap")]
    rows += [_ev("kalshi", busy + timedelta(seconds=i), 1, i + 1) for i in range(40)]
    conn = _book(tmp_path / "s.duckdb", rows)
    stats: dict = {}
    assert qa._poly_replay(conn, NOW, 26, stats) == qa._poly_replay(conn, NOW, 26)
    assert stats == {"rows": 5, "lo": NOW - timedelta(hours=5)}


NAME = "slice walks inside their measured input"


@pytest.fixture
def _clean(tmp_path, monkeypatch):
    monkeypatch.setattr(qa, "STATE", tmp_path / "sections.json")
    qa._failures.clear()
    yield
    qa._failures.clear()


def test_a_slice_at_its_measured_rows_passes_and_one_row_past_fails(_clean, capsys):
    lo = datetime(2026, 11, 15, 1, 30)
    qa.slice_walk_check([("seq", 0.5, {"rows": 40_000_000, "lo": lo}, 40_000_000)])
    assert NAME not in qa._failures
    assert "seq busiest 0.5 h slice 40.00M rows from 11-15 01:30Z (measured to 40M)" in (
        capsys.readouterr().out
    )
    qa.slice_walk_check(
        [
            ("seq", 0.5, {"rows": 40_000_001, "lo": lo}, 40_000_000),
            ("poly replay", 1, {"rows": 900_000, "lo": lo}, 2_000_000),
        ]
    )
    assert qa._failures == [NAME]
    out = capsys.readouterr().out
    assert "seq PAST the measured input" in out and "poly replay PAST" not in out
    assert "poly replay busiest 1 h slice 0.90M rows" in out


def test_a_walk_that_never_ran_is_said_so_and_is_not_a_breach(_clean, capsys):
    qa.slice_walk_check([("seq", 0.5, {}, 40_000_000)])
    assert qa._failures == [] and "seq: no slice walked" in capsys.readouterr().out


def _live_stream(tmp_path):
    path = _stream(tmp_path / "s.duckdb")
    st = StreamStore(path)
    now = datetime.now(UTC).replace(tzinfo=None)
    st.append_events(
        [_ev("kalshi", now - timedelta(minutes=m), 1, 10 - m) for m in (3, 2, 1)]
        + [_ev("polymarket", now - timedelta(minutes=2), None, None, "snap")]
    )
    st.flush()
    return path


def test_the_stream_section_runs_the_check_green_on_a_small_archive(_clean, tmp_path, capsys):
    qa.qa_stream(26.0, path=str(_live_stream(tmp_path)))
    out = capsys.readouterr().out
    assert f"PASS  {NAME}" in out, out
    assert "seq busiest 0.5 h slice 0.00M rows" in out and "poly replay busiest 1 h slice" in out


@pytest.mark.parametrize("const", ["SEQ_SLICE_MEASURED_ROWS", "POLY_SLICE_MEASURED_ROWS"])
def test_the_stream_section_fails_a_walk_past_its_measured_rows(
    _clean, tmp_path, monkeypatch, const
):
    """Driven red through the real section, once per walk: the walk's own
    count reaches the verdict, not a number handed to the renderer."""
    monkeypatch.setattr(qa, const, 0)
    qa.qa_stream(26.0, path=str(_live_stream(tmp_path)))
    assert NAME in qa._failures


def test_the_alarm_sits_under_the_largest_slice_measured():
    """The constants' own arithmetic (qa.py, 2026-11-15): 53.4M rows measured
    at 7.8 GB of the 12G cap; the busiest slice that day was 27.07M."""
    assert 27_072_002 < qa.SEQ_SLICE_MEASURED_ROWS < 53_400_000
    assert 904_611 < qa.POLY_SLICE_MEASURED_ROWS <= 2_010_000
