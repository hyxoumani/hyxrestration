"""A REAL disk-full, not a mocked one: `StreamStore` on a filesystem that
actually runs out (rehearsed 2026-11-11 with the shared disk 1.9 d from the
kubelet's threshold and nobody having watched a writer meet ENOSPC).

Every other ENOSPC test in this suite raises `OSError(28)` from a patched
call, which proves the handler for the call that was patched. What a full
filesystem does is fail WHICHEVER write comes first -- here the staged CSV in
`_insert_frame`, before DuckDB is asked for anything -- then the overflow
spill inside the flush's own `except`, then the shutdown spills, and it keeps
failing them on every tick. This runs the store inside a private mount
namespace on a 24 MB tmpfs (`unshare -Urm`; RAM-backed and small, so the
/tmp-is-RAM rule is not in play) and reads the archive back from a SECOND
process, which is the only reader that proves the file on disk rather than
this process's instance of it.

Measured the same day and not pinned here, because they are DuckDB's
behaviour rather than ours (docs/wiki/mistakes.md #174): a commit that meets
ENOSPC in the WAL or in the database file raises a clean
`TransactionException`, rolls back whole, and leaves the file readable and
writable once space returns.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

INNER = r"""
import json, os, random, subprocess, sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from hyxlab.streamstore import BookEvent, StreamStore

mnt = Path(sys.argv[1]); db = mnt / "s.duckdb"
rng = random.Random(7); t0 = datetime(2026, 1, 1, tzinfo=UTC); seq = 0; BATCH = 10_000

def mk(n):
    global seq
    out = []
    for _ in range(n):
        seq += 1
        out.append(BookEvent("kalshi", f"KX{rng.getrandbits(64):016x}",
                             t0 + timedelta(microseconds=seq), None, rng.getrandbits(20),
                             seq, "delta", "yes", rng.random(), rng.random() * 1000))
    return out

def outside():
    code = ("import sys;from hyxlab.store import duck_connect;"
            "c=duck_connect(sys.argv[1],read_only=True);"
            "print(list(c.execute('select count(*),count(distinct seq),max(seq)"
            " from book_events').fetchone()))")
    r = subprocess.run([sys.executable, "-c", code, str(db)], capture_output=True, text=True)
    return json.loads(r.stdout) if r.returncode == 0 else r.stderr[-300:]

def sidecar():
    p = mnt / "s.duckdb.spill.jsonl"
    return p.stat().st_size if p.exists() else 0

ballast = mnt / "ballast"
ballast.write_bytes(os.urandom(12 << 20))
store = StreamStore(db)
store.SPILL_CAP = BATCH  # so every full-disk tick also drives the overflow spill
res = {"landed": 0, "errors": []}
for _ in range(400):  # fill through the store's own flush
    store.append_events(mk(BATCH))
    try:
        res["landed"] += store.flush_outcome().rows
    except BaseException as exc:
        res["errors"].append([type(exc).__name__, getattr(exc, "errno", None)])
        break
for _ in range(3):  # the daemon's next flush ticks, still full
    store.append_events(mk(BATCH))
    try:
        res["landed"] += store.flush_outcome().rows
    except BaseException as exc:
        res["errors"].append([type(exc).__name__, getattr(exc, "errno", None)])
res["full"] = {"pending": store.pending, "appended": seq, "sidecar_b": sidecar(),
               "outside": outside()}
boot = subprocess.run(  # a restart at full disk: the constructor runs DDL
    [sys.executable, "-c", "import sys;from hyxlab.streamstore import StreamStore;"
     "StreamStore(sys.argv[1]).adopt_snapshots()", str(db)], capture_output=True, text=True)
res["boot_rc"] = boot.returncode
# 300 kB of room: the shutdown spills (6 MB) now fail PART-WAY, which is the
# case the rewind exists for -- at zero free a write lands nothing at all and
# a missing rewind is invisible (red-verified: it was).
os.truncate(ballast, ballast.stat().st_size - 300_000)
res["shutdown"] = []
for name in ("spill_all", "spill_snapshot"):  # the shutdown paths, still full
    try:
        getattr(store, name)()
        res["shutdown"].append([name, "ok"])
    except OSError as exc:
        res["shutdown"].append([name, exc.errno])
res["after_shutdown"] = {"pending": store.pending, "sidecar_b": sidecar(),
                         "snaps": len(list(mnt.glob("s.duckdb.spill.snap-*")))}
ballast.unlink()  # space returns
res["recovered"] = store.flush_outcome().rows
res["end"] = {"pending": store.pending, "appended": seq, "outside": outside(),
              "csv_left": len(list(mnt.glob(".*.csv")))}
print("RESULT " + json.dumps(res))
"""


def _run_on_full_fs(tmp_path: Path) -> dict:
    if shutil.which("unshare") is None:
        pytest.skip("no unshare(1): cannot build a private filesystem to fill")
    mnt = tmp_path / "mnt"
    mnt.mkdir()
    script = tmp_path / "inner.py"
    script.write_text(INNER)
    cmd = (
        f"mount -t tmpfs -o size=24m tmpfs '{mnt}' || exit 77;"
        f" PYTHONPATH='{REPO}' exec '{sys.executable}' '{script}' '{mnt}'"
    )
    r = subprocess.run(
        ["unshare", "-Urm", "sh", "-c", cmd], capture_output=True, text=True, timeout=300
    )
    if r.returncode == 77 or (r.returncode != 0 and "unshare" in r.stderr):
        pytest.skip(f"unprivileged mount namespace unavailable: {r.stderr.strip()[-200:]}")
    assert r.returncode == 0, r.stderr[-2000:]
    line = next(ln for ln in r.stdout.splitlines() if ln.startswith("RESULT "))
    return json.loads(line.removeprefix("RESULT "))


def test_a_full_disk_costs_the_stream_archive_no_row_and_no_duplicate(tmp_path):
    res = _run_on_full_fs(tmp_path)

    # Vacuity guard: the filesystem really filled, on every one of the four
    # ticks, and it was ENOSPC rather than some other refusal.
    assert res["errors"] == [["OSError", 28]] * 4, res["errors"]
    assert res["landed"] > 0

    # While full: nothing left the buffer, the failed overflow spill left an
    # empty sidecar (at zero free that proves nothing about the rewind -- the
    # part-way case is below), and the archive on disk is exactly the flushes
    # that returned -- read from another process.
    full = res["full"]
    assert full["pending"] == full["appended"] - res["landed"] == 40_000
    assert full["sidecar_b"] == 0
    assert full["outside"] == [res["landed"], res["landed"], res["landed"]]

    # A restart at full disk boots (its DDL is a no-op on an existing schema).
    assert res["boot_rc"] == 0

    # The shutdown paths cannot save the buffer on a full disk -- with 300 kB
    # of room they fail PART-WAY and raise ENOSPC -- and they must not trim
    # it, leave a torn sidecar tail (the rewind; a mutant without it leaves
    # 299,008 bytes here) or a part-written snapshot the next boot would
    # adopt as if it were whole.
    assert res["shutdown"] == [["spill_all", 28], ["spill_snapshot", 28]]
    assert res["after_shutdown"] == {"pending": 40_000, "sidecar_b": 0, "snaps": 0}

    # Space returns: one flush lands the whole backlog, once.
    end = res["end"]
    assert res["recovered"] == 40_000
    assert end["pending"] == 0
    assert end["outside"] == [end["appended"]] * 3
    assert end["csv_left"] == 0
