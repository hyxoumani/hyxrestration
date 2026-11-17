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


# The queue in FRONT of the attach (mistakes #208). `writer_burst` takes
# `data/writer.lock` before `held_open`, and the attach ledger's clock starts
# after it: on 2026-11-16 this sweep's bursts queued behind the poly sweep's
# 94 s flush holds and its `attach_wait` line said 76 s slept in total.


def _hold_then_release(lock, seconds):
    """Hold `lock` from another open file description, as another writer
    does, record the holder, and let go after `seconds`."""
    import fcntl
    import threading

    from hyxlab.lockid import note_holder

    fh = open(lock, "a")  # noqa: SIM115 -- closed by the timer below
    fcntl.flock(fh, fcntl.LOCK_EX)
    note_holder(lock, pid=1)

    def _release():
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()

    t = threading.Timer(seconds, _release)
    t.start()
    return t


def test_a_burst_that_queued_on_the_flock_ledgers_the_wait(tmp_path):
    db, lock = _db(tmp_path)
    sweep_mod.reset_lock_wait()
    t = _hold_then_release(lock, 0.4)
    with sweep_mod.writer_burst(db, lock_file=lock):
        pass
    t.join()
    assert sweep_mod._LOCK_WAIT["n"] == 1
    assert 0.3 <= sweep_mod._LOCK_WAIT["total_s"] < 5.0
    assert sweep_mod._LOCK_WAIT["worst_s"] == sweep_mod._LOCK_WAIT["total_s"]


def test_a_long_queue_prints_its_wait_and_the_holder_it_queued_behind(
    tmp_path, capsys, monkeypatch
):
    db, lock = _db(tmp_path)
    sweep_mod.reset_lock_wait()
    monkeypatch.setattr(sweep_mod, "LONG_LOCK_WAIT_S", 0.2)
    t = _hold_then_release(lock, 0.4)
    with sweep_mod.writer_burst(db, lock_file=lock):
        pass
    t.join()
    (line,) = [ln for ln in capsys.readouterr().out.splitlines() if "long lock wait" in ln]
    assert line.startswith("[burst] long lock wait: queued 0.")
    # pid=1 is the OTHER holder's record: read before this burst overwrote it.
    assert "on writer.lock; last holder " in line and "pid=1 " in line
    assert sweep_mod.lock_wait_block()["long_n"] == 1


def test_an_uncontended_burst_counts_and_prints_no_queue_line(tmp_path, capsys):
    db, lock = _db(tmp_path)
    sweep_mod.reset_lock_wait()
    assert sweep_mod.LONG_LOCK_WAIT_S >= 10.0, "a routine queue is one collector write, ~4 s"
    for _ in range(3):
        with sweep_mod.writer_burst(db, lock_file=lock):
            pass
    block = sweep_mod.lock_wait_block()
    assert (block["n"], block["long_n"], block["min"]) == (3, 0, 0.0)
    assert "long lock wait" not in capsys.readouterr().out


def test_a_holder_record_that_cannot_be_read_does_not_abort_the_burst(
    tmp_path, capsys, monkeypatch
):
    db, lock = _db(tmp_path)
    monkeypatch.setattr(sweep_mod, "LONG_LOCK_WAIT_S", 0.0)

    def boom(_):
        raise ValueError("torn record")

    monkeypatch.setattr(sweep_mod, "describe_holder", boom)
    with sweep_mod.writer_burst(db, lock_file=lock) as store:
        assert store is not None
    assert "holder unreadable (ValueError)" in capsys.readouterr().out
