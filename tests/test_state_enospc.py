"""A small state file meeting a REAL full filesystem (mistakes #206).

`Path.write_text` truncates, then writes. Rehearsed 2026-11-17 in a private
mount namespace on a 4 MB tmpfs: a rewrite that fits the block its own
truncate freed succeeds (so the naive rehearsal reads clean), and one that
needs a single block more leaves torn JSON -- which `qa._load_state` reads as
`{}`, every section clock and the prior run gone, in silence. On a
copy-on-write filesystem every rewrite is the second case (not measured).
The record must survive, and the run must say the update was lost.
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
from collector import qa
from hyxlab.atomicio import write_text_atomic

now = datetime(2026, 11, 17, tzinfo=UTC)
# A record just under one 4096-byte block.
state = {f"section-{i:02d}": {"first_seen": now.isoformat(), "last_ok": now.isoformat()}
         for i in range(34)}
# Pad to exactly 4090 bytes, so the first new section crosses the boundary.
state["section-00"]["pad"] = ""
state["section-00"]["pad"] = "p" * (4090 - len(json.dumps(state, indent=1, sort_keys=True)))
qa._save_state(state)
res = {"before_b": qa.STATE.stat().st_size, "before_n": len(qa._load_state())}
doc = Path("doc.json"); doc.write_text("x" * 4000)

fd = os.open("ballast", os.O_WRONLY | os.O_CREAT)
try:
    while True:
        os.write(fd, b"\0" * 4096)
except OSError as exc:
    res["filled"] = exc.errno
os.close(fd)

# Vacuity guard: a file that needs a NEW block cannot be written here.
naive = Path("naive.json")
try:
    naive.write_text("y")
    res["naive_create"] = "ok"
except OSError as exc:
    res["naive_create"] = exc.errno

for i in range(34, 40):  # grows the record past the block boundary
    qa._note_seen(f"section-{i:02d}", now)
res["after_b"] = qa.STATE.stat().st_size
res["after_n"] = len(qa._load_state())
res["after_text_same"] = json.loads(qa.STATE.read_text()) == state

try:
    write_text_atomic(doc, "z" * 9000)
    res["doc"] = "ok"
except OSError as exc:
    res["doc"] = exc.errno
res["doc_text_same"] = doc.read_text() == "x" * 4000
res["tmp_left"] = sorted(p.name for p in Path(".").rglob(".*.tmp-*"))

os.unlink("ballast")  # space returns
qa._note_seen("section-34", now)
res["end_n"] = len(qa._load_state())
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


def test_a_full_disk_leaves_the_qa_record_whole_and_says_so(tmp_path):
    res, stdout = _run_on_full_fs(tmp_path)

    # Vacuity guards: the filesystem really filled, a new file cannot be
    # written, and the record sat just under one block before the update.
    assert res["filled"] == 28
    assert res["naive_create"] == 28
    assert res["before_n"] == 34 and res["before_b"] == 4090

    # Full: the update is lost, the record is not -- byte for byte the 34
    # sections it held, where truncate-then-write left 4096 torn bytes that
    # load as {} (and the five sections after it then rebuilt a record of 5).
    assert res["after_n"] == 34
    assert res["after_b"] == res["before_b"]
    assert res["after_text_same"] is True

    # ...and the run says so, once, not six times.
    warnings = [ln for ln in stdout.splitlines() if ln.startswith("WARNING qa record unwritable")]
    assert len(warnings) == 1, stdout
    assert "left as it was" in warnings[0] and "Errno 28" in warnings[0]

    # The helper itself: raises, old file untouched, no temporary left behind.
    assert res["doc"] == 28
    assert res["doc_text_same"] is True
    assert res["tmp_left"] == []

    # Space returns: the next write lands on top of the whole history.
    assert res["end_n"] == 35
