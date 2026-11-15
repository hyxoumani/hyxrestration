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


# --- a KILLED run: the `finally` above is not a cleanup SIGKILL reaches -------

_REPO = Path(__file__).resolve().parent.parent
_KILLED_RUN = """
import sys, time
import collector.qa as qa
def body(conn, hours, now):
    print("IN", flush=True)
    time.sleep(120)
qa._stream_checks = body
qa.qa_stream(26.0)
"""


def _run_qa(cwd, *args, **kw):
    import subprocess
    import sys

    return subprocess.Popen(
        [sys.executable, "-u", *args],
        cwd=cwd,
        env={"PYTHONPATH": str(_REPO), "PATH": "/usr/bin:/bin"},
        stdout=subprocess.PIPE,
        text=True,
        **kw,
    )


def test_a_killed_run_leaves_its_snapshot_and_the_stop_post_clears_it(tmp_path):
    """Both arms, from outside the process. The 2026-11-15 10:03Z run was
    killed at its cgroup cap mid-section; nothing in the process can clean up
    after SIGKILL, so the unit's ExecStopPost does (`--clear-snapshot`)."""
    (tmp_path / "data").mkdir()
    _stream(tmp_path / "data" / "hyxstream.duckdb")
    snap_dir = tmp_path / "data" / qa.STREAM_SNAPSHOT_DIRNAME

    child = _run_qa(tmp_path, "-c", _KILLED_RUN)
    try:
        lines = iter(child.stdout.readline, "")
        assert any(ln.strip() == "IN" for ln in lines), "the section never started"
    finally:
        child.kill()
        child.wait()
    left = list(snap_dir.iterdir())
    assert left, "vacuity: the killed run left nothing, so this test proves nothing"

    post = _run_qa(tmp_path, "-m", "collector.qa", "--clear-snapshot")
    out, _ = post.communicate(timeout=60)
    assert post.returncode == 0
    assert list(snap_dir.iterdir()) == []
    assert "stop-post: cleared" in out and "GB apparent" in out
    assert (tmp_path / "data" / "hyxstream.duckdb").exists(), "the cleanup touched the archive"

    # A run that reached its own cleanup: the stop-post says so and removes nothing.
    again = _run_qa(tmp_path, "-m", "collector.qa", "--clear-snapshot")
    out, _ = again.communicate(timeout=60)
    assert again.returncode == 0 and "already empty" in out
    # It ran no check: no section record, no run record, in the cwd it was given.
    assert not (tmp_path / "reports").exists()


def test_clear_snapshot_without_a_directory_is_a_clean_zero(tmp_path):
    assert qa.clear_stream_snapshot(str(tmp_path / "data" / "hyxstream.duckdb")) == (0, 0)


def test_the_qa_unit_clears_the_snapshot_at_stop_after_the_cgroup_read():
    unit = (_REPO / "scripts/systemd/hyxlab-qa.service").read_text()
    posts = [ln for ln in unit.splitlines() if ln.startswith("ExecStopPost=")]
    assert len(posts) == 2
    assert posts[0].endswith("scripts/cgmem.sh"), "the cgroup read must come first"
    # `-`: a failed cleanup never changes the unit's result.
    assert posts[1].startswith("ExecStopPost=-/")
    assert posts[1].endswith("-m collector.qa --clear-snapshot")
    start = next(ln for ln in unit.splitlines() if ln.startswith("ExecStart="))
    assert posts[1].split()[0].removeprefix("ExecStopPost=-") == start.split()[0].removeprefix(
        "ExecStart="
    ), "the stop-post must run the same interpreter as the run"
