"""The collector's sidecar ledgers and QA's free-space series meeting a REAL
full filesystem (mistakes #207).

Rehearsed 2026-11-17 in a private mount namespace on a 4 MB tmpfs, with the
shared disk ~1.5 d from its first ENOSPC. Three appends that record an event
ran ahead of, or inside, the work they describe, and each raised `OSError 28`:

* `collect.record_skip` is called BEFORE the spool attempt: the rows in hand
  were never offered to the spool, and the skip line and exit 75 became a
  traceback;
* `spool.note` runs after the payload is renamed into place (a refusal reads
  as "spool failed, cycle LOST" about a cycle that is on disk) and after a
  drained file is unlinked (a refusal aborts the run before the live cycle);
* `qa._record_free` runs inside the disk check ahead of its verdict: the
  alarm for a full disk died of the full disk, with every later section.

An instrument that can abort the write it observes trades a data hole for a
bigger one (`hyxlab.lockid.note_holder`'s rule, reaching its neighbours).
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
import json, os, sys
from datetime import UTC, datetime
from pathlib import Path
os.chdir(sys.argv[1])
from collector import collect, qa, spool
from hyxlab.models import Snapshot

ts = datetime(2026, 11, 17, 14, 0, tzinfo=UTC)
EMPTY = dict(infos=[], poly_snaps=[], forecasts=[], sources=[])
res = {}

def guarded(name, fn):
    try:
        fn()
        res[name] = "returned"
    except OSError as exc:
        res[name] = f"RAISED {exc.errno}"

fd = os.open("ballast", os.O_WRONLY | os.O_CREAT)
try:
    while True:
        os.write(fd, b"\0" * 4096)
except OSError as exc:
    res["filled"] = exc.errno
os.close(fd)

# Vacuity guard: a file that needs a new block cannot be written here.
try:
    Path("naive.json").write_text("y")
    res["naive_create"] = "ok"
except OSError as exc:
    res["naive_create"] = exc.errno

cyc = collect.Cycle(ts=ts, kalshi_snaps=[Snapshot(venue="kalshi", market_id="LIVE", ts=ts)])
guarded("skip", lambda: collect._skip_cycle(cyc, "writer lock held", 1.0, None))
guarded("note", lambda: spool.note("drained", file="x"))
guarded("free", lambda: qa._record_free(Path("data/free.jsonl"), ts, 1))
res["partials"] = sorted(p.name for p in Path(".").rglob("*.partial"))
res["spooled_full"] = len(spool.spooled())

os.unlink("ballast")  # space returns: the same calls now land
collect._skip_cycle(cyc, "writer lock held", 1.0, None)
qa._record_free(Path("data/free.jsonl"), ts, 1)
res["spooled_after"] = len(spool.spooled())
res["skips_after"] = len(Path("data/collect_skips.jsonl").read_text().splitlines())
res["spool_log_after"] = len(Path("data/collect_spool.jsonl").read_text().splitlines())
res["free_after"] = len(Path("data/free.jsonl").read_text().splitlines())
print("RESULT " + json.dumps(res))
"""


def _run_on_full_fs(tmp_path: Path) -> tuple[dict, str]:
    if shutil.which("unshare") is None:
        pytest.skip("no unshare(1): cannot build a private filesystem to fill")
    mnt = tmp_path / "mnt"
    mnt.mkdir()
    script = tmp_path / "inner.py"
    script.write_text(INNER)
    cmd = (
        f"mount -t tmpfs -o size=4m tmpfs '{mnt}' || exit 77;"
        f" PYTHONPATH='{REPO}' exec '{sys.executable}' '{script}' '{mnt}'"
    )
    r = subprocess.run(
        ["unshare", "-Urm", "sh", "-c", cmd], capture_output=True, text=True, timeout=120
    )
    if r.returncode == 77 or (r.returncode != 0 and "unshare" in r.stderr):
        pytest.skip(f"unprivileged mount namespace unavailable: {r.stderr.strip()[-200:]}")
    assert r.returncode == 0, r.stderr[-2000:]
    line = next(ln for ln in r.stdout.splitlines() if ln.startswith("RESULT "))
    return json.loads(line.removeprefix("RESULT ")), r.stdout


def test_a_full_disk_refuses_the_ledgers_without_aborting_what_they_record(tmp_path):
    res, stdout = _run_on_full_fs(tmp_path)

    # Vacuity guards: the filesystem really filled and refuses a new block.
    assert res["filled"] == 28
    assert res["naive_create"] == 28

    # Full: no ledger append takes its caller down, and each says so.
    assert res["skip"] == "returned", res
    assert res["note"] == "returned", res
    assert res["free"] == "returned", res
    assert "[collect] WARNING skip ledger unwritable" in stdout
    assert "[spool] WARNING journal unwritable (drained)" in stdout
    assert "[qa] WARNING free-space series unwritable" in stdout
    # The skip still reached the spool attempt and its own line, in order.
    lost = stdout.index("[collect] spool failed, cycle LOST")
    assert stdout.index("WARNING skip ledger unwritable") < lost
    assert lost < stdout.index("[collect] skipped: writer lock held")
    # The refused payload did not stay behind holding blocks.
    assert res["spooled_full"] == 0 and res["partials"] == []

    # Space back: the same calls land, once each.
    assert res["spooled_after"] == 1
    assert res["skips_after"] == 1 and res["free_after"] == 1
    assert res["spool_log_after"] == 1
