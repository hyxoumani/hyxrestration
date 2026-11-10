"""Shared fixtures.

Two families, both about state a test writes WITHOUT MEANING TO, and both
sharper here than they look: `hyxrestration-stable/data` is a symlink to
this repo's `data/`, so the dev tree's sidecars are production's.
"""

import os
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
