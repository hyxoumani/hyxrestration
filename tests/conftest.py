"""Shared fixtures.

Two families, both about state a test writes WITHOUT MEANING TO, and both
sharper here than they look: `hyxrestration-stable/data` is a symlink to
this repo's `data/`, so the dev tree's sidecars are production's.
"""

from pathlib import Path

import pytest

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
    ("collector.qa", "BACKUP_DIR"),
    ("collector.qa", "COLLECT_SKIP_LOG"),
    ("collector.qa", "COLLECT_SPOOL_DIR"),
    ("collector.qa", "COLLECT_SPOOL_LOG"),
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
