"""`qa_stream` holds the LIVE stream archive for a reflink copy, not a section.

Mistakes #141: the hold #102 scoped to the section was bounded by the
section's input, and the input grew 30x in two days -- 872.3s on
2026-10-25 against the 28-52s band, while the stream daemon buffered a
million rows and took 8,789 socket throttles flushing them. The locked leg
of `collector.backup` reflinks the file in ~0.4s (measured on the 68 GB live
archive), so the walk now runs on a copy nobody else wants.
"""

import time
from pathlib import Path

import pytest

import collector.qa as qa
from hyxlab.store import attach_wait_block, reset_attach_waits
from tests.test_qa_holds import _only_qas_attaches, _snapshot_name, _stream


def test_live_hold_is_the_copy_and_the_section_runs_on_the_snapshot(tmp_path, monkeypatch):
    path = _stream(tmp_path / "s.duckdb")
    _only_qas_attaches()
    seen: dict = {}

    def body(conn, hours, now):
        seen["path"] = conn.execute("PRAGMA database_list").fetchall()[0][2]
        time.sleep(0.2)

    monkeypatch.setattr(qa, "_stream_checks", body)
    qa.qa_stream(26.0, path=str(path))
    block = attach_wait_block(rows=False)
    snap = _snapshot_name(block, "s")
    assert Path(seen["path"]).name == snap, "the section ran on the live file"
    assert block["held_by_db"]["s.duckdb"]["held_s_total"] < 0.1, (
        "the live file was held across the section -- the snapshot bought nothing"
    )
    assert block["held_by_db"][snap]["held_s_total"] >= 0.2
    # Nothing left behind: the copy, its WAL, and DuckDB's spill dir.
    snap_dir = tmp_path / qa.STREAM_SNAPSHOT_DIRNAME
    assert snap_dir.is_dir() and list(snap_dir.iterdir()) == []
    # The snapshot sits beside the SOURCE, never under the repo's data/.
    assert snap_dir.parent == path.parent


def test_a_dead_runs_snapshot_is_removed_before_the_next_copy(tmp_path, monkeypatch):
    path = _stream(tmp_path / "s.duckdb")
    snap_dir = tmp_path / qa.STREAM_SNAPSHOT_DIRNAME
    snap_dir.mkdir()
    (snap_dir / "s.Tue.duckdb").write_bytes(b"stale")
    (snap_dir / "s.Tue.duckdb.tmp").mkdir()
    (snap_dir / "s.Tue.duckdb.tmp" / "pid-1").write_bytes(b"spill")
    monkeypatch.setattr(qa, "_stream_checks", lambda *a, **k: None)
    qa.qa_stream(26.0, path=str(path))
    assert list(snap_dir.iterdir()) == []


def test_snapshot_failure_falls_back_to_the_live_file(tmp_path, monkeypatch, capsys):
    path = _stream(tmp_path / "s.duckdb")
    _only_qas_attaches()

    def refuse(*a, **k):
        raise OSError("no space")

    monkeypatch.setattr(qa, "backup_one", refuse)
    ran: dict = {}
    monkeypatch.setattr(qa, "_stream_checks", lambda conn, h, n: ran.setdefault("ok", True))
    qa.qa_stream(26.0, path=str(path))
    assert ran == {"ok": True}, "the section did not run on the fallback"
    block = attach_wait_block(rows=False)
    assert set(block["held_by_db"]) == {"s.duckdb"}, "the fallback is the live file, by name"
    out = capsys.readouterr().out
    assert "stream snapshot FAILED (OSError: no space)" in out
    assert "attaching the live file" in out


def test_the_section_still_raises_through_the_snapshot_and_cleans_up(tmp_path, monkeypatch):
    path = _stream(tmp_path / "s.duckdb")
    reset_attach_waits()

    def boom(*a, **k):
        raise RuntimeError("mid-section")

    monkeypatch.setattr(qa, "_stream_checks", boom)
    with pytest.raises(RuntimeError):
        qa.qa_stream(26.0, path=str(path))
    assert list((tmp_path / qa.STREAM_SNAPSHOT_DIRNAME).iterdir()) == []
