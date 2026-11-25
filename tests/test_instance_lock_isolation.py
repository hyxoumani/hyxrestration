"""No test may take, refuse or rewrite a lock under the checkout's `data/`
(mistakes #244).

`hyxrestration-stable/data` is a symlink to the dev tree's `data/`, and
`hyxlab.lockid.instance_lock_path(job, data_dir=DATA_DIR)` binds "data" at
def time. Two reconcile tests drove `rec.main` past its single-instance guard
with the repo as cwd, so every dev-tree suite run flocked production's
`data/reconcile.instance.lock` (the lock hylshi-archive-reconcile takes) and
left a pytest command line as its last holder (EDG-314, 2026-11-25).

conftest closes it two ways, and both are tested here, each with a vacuity
arm so a guard that cannot fail does not pass:

  `_redirect_instance_locks`  repoints the DEFAULT into tmp_path;
  `_lock_audit_hook` +        records this process's lock-shaped writes under
  `_no_lock_write_under_checkout_data`  the checkout's data/ and fails the
                              test that made one.

Nothing here ever writes the checkout's `data/`: each path is checked before
anything is opened, and the hook's live arm is driven on a stand-in root.
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path

from hyxlab import lockid
from tests import conftest

REPO = Path(__file__).resolve().parent.parent


def _under(path: str, root: Path) -> bool:
    return os.path.realpath(path).startswith(os.path.realpath(root) + os.sep)


def test_the_shipped_default_is_unchanged():
    """The redirect is a test-time patch, never a change of the job's path:
    production still resolves `data/<job>.instance.lock` against its cwd."""
    assert conftest.INSTANCE_LOCK_DATA_DIR_SHIPPED == "data" == lockid.DATA_DIR
    shipped = conftest.INSTANCE_LOCK_PATH_SHIPPED
    assert inspect.signature(shipped).parameters["data_dir"].default == "data"
    assert shipped("reconcile") == "data/reconcile.instance.lock"


def test_every_job_default_lands_in_tmp_path(tmp_path):
    for job in ("reconcile", "sweep", "poly_sweep", "trades_backfill", "backfill"):
        path = lockid.instance_lock_path(job)
        assert _under(path, tmp_path), f"{job}: default instance lock left tmp_path: {path}"
        assert not _under(path, REPO / "data")
        # An explicit "data" is the same production path from the dev tree.
        assert _under(lockid.instance_lock_path(job, "data"), tmp_path)


def test_an_explicit_data_dir_still_wins(tmp_path):
    other = tmp_path / "elsewhere"
    assert lockid.instance_lock_path("sweep", str(other)) == str(other / "sweep.instance.lock")


def test_the_job_entry_point_takes_its_lock_in_tmp_path(tmp_path):
    """The path every `main` reaches: `instance_lock_or_reason(job)` with no
    data_dir, as collector/reconcile.py:863 calls it. The path is asserted
    BEFORE the acquire, so the failing arm cannot touch production."""
    assert _under(lockid.instance_lock_path("reconcile"), tmp_path)
    lock, why = lockid.instance_lock_or_reason("reconcile")
    try:
        assert lock is not None and why == ""
        assert _under(lock.name, tmp_path)
        assert Path(lockid.holder_path(lock.name)).exists()
        # Mutual exclusion is untouched: a second take is refused and named.
        again, why = lockid.instance_lock_or_reason("reconcile")
        assert again is None and f"pid={os.getpid()}" in why
    finally:
        lock.close()


def test_the_hook_sees_a_lock_write_on_a_stand_in_root(tmp_path, monkeypatch):
    """Vacuity arm for the detector: on a stand-in `data/` the same events
    the reconcile leak made -- `open(path, "a")` for the flock and a
    `write_text` of the holder -- are recorded, and so are a remove and a
    rename. A read, and a non-lock file, are not."""
    root = tmp_path / "standin-data"
    root.mkdir()
    monkeypatch.setattr(
        conftest.lock_write_target,
        "__defaults__",
        ((str(root), os.path.realpath(root)),),
    )
    seen: list = []
    # Swapped by hand, not by monkeypatch: the per-test fixture owns this slot
    # and its own teardown must find its own (empty) list there.
    outer = conftest._LOCK_WRITES["current"]
    conftest._LOCK_WRITES["current"] = seen
    try:
        lock = root / "reconcile.instance.lock"
        with open(lock, "a"):
            pass
        Path(str(lock) + ".holder").write_text("{}\n")
        (root / "sidecar.jsonl").write_text("x\n")  # not a lock: out of scope
        with open(lock):  # a read is not a write
            pass
        os.rename(str(lock) + ".holder", root / "moved.lock.holder")
        os.remove(lock)
    finally:
        conftest._LOCK_WRITES["current"] = outer

    kinds = [(ev, Path(p).name) for ev, p in seen]
    assert ("open", "reconcile.instance.lock") in kinds
    assert ("open", "reconcile.instance.lock.holder") in kinds
    assert ("os.rename", "moved.lock.holder") in kinds
    assert ("os.remove", "reconcile.instance.lock") in kinds
    assert not any(name == "sidecar.jsonl" for _, name in kinds)
    assert sum(1 for ev, n in kinds if ev == "open" and n == "reconcile.instance.lock") == 1


def test_the_hook_does_not_count_a_chdir_relative_lock(tmp_path, monkeypatch):
    """`monkeypatch.chdir(tmp_path)` then `data/x.lock` (the #111 isolation)
    resolves outside the checkout and is not a finding."""
    monkeypatch.chdir(tmp_path)
    assert conftest.lock_write_target("data/x.instance.lock") is None
    monkeypatch.chdir(REPO)
    assert conftest.lock_write_target("data/x.instance.lock") is not None


def test_the_checkout_roots_include_the_resolved_data_dir():
    """`hyxrestration-stable/data` is a symlink to this `data/`: a path
    reached through either spelling must be attributed."""
    roots = conftest.CHECKOUT_DATA_ROOTS
    assert str(REPO / "data") in roots
    assert os.path.realpath(REPO / "data") in roots
