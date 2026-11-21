"""The raw-frame copy yields the last gigabytes to the parsed archive
(mistakes #227). Measured 2026-11-21: 16 GB free, raw frames 14.4 GB/day of
a 28 GB/day net burn -- on a filling disk the copy took every second byte
the primary record needed."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from collector import rawframes
from collector.rawframes import RawFrameWriter

RECV = datetime(2026, 11, 21, 20, 0, 0, tzinfo=UTC)
GB = 1024**3
#: Taken at collection, before conftest's per-test replacement of the probe.
REAL_PROBE = rawframes.fs_free_bytes


class Disk:
    def __init__(self, free):
        self.free = free
        self.asked = []

    def __call__(self, root):
        self.asked.append(root)
        return self.free


def _writer(tmp_path, disk, **kw):
    return RawFrameWriter(tmp_path, yield_below=6 * GB, resume_margin=1 * GB, free_bytes=disk, **kw)


def _files(tmp_path):
    return sorted(p for p in tmp_path.rglob("*") if p.is_file())


def test_under_the_floor_frames_are_discarded_counted_and_nothing_is_written(tmp_path, capsys):
    disk = Disk(5 * GB)
    w = _writer(tmp_path, disk)
    assert w.check_disk() is True
    for i in range(3):
        assert w.put("books", "books-1", RECV, f"frame-{i}")  # the queue still takes them
    assert w.close()
    st = w.stats()
    assert st["raw_yielded"] == 3 and st["raw_written"] == 0 and st["raw_errors"] == 0
    assert st["raw_queued_bytes"] == 0  # discarded, not held in memory
    assert _files(tmp_path) == []  # not even an empty hour file or day directory
    out = capsys.readouterr().out
    assert "YIELDING the disk to the archive" in out and "5.4 GB" in out and "6.4 GB floor" in out
    assert disk.asked == [tmp_path]


def test_above_the_floor_nothing_changes(tmp_path, capsys):
    w = _writer(tmp_path, Disk(6 * GB))  # AT the floor is not under it
    assert w.check_disk() is False
    w.put("books", "books-1", RECV, "frame")
    assert w.close()
    assert w.stats()["raw_written"] == 1 and w.stats()["raw_yielded"] == 0
    assert len(_files(tmp_path)) == 1
    assert capsys.readouterr().out == ""


def test_entering_the_yield_closes_open_files_so_no_buffered_block_lands_later(tmp_path):
    disk = Disk(50 * GB)
    w = _writer(tmp_path, disk)
    w.put("books", "books-1", RECV, "before")
    w._drain()
    assert set(w._files) == {"books"}
    disk.free = 5 * GB
    assert w.check_disk() is True
    assert w._files == {}
    (only,) = _files(tmp_path)
    size = only.stat().st_size
    w.put("books", "books-1", RECV, "during")
    w._drain()
    w._flush_all()
    assert only.stat().st_size == size and w.stats()["raw_yielded"] == 1


def test_writing_resumes_only_past_the_margin_and_the_file_still_decodes(tmp_path, capsys):
    from compression import zstd

    disk = Disk(50 * GB)
    w = _writer(tmp_path, disk)
    w.put("books", "books-1", RECV, "before")
    w._drain()
    disk.free = 5 * GB
    w.check_disk()
    w.put("books", "books-1", RECV + timedelta(seconds=1), "lost")
    w._drain()
    disk.free = 6 * GB + GB // 2  # over the floor, inside the margin: still yielding
    assert w.check_disk() is True
    disk.free = 7 * GB
    assert w.check_disk() is False
    w.put("books", "books-1", RECV + timedelta(seconds=2), "after")
    assert w.close()
    (only,) = _files(tmp_path)
    text = zstd.decompress(only.read_bytes()).decode()
    assert [ln.split("\t")[2] for ln in text.splitlines()] == ["before", "after"]
    assert w.stats() == {**w.stats(), "raw_written": 2, "raw_yielded": 1}
    out = capsys.readouterr().out
    assert out.count("YIELDING") == 1  # one line per transition, not per sample
    assert "writing again -- 7.5 GB free; 1 frame(s) were discarded in all" in out


def test_an_unreadable_disk_is_not_a_full_one_and_none_disables_the_floor(tmp_path):
    w = _writer(tmp_path, Disk(None))
    assert w.check_disk() is False
    w.yielding = True  # ...and it does not end a yield either
    assert w.check_disk() is True
    disk = Disk(0)
    off = RawFrameWriter(tmp_path, yield_below=None, free_bytes=disk)
    assert off.check_disk() is False and disk.asked == []


def test_the_writer_thread_samples_before_it_writes_and_on_its_flush_cadence(tmp_path):
    disk = Disk(5 * GB)
    w = _writer(tmp_path, disk, flush_secs=0.01)
    w.start()
    w.put("trades", "trades", RECV, "x")
    assert w.close(5)
    assert w.stats()["raw_yielded"] == 1 and _files(tmp_path) == []
    assert disk.asked  # the thread asked; nobody called check_disk by hand


def test_the_real_probe_reads_the_roots_filesystem_from_the_nearest_existing_dir(tmp_path):
    assert REAL_PROBE.__name__ == "fs_free_bytes" and rawframes.fs_free_bytes is not REAL_PROBE
    st = os.statvfs(tmp_path)
    got = REAL_PROBE(
        tmp_path / "not" / "made" / "yet"
    )  # the root does not exist before the first frame
    assert got is not None and abs(got - st.f_bavail * st.f_frsize) < 1 * GB


def test_the_floor_sits_above_journalds_keep_free_and_the_daemon_takes_the_default():
    import inspect

    from collector import streamd

    assert rawframes.DISK_YIELD_BYTES == 6 * GB > 4 * GB  # SystemKeepFree default, mistakes #222
    assert rawframes.DISK_RESUME_MARGIN_BYTES == 1 * GB
    sig = inspect.signature(RawFrameWriter.__init__)
    assert sig.parameters["yield_below"].default == rawframes.DISK_YIELD_BYTES
    assert sig.parameters["free_bytes"].default is None  # resolved per sample, not at def time
    assert "yield_below" not in inspect.getsource(streamd)  # production runs the default


def test_a_disk_that_fills_while_the_thread_runs_is_seen_within_a_flush_period(tmp_path):
    import time

    disk = Disk(50 * GB)
    w = _writer(tmp_path, disk, flush_secs=0.01)
    w.start()
    w.put("trades", "trades", RECV, "kept")
    deadline = time.monotonic() + 5
    while w.stats()["raw_written"] < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    disk.free = 5 * GB
    while not w.yielding and time.monotonic() < deadline:
        time.sleep(0.01)
    assert w.yielding  # nobody but the thread's own cadence could have set it
    w.put("trades", "trades", RECV, "discarded")
    assert w.close(5)
    assert w.stats()["raw_written"] == 1 and w.stats()["raw_yielded"] == 1
