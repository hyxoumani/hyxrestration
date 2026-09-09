"""Shared fixtures.

EXP-1333 (hylshi): the kalshi venue client appends the headers of any 429 it
observes to `data/rate_limit_headers.jsonl` (relative, cwd-rooted). Several
tests simulate 429s through the real code paths; without redirection they
would write fake header rows into the DEV repo's real sink — poisoning the
very telemetry the capture exists to collect. Redirect it per-test.
"""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _redirect_429_header_sink(tmp_path, monkeypatch):
    from collector.venues import kalshi

    monkeypatch.setattr(
        kalshi, "RATE_LIMIT_HEADERS_LOG", str(tmp_path / "rate_limit_headers.jsonl")
    )


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
