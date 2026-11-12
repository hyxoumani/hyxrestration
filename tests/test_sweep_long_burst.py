"""A write burst that holds the archive long says so in the journal, when it
happens (mistakes #185). The run's `attach_wait` line carries only the worst
hold of ~8,500: 103.5 s on 2026-11-12 with nothing to say which series."""

from __future__ import annotations

from collector import sweep as sweep_mod
from hyxlab.store import Store


def _db(tmp_path):
    db = tmp_path / "lab.duckdb"
    Store(str(db)).close()
    return str(db), str(tmp_path / "writer.lock")


def test_a_burst_at_the_threshold_prints_its_hold_and_file(tmp_path, capsys, monkeypatch):
    db, lock = _db(tmp_path)
    monkeypatch.setattr(sweep_mod, "LONG_BURST_S", 0.0)
    with sweep_mod.writer_burst(db, lock_file=lock):
        pass
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("[burst]")]
    assert len(lines) == 1
    assert lines[0].startswith("[burst] long write burst: held lab.duckdb ")
    assert lines[0].endswith("s")


def test_a_short_burst_prints_nothing(tmp_path, capsys):
    db, lock = _db(tmp_path)
    assert sweep_mod.LONG_BURST_S >= 10.0, "a routine burst is milliseconds; keep the line rare"
    with sweep_mod.writer_burst(db, lock_file=lock):
        pass
    assert "[burst]" not in capsys.readouterr().out


def test_a_burst_that_raises_still_reports_its_hold(tmp_path, capsys, monkeypatch):
    db, lock = _db(tmp_path)
    monkeypatch.setattr(sweep_mod, "LONG_BURST_S", 0.0)
    try:
        with sweep_mod.writer_burst(db, lock_file=lock):
            raise RuntimeError("mid-burst")
    except RuntimeError:
        pass
    assert "[burst] long write burst" in capsys.readouterr().out
