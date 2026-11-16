"""A finished run's divergence replay reads a backup slot, not the live file.

`hyxlab.backupslot.covering_slot` picks the slot; `simulator.divergence`
obeys it only when nobody named a file. Measured 2026-11-16: the replay on
`hyxstream.Mon.duckdb` matched 10,083 of 10,083 fills with zero seconds
held on the live stream archive.
"""

import os
import sys
from datetime import UTC, datetime, timedelta

import duckdb
import pytest

from hyxlab.backupslot import SLOT_MARGIN, covering_slot
from simulator import divergence

END = datetime(2026, 11, 14, 12, 0, 0)


def _slot(root, name, taken, wal=False):
    p = root / name
    p.write_bytes(b"x")
    ts = taken.replace(tzinfo=UTC).timestamp()
    os.utime(p, (ts, ts))
    if wal:
        (root / f"{name}.wal").write_bytes(b"w")
    return p


def test_newest_slot_taken_after_the_margin_is_chosen(tmp_path):
    _slot(tmp_path, "hyxstream.Sat.duckdb", END + timedelta(hours=15))
    new = _slot(tmp_path, "hyxstream.Sun.duckdb", END + timedelta(hours=39))
    got = covering_slot("data/hyxstream.duckdb", END, backup_dir=tmp_path)
    assert (got.path, got.taken) == (new, END + timedelta(hours=39))


def test_slot_taken_inside_the_margin_does_not_cover(tmp_path):
    _slot(tmp_path, "hyxstream.Sat.duckdb", END + SLOT_MARGIN - timedelta(seconds=1))
    assert covering_slot("data/hyxstream.duckdb", END, backup_dir=tmp_path) is None


def test_slot_taken_exactly_at_the_margin_covers(tmp_path):
    p = _slot(tmp_path, "hyxstream.Sat.duckdb", END + SLOT_MARGIN)
    assert covering_slot("data/hyxstream.duckdb", END, backup_dir=tmp_path).path == p


def test_slot_taken_before_the_end_does_not_cover(tmp_path):
    _slot(tmp_path, "hyxstream.Fri.duckdb", END - timedelta(hours=8))
    assert covering_slot("data/hyxstream.duckdb", END, backup_dir=tmp_path) is None


def test_another_archives_slot_is_not_this_archives(tmp_path):
    _slot(tmp_path, "hyxlab.Sun.duckdb", END + timedelta(hours=39))
    _slot(tmp_path, "hyxshadow.Sun.duckdb", END + timedelta(hours=39))
    assert covering_slot("data/hyxstream.duckdb", END, backup_dir=tmp_path) is None


def test_slot_with_a_wal_beside_it_is_declined(tmp_path):
    _slot(tmp_path, "hyxstream.Sun.duckdb", END + timedelta(hours=39), wal=True)
    assert covering_slot("data/hyxstream.duckdb", END, backup_dir=tmp_path) is None


def test_spill_directory_named_like_a_slot_is_not_a_slot(tmp_path):
    (tmp_path / "hyxstream.Sun.duckdb").mkdir()
    assert covering_slot("data/hyxstream.duckdb", END, backup_dir=tmp_path) is None


def test_no_backup_dir_and_no_env_is_none(tmp_path, monkeypatch):
    monkeypatch.delenv("HYXLAB_BACKUP_DIR", raising=False)
    assert covering_slot("data/hyxstream.duckdb", END) is None


def test_env_names_the_directory(tmp_path, monkeypatch):
    p = _slot(tmp_path, "hyxstream.Sun.duckdb", END + timedelta(hours=39))
    monkeypatch.setenv("HYXLAB_BACKUP_DIR", str(tmp_path))
    assert covering_slot("data/hyxstream.duckdb", END).path == p


def test_unknown_end_is_none(tmp_path):
    _slot(tmp_path, "hyxstream.Sun.duckdb", END + timedelta(hours=39))
    assert covering_slot("data/hyxstream.duckdb", None, backup_dir=tmp_path) is None


# -- the report obeys it ---------------------------------------------------


def test_explicit_stream_db_wins_over_a_covering_slot(tmp_path, monkeypatch):
    _slot(tmp_path, "hyxstream.Sun.duckdb", END + timedelta(hours=39))
    monkeypatch.setenv("HYXLAB_BACKUP_DIR", str(tmp_path))
    path, source = divergence.stream_source_for("elsewhere.duckdb", END)
    assert (path, source["kind"]) == ("elsewhere.duckdb", "explicit")


def test_no_slot_reads_live_and_the_line_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv("HYXLAB_BACKUP_DIR", str(tmp_path))
    path, source = divergence.stream_source_for(None, END)
    assert (path, source["kind"]) == (divergence.STREAM_DB, "live")
    assert "LIVE" in divergence.stream_source_line(source)


def _run_main(tmp_path, monkeypatch, extra=()):
    from tests.test_hyxlab_divergence import _runs_db

    seen = {}

    def _replay(*a, **k):
        seen.update(k)
        return [], []

    db = _runs_db(tmp_path, [("done", datetime(2026, 8, 29), 5), ("live", datetime(2026, 9, 7), 1)])
    # The run's end is its last equity row; the shared fixture writes none.
    with duckdb.connect(str(db)) as conn:
        conn.execute("INSERT INTO shadow_equity VALUES ('done', ?, 0.0)", [datetime(2026, 8, 30)])
    monkeypatch.setattr(divergence, "replay_run", _replay)
    out = tmp_path / "reports"
    monkeypatch.setattr(
        sys, "argv", ["divergence", "--shadow-db", str(db), "--out", str(out), *extra]
    )
    divergence.main()
    import json

    return seen, json.loads(next(out.glob("*.json")).read_text())


def test_main_replays_from_the_slot_and_records_it(tmp_path, monkeypatch):
    slots = tmp_path / "slots"
    slots.mkdir()
    p = _slot(slots, "hyxstream.Sun.duckdb", datetime.now(UTC).replace(tzinfo=None))
    monkeypatch.setenv("HYXLAB_BACKUP_DIR", str(slots))
    seen, report = _run_main(tmp_path, monkeypatch)
    assert seen["stream_db"] == str(p)
    assert report["stream_source"]["kind"] == "backup_slot"
    assert report["stream_source"]["path"] == str(p)


def test_main_without_a_slot_replays_from_the_live_archive(tmp_path, monkeypatch):
    monkeypatch.delenv("HYXLAB_BACKUP_DIR", raising=False)
    seen, report = _run_main(tmp_path, monkeypatch)
    assert seen["stream_db"] == divergence.STREAM_DB
    assert report["stream_source"]["kind"] == "live"


def test_main_obeys_an_explicit_stream_db(tmp_path, monkeypatch):
    slots = tmp_path / "slots"
    slots.mkdir()
    _slot(slots, "hyxstream.Sun.duckdb", datetime.now(UTC).replace(tzinfo=None))
    monkeypatch.setenv("HYXLAB_BACKUP_DIR", str(slots))
    seen, _ = _run_main(tmp_path, monkeypatch, extra=("--stream-db", "named.duckdb"))
    assert seen["stream_db"] == "named.duckdb"


@pytest.mark.parametrize("kind", ["backup_slot", "live", "explicit"])
def test_every_source_kind_has_a_line(kind):
    line = divergence.stream_source_line({"kind": kind, "path": "p.duckdb", "taken": "t"})
    assert "p.duckdb" in line
