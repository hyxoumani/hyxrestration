"""Temporary files are written to DISK, not to the host's RAM-backed /tmp.

On this box `/tmp` is a tmpfs (see `/proc/mounts`), so a file a test leaves
there is memory: a full-suite run left 1.5 GiB in `/tmp/pytest-of-devs` and
pytest keeps three such runs, beside a sibling suite's 8 GiB of the same --
the standing half of the 2026-11-01 host OOM storm that cost `hyxlab-stream`
66 GAP lines (mistakes #152). `tests/conftest.py` pins `tempfile`'s default
directory to `<repo>/.pytest-tmp` at import; these tests prove the pin landed
where every consumer reads it, and that the volume under it is not RAM.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from collector.health import mount_is_tmpfs
from tests.conftest import PYTEST_TMP_ROOT

MOUNTS = Path("/proc/mounts")


def _mountpoint(path: Path) -> Path:
    p = path.resolve()
    while not os.path.ismount(p):
        p = p.parent
    return p


def test_tempfile_default_directory_is_the_repo_temp_root() -> None:
    assert Path(tempfile.gettempdir()).resolve() == PYTEST_TMP_ROOT.resolve()


def test_tmp_path_lives_under_the_repo_temp_root(tmp_path: Path) -> None:
    assert tmp_path.resolve().is_relative_to(PYTEST_TMP_ROOT.resolve())


def test_mkdtemp_and_tmpdir_env_follow_the_pin() -> None:
    d = Path(tempfile.mkdtemp())
    try:
        assert d.is_relative_to(PYTEST_TMP_ROOT.resolve())
    finally:
        d.rmdir()
    assert Path(os.environ["TMPDIR"]).resolve() == PYTEST_TMP_ROOT.resolve()


@pytest.mark.skipif(not MOUNTS.exists(), reason="no mount table to read")
def test_the_temp_root_is_not_on_a_tmpfs() -> None:
    """The claim that matters: the bytes land on a disk, whatever the path."""
    table = MOUNTS.read_text()
    mp = _mountpoint(PYTEST_TMP_ROOT)
    assert mount_is_tmpfs(table, str(mp)) is False, f"{PYTEST_TMP_ROOT} is on {mp}, a tmpfs"


@pytest.mark.skipif(not MOUNTS.exists(), reason="no mount table to read")
def test_the_default_tmp_would_have_been_ram_here() -> None:
    """Vacuity guard for the rule: it is only worth having on a box where
    `/tmp` IS a tmpfs. If this ever reads False the rule's premise moved and
    the docstring above is stale -- fix the words, keep the pin."""
    kind = mount_is_tmpfs(MOUNTS.read_text(), "/tmp")
    if kind is None:
        pytest.skip("/tmp has no mount of its own here")
    assert kind is True
