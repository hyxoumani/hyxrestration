"""Shared fixtures.

Two families, both about state a test writes WITHOUT MEANING TO, and both
sharper here than they look: `hyxrestration-stable/data` is a symlink to
this repo's `data/`, so the dev tree's sidecars are production's.
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Temporary files go on DISK, never on the host's RAM-backed /tmp. On this
# box `/tmp` is a tmpfs, so every byte a test leaves there is memory held
# until something deletes it: one full-suite run left 1.5 GiB (the EXP-1373
# shared-spill test alone 587 MiB) in `/tmp/pytest-of-devs`, and pytest keeps
# the last three, while a sibling's suite had parked 8 GiB of the same kind
# -- the standing half of the 2026-11-01 host OOM storm that cost the stream
# 66 GAP lines (mistakes #152). pytest derives its basetemp from
# `tempfile.gettempdir()`, lazily, so pinning the module-level default here
# -- before any fixture asks -- moves `tmp_path`, `mkdtemp` and every
# `TemporaryDirectory` in one place. Set at import, not in a fixture: a
# fixture runs after the factory has already chosen. `tests/test_tmp_on_disk.py`
# proves the pin landed and that the volume under it is not tmpfs.
PYTEST_TMP_ROOT = Path(__file__).resolve().parent.parent / ".pytest-tmp"
PYTEST_TMP_ROOT.mkdir(exist_ok=True)
os.environ["TMPDIR"] = str(PYTEST_TMP_ROOT)
tempfile.tempdir = str(PYTEST_TMP_ROOT)

#: Every cwd-rooted sidecar this repo WRITES, redirected per-test.
#:
#: `hyxrestration-stable/data` is a SYMLINK to this repo's `data/`, so the
#: dev tree's sidecar files are not a copy of production's -- they ARE
#: production's. A test that drives real code with the repo as its cwd
#: does not litter, it forges telemetry, and the instruments read it: the
#: 2026-09-10 spool leak put eight fabricated `spooled`/`drained` events
#: where `qa_collect_spool` would have counted them as evidence that
#: recovery works.
#:
#: This list is not the guard -- `tests/test_sidecar_redirect_coverage.py`
#: DERIVES the set from the source and fails if a new sidecar constant is
#: missing here, because a list maintained by hand cannot fail when
#: someone adds the twelfth one.
SIDECAR_CONSTANTS: tuple[tuple[str, str], ...] = (
    ("collector.collect", "SKIP_LOG"),
    ("collector.health", "RAW_FRAMES_DIR"),
    ("collector.qa", "BACKUP_DIR"),
    ("collector.rawsizes", "LEDGER"),
    ("hyxlab.lagledger", "LEDGER"),
    ("collector.rawsizes", "RAW_FRAMES_DIR"),
    ("collector.qa", "COLLECT_SKIP_LOG"),
    ("collector.qa", "COLLECT_SPOOL_DIR"),
    ("collector.qa", "COLLECT_SPOOL_LOG"),
    ("collector.qa", "RAW_FRAMES_DIR"),
    ("collector.qa", "SIGNALS_FETCH_LOG"),
    ("collector.qa", "STREAM_STALL_LOG"),
    ("collector.reconcile", "SUMMARY_PATH"),
    ("collector.signals", "FETCH_LOG"),
    ("collector.spool", "SPOOL_DIR"),
    ("collector.spool", "SPOOL_LOG"),
    ("collector.streamd", "STALL_LOG"),
    ("collector.venues.kalshi", "RATE_LIMIT_HEADERS_LOG"),
)


@pytest.fixture(autouse=True)
def _redirect_sidecars(tmp_path, monkeypatch):
    """Point every sidecar constant into this test's tmp_path.

    EXP-1333 (hylshi) redirected the first of these by hand -- the kalshi
    client appends the headers of any 429 to `data/rate_limit_headers.jsonl`,
    and tests that simulate 429s through the real code path were poisoning
    the very telemetry the capture exists to collect. Two more leaks
    followed, each found by eye. Redirecting the class costs the same as
    redirecting a member.

    A test that WANTS the real file passes the path explicitly; every
    check and writer here takes one.
    """
    import importlib

    for mod_name, attr in SIDECAR_CONSTANTS:
        mod = importlib.import_module(mod_name)
        original = Path(getattr(mod, attr))
        monkeypatch.setattr(mod, attr, str(tmp_path / "data" / original.name))


_REPO_ROOT = Path(__file__).resolve().parent.parent


#: `collector.qa.STATE` as SHIPPED, captured before any test patches it -- for
#: the tests whose subject is the constant itself (it must stay relative).
import collector.qa as _qa  # noqa: E402

QA_STATE_SHIPPED = _qa.STATE


@pytest.fixture(autouse=True)
def _redirect_qa_state(tmp_path, monkeypatch):
    """`collector.qa.STATE` is cwd-relative ON PURPOSE (each checkout keeps
    its own section-completion record, tests/test_shared_reports.py), so it
    is not a sidecar constant and the fixture above never covered it -- and
    every `qa.check`/`_skip` reached with the repo as cwd stamped the DEV
    tree's real record. Measured 2026-11-20: a full suite run left
    `collect-skips.last_ok` at the suite's own wall clock in
    `reports/qa/sections.json`, written by the eleven `qa_collect_skips`
    tests in tests/test_hyxlab_writer_lock.py that never patched it; a
    by-hand `python -m collector.qa` from this tree would then have printed
    "last verified" from a test. A test that sets its own STATE still wins
    (its setattr runs after this one); tests/test_qa_state_isolation.py
    is the guard.
    """
    from collector import qa

    monkeypatch.setattr(qa, "STATE", tmp_path / "reports" / "qa" / "sections.json")


# --- Instance locks (mistakes #244) ----------------------------------------
# `hyxlab.lockid.instance_lock_path(job, data_dir=DATA_DIR)` binds "data" at
# DEF time: a default argument is a constant no module-attribute redirect can
# reach, and `test_sidecar_redirect_coverage` exempts locks by name. Two
# reconcile tests drive `rec.main` past its single-instance guard with the repo
# as cwd, so every full suite run from the dev tree flocked PRODUCTION's
# `data/reconcile.instance.lock` -- the lock hylshi-archive-reconcile takes
# through the stable symlink -- and overwrote its holder record (EDG-314,
# 2026-11-25). The exemption's reasoning ("a test that took a production lock
# would stall the job, not corrupt a record") is the reason to REDIRECT it: a
# test run must never be able to win, refuse or rewrite a lock a live job
# takes. No test needs the real path -- every exclusion test passes tmp_path
# or chdirs into it (#111) -- so the default is repointed for all of them.
from hyxlab import lockid as _lockid  # noqa: E402

#: The helper and its default AS SHIPPED, captured before any test patches
#: them -- for tests whose subject is the shipped default itself.
INSTANCE_LOCK_PATH_SHIPPED = _lockid.instance_lock_path
INSTANCE_LOCK_DATA_DIR_SHIPPED = _lockid.DATA_DIR


@pytest.fixture(autouse=True)
def _redirect_instance_locks(tmp_path, monkeypatch):
    """Point the instance-lock DEFAULT `data/` into this test's tmp_path.

    Only the default moves: a test that passes its own `data_dir` keeps it.
    An explicit `"data"` is the same production path from the dev tree, so
    it is redirected too. `acquire_instance_lock` and
    `instance_lock_or_reason` look the helper up as a `lockid` global at
    call time, so patching that one name covers every job's `main`. A test
    that imported the helper BY VALUE and chdirs into tmp_path
    (tests/test_tradepass_instance_lock.py) resolves the same file: its
    relative `data/<job>.instance.lock` is `tmp_path/data/<job>...`.
    tests/test_instance_lock_isolation.py is the guard.
    """
    tmp_data = str(tmp_path / "data")

    def _path(job: str, data_dir: str = INSTANCE_LOCK_DATA_DIR_SHIPPED) -> str:
        if str(data_dir) == INSTANCE_LOCK_DATA_DIR_SHIPPED:
            data_dir = tmp_data
        return INSTANCE_LOCK_PATH_SHIPPED(job, data_dir)

    monkeypatch.setattr(_lockid, "instance_lock_path", _path)


# The detector for the class, not the member. Snapshotting `data/` cannot
# work (the live jobs write their own lock holders from the other end of the
# symlink, test_sidecar_redirect_coverage's docstring), so this watches THIS
# process's own file events through an audit hook and attributes each one to
# the running test. It sees in-process Python opens, removes and renames only:
# a subprocess, or DuckDB's C++ I/O, is invisible to it.
CHECKOUT_DATA_ROOTS: tuple[str, ...] = tuple(
    dict.fromkeys((str(_REPO_ROOT / "data"), os.path.realpath(_REPO_ROOT / "data")))
)
LOCK_SUFFIXES: tuple[str, ...] = (".lock", ".lock.holder")
#: (event, path, test) for every lock-shaped write under CHECKOUT_DATA_ROOTS.
#: None outside a test, so collection and session hooks record nothing.
_LOCK_WRITES: dict[str, list | None] = {"current": None}
_LOCK_PATH_EVENTS: dict[str, tuple[int, ...]] = {
    "os.remove": (0,),
    "os.rename": (0, 1),
    "os.truncate": (0,),
}


def lock_write_target(path: object, roots: tuple[str, ...] = CHECKOUT_DATA_ROOTS) -> str | None:
    """The absolute path when `path` is a lock or holder file under `roots`.

    Lexical AND resolved, so `hyxrestration-stable/data/...` (a symlink to this
    `data/`) counts. A relative path resolves against the cwd at the moment of
    the call, which is what the kernel does: a test that chdirs into tmp_path
    and opens `data/x.lock` is outside the checkout and is not a finding.
    """
    if path is None or isinstance(path, int):
        return None
    p = os.fsdecode(path) if isinstance(path, bytes) else str(path)
    if not p.endswith(LOCK_SUFFIXES):
        return None
    ap = os.path.abspath(p)
    rp = os.path.join(os.path.realpath(os.path.dirname(ap)), os.path.basename(ap))
    for cand in (ap, rp):
        if any(cand.startswith(r + os.sep) for r in roots):
            return cand
    return None


def _is_write_open(mode: object, flags: object) -> bool:
    if isinstance(mode, str) and mode:
        return any(c in mode for c in "wax+")
    if isinstance(flags, int) and flags >= 0:
        return bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC))
    return False


def _lock_audit_hook(event: str, args: tuple) -> None:
    rec = _LOCK_WRITES["current"]
    if rec is None:
        return
    try:
        if event == "open":
            path, mode, flags = args
            if _is_write_open(mode, flags) and (hit := lock_write_target(path)):
                rec.append((event, hit))
        elif event in _LOCK_PATH_EVENTS:
            # A bare name relative to a dir_fd is not relative to the cwd.
            dir_fd = args[-1] if len(args) > 1 and isinstance(args[-1], int) else None
            for i in _LOCK_PATH_EVENTS[event]:
                target = args[i] if i < len(args) else None
                if target is None or isinstance(target, int):
                    continue
                if dir_fd is not None and dir_fd >= 0 and not os.path.isabs(os.fsdecode(target)):
                    continue
                if hit := lock_write_target(target):
                    rec.append((event, hit))
    except Exception:  # noqa: BLE001 -- an audit hook must never break the run
        pass


sys.addaudithook(_lock_audit_hook)


@pytest.fixture(autouse=True)
def _no_lock_write_under_checkout_data():
    """Fail the test that wrote, removed or renamed a `*.lock`/`*.lock.holder`
    under the checkout's `data/` -- per test, so the failure names it."""
    seen: list = []
    _LOCK_WRITES["current"] = seen
    try:
        yield
    finally:
        _LOCK_WRITES["current"] = None
    assert not seen, (
        f"this test wrote lock state under the checkout's data/: {sorted(set(seen))}."
        " From the dev tree that is PRODUCTION's lock (mistakes #244): pass a"
        " tmp_path data_dir, patch the module's LOCK_FILE, or chdir(tmp_path)."
    )


@pytest.fixture(autouse=True)
def _raw_frames_see_a_roomy_disk(monkeypatch):
    """`RawFrameWriter` discards frames while the REAL disk under its root is
    below `DISK_YIELD_BYTES` (mistakes #227), and every test's root is a
    tmp_path on the box's own filesystem -- which was 16 GB from full the
    day the floor shipped. Without this the raw-frame tests turn red on a
    full disk for a reason none of them is about. A test of the floor
    injects `free_bytes` and is untouched; tests/test_rawframes_disk_yield.py
    reads the real probe through a path of its own."""
    from collector import rawframes

    monkeypatch.setattr(rawframes, "fs_free_bytes", lambda root: 1 << 50)


@pytest.fixture(autouse=True)
def _kalshi_pacer_off():
    """`collect.main()` switches the Kalshi request pacer on for ITS process
    (`kalshi.set_pace`), and that is module state: a test that drives the
    collector leaves the floor armed for every later test in the session,
    which turned a `[3.0]` Retry-After assertion in test_hyxlab_breadth
    into `[0.124]` on the first full run (2026-10-23). Off before and after
    every test, counters and clock cleared."""
    from collector.venues import kalshi

    def _off():
        kalshi.set_pace(0.0)
        kalshi.reset_pace_stats()
        kalshi._PACE_LAST_MONO = None

    _off()
    yield
    _off()


@pytest.fixture(autouse=True)
def _no_cwd_rooted_duckdb_scratch():
    """Same family as the 429 sink above: cwd-rooted state written into
    the DEV repo, by a test that thought it touched nothing.

    A RELATIVE db path handed to the connect chokepoint creates
    `<db>.tmp/pid-<pid>` and its owner lock NEXT TO THE DATABASE, and a
    relative name resolves against the cwd — the repo root. It appears
    even when `duckdb.connect` is monkeypatched away, because
    `store.private_spill` builds the directory via
    `scratch.duck_scratch_dir` BEFORE the connection is ever used and
    swallows everything downstream: a test that never opens a real
    database still leaves a live owner lock behind. Measured 2026-09-09:
    a full suite run left `x.duckdb.tmp/` untracked in the repo root.

    Checked per-test rather than once at the end so the failure names
    the test that did it — a leak found after the suite is a bisect.
    """
    before = {p.name for p in _REPO_ROOT.glob("*.duckdb.tmp")}
    yield
    leaked = sorted({p.name for p in _REPO_ROOT.glob("*.duckdb.tmp")} - before)
    assert not leaked, (
        f"cwd-rooted duckdb scratch left in the repo root: {leaked}."
        " Pass an absolute path (tmp_path) to any connect helper."
    )


# --- Verdict reachability -------------------------------------------------
# A QA check that is printed, on data it really reads, can still have a FAIL
# arm nothing reaches (mistakes #85). The recorder goes in at conftest import
# so it is around `collector.qa.check` for every test in the session, and the
# verdict is reported from `pytest_sessionfinish`, where the evidence is
# finally complete. See tests/verdict_audit.py.
from tests import verdict_audit  # noqa: E402

verdict_audit.install()


def pytest_sessionfinish(session, exitstatus):
    verdict_audit.report(session, exitstatus)


# --- Write budget ---------------------------------------------------------
# The suite is a tenant of the disk the stream daemon flushes to, and its
# bytes were never counted: measured per test from `/proc/self/io` on
# 2026-11-27, one run wrote 91.6 GB in 473 s, 86.3 GB of it from ONE file
# whose fixtures spelled out GB-scale files of zeros for a reader that only
# `stat`s them (mistakes #252). Alone in a quiet hour those bytes cost the
# host 0.5 s of IO stall; the 11-27 08Z run shared a sibling's nightly spill
# and three stream flushes of 16-30 s. The largest honest writer is the EXP-1373
# shared-spill test at 0.74 GB, so the budget is ~2.7x that; a fixture that
# needs a SIZE asks for a hole (`truncate`), not for the bytes.
WRITE_BUDGET_BYTES = 2 * 1024**3


def proc_write_bytes() -> int | None:
    """Bytes this process (and the children it has reaped) caused to be sent
    to storage, from the kernel's own account; None where it is unreadable."""
    try:
        for line in Path("/proc/self/io").read_text().splitlines():
            if line.startswith("write_bytes:"):
                return int(line.split(":")[1])
    except (OSError, ValueError):
        pass
    return None


def write_budget_breach(before: int | None, after: int | None, budget: int) -> str | None:
    if before is None or after is None or after - before <= budget:
        return None
    return (
        f"this test wrote {(after - before) / 1e9:.2f} GB to disk against a budget of"
        f" {budget / 1e9:.2f} GB (tests/conftest.py WRITE_BUDGET_BYTES): the suite shares"
        " the disk with the stream daemon's flush -- size a fixture file with truncate()"
    )


@pytest.fixture(autouse=True)
def _write_budget():
    before = proc_write_bytes()
    yield
    breach = write_budget_breach(before, proc_write_bytes(), WRITE_BUDGET_BYTES)
    if breach:
        pytest.fail(breach)


@pytest.fixture(autouse=True)
def _venues_reachable(monkeypatch):
    """`netwait.OutagePause` probes the venue (a TCP handshake) when a sweep's
    unit of work raises a transport error, and waits up to 15 min when the
    probe fails. A sweep test that simulates such an error must not open a
    socket to a real venue, nor sleep through a real outage on the dev box:
    reachable by default = the pre-pause behaviour. A test of the pause
    injects its own probe."""
    from collector import netwait

    monkeypatch.setattr(netwait, "_reachable", lambda host, timeout_s=5.0: True)
