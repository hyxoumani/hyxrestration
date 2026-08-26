"""Which entrypoints must run only one at a time, written down (EXP-1371).

`test_writer_lock_discipline.py` (EXP-1370) answers who holds
`data/writer.lock` while writing; `test_connect_discipline.py` (EXP-1369)
answers who attaches the archive at all. Neither answers the next
question out: **which jobs must not run twice at once.**

Exactly one job guarded itself. `collector.sweep` flocked
`<db>.lock` before its multi-hour run and aborted EX_TEMPFAIL if another
sweep held it; `poly_sweep` (~7h of paced HTTP), `trades_backfill` (15h06m
on 2026-08-03), `backfill` and `reconcile` took nothing. systemd will not
start a second copy of a `.service` that is already active, so the timers
looked like the guard — but every one of these is a CLI that CLAUDE.md
tells an operator to run by hand, and an ad-hoc run racing the timer's
run is outside systemd's guarantee entirely. Two copies of a fetch-paced
worklist pass re-walk the same watermarks, spend the venue rate budget
twice for the same anti-joined rows, and double writer-lock contention
against the 5-min collector — the 2026-08-02 starvation shape, arriving
by a route the writer-lock enumeration cannot see.

The lock is deliberately JOB-scoped (`data/<job>.instance.lock`). The
sweep's old `<db>.lock` was archive-scoped, and any second job adopting
it would have made the 04:15Z poly sweep exclude the 06:10Z incremental
sweep every single day — a starvation bug wearing a safety fix's clothes.
`test_no_two_jobs_share_an_instance_lock` pins that.

As in both enumerations below it, the DERIVED half is derived and the
CLAIMED half is checked:

  derived — a module is a multi-burst archive job when a function that
            writes the archive is reachable from inside a loop. Computed
            from the AST by fixpoint, so a new such job cannot enter
            without reddening this file.
  SELF    — `main` takes an instance lock with a literal job name AND
            exits nonzero when refused. Verified.
  POLLER  — a `while True` cycle loop with `--once`; a second copy costs
            one duplicate cycle, and the writer lock plus its skip record
            already price that. Verified to actually have both.
  SHORT   — no HTTP at all: bounded, idempotent, seconds under the writer
            lock. Verified to import no fetching client, because "short"
            is the claim a fetch-paced job would most like to make.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ("collector", "simulator", "strategies", "hyxlab")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_writer_lock_discipline import archive_mutators  # noqa: E402

#: relpath -> (disposition, why). Every multi-burst archive job in the
#: repo, and what stops a second copy of it from running.
ALLOWED: dict[str, tuple[str, str]] = {
    # -- SELF: holds data/<job>.instance.lock for the whole run ----------
    "collector/sweep.py": ("SELF", "the original instance lock; job-scoped since EXP-1371"),
    "collector/poly_sweep.py": (
        "SELF",
        "~7h paced walk over per-token watermarks (EXP-1371)",
    ),
    "collector/trades_backfill.py": (
        "SELF",
        "unbounded oldest-first worklist; the 08-03 crypto pass ran 15h06m",
    ),
    "collector/backfill.py": ("SELF", "5 series x 50 pages plus a candle call per market"),
    "collector/reconcile.py": ("SELF", "re-repairs the same work order against a blind ledger"),
    # -- POLLER: cycle loop, guarded by the writer lock + skip record ----
    "collector/collect.py": (
        "POLLER",
        "5-min cycle; a duplicate costs one cycle, and `--once` racing the"
        " timer is a documented operator move (EXP-957 records the skip)",
    ),
    "collector/breadth.py": ("POLLER", "same cycle shape, OFF by default"),
    # -- SHORT: no HTTP; idempotent DDL, seconds --------------------------
    "hyxlab/migrate.py": (
        "SHORT",
        "idempotent schema steps under the writer lock; no fetching at all",
    ),
}

DISPOSITIONS = ("SELF", "POLLER", "SHORT")
LOCK_HELPERS = ("instance_lock_or_reason", "acquire_instance_lock")
#: Clients that make a job fetch-paced. A SHORT claim must import none.
FETCH_CLIENTS = ("requests", "httpx", "urllib", "websockets")


# ---------------------------------------------------------------------------
# Derived: which modules write the archive from inside a loop
# ---------------------------------------------------------------------------


def _archive_writers(tree: ast.AST, mutators: set[str]) -> set[str]:
    """Functions in this module that write the archive, directly or by
    calling one that does. Fixpoint, so an extra layer of helper does not
    hide the write."""
    funcs = {
        n.name: n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    writers = {
        name
        for name, fn in funcs.items()
        if any(
            isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr in mutators
            for c in ast.walk(fn)
        )
    }
    changed = True
    while changed:
        changed = False
        for name, fn in funcs.items():
            if name in writers:
                continue
            if any(_called_name(c) in writers for c in ast.walk(fn) if isinstance(c, ast.Call)):
                writers.add(name)
                changed = True
    return writers


def _called_name(call: ast.Call) -> str:
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")


def multiburst_modules() -> dict[str, set[str]]:
    """relpath -> the writing functions reached from a loop."""
    mutators = archive_mutators()
    out: dict[str, set[str]] = {}
    for pkg in PACKAGES:
        for path in sorted((ROOT / pkg).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if rel == "hyxlab/store.py":
                continue  # the mutators' own bodies
            tree = ast.parse(path.read_text())
            writers = _archive_writers(tree, mutators)
            if not writers:
                continue
            looped = {
                _called_name(c)
                for loop in ast.walk(tree)
                if isinstance(loop, ast.For | ast.While | ast.AsyncFor)
                for c in ast.walk(loop)
                if isinstance(c, ast.Call) and _called_name(c) in (writers | mutators)
            }
            if looped:
                out[rel] = looped
    return out


def _main_of(rel: str) -> ast.FunctionDef:
    tree = ast.parse((ROOT / rel).read_text())
    fn = next(
        (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main"), None
    )
    assert fn is not None, f"{rel} is a multi-burst archive job with no main()"
    return fn


def instance_lock_jobs(rel: str) -> set[str]:
    """The literal job names `main` locks. Literal on purpose: a computed
    name cannot be checked for collision with another job's."""
    return {
        c.args[0].value
        for c in ast.walk(_main_of(rel))
        if isinstance(c, ast.Call)
        and _called_name(c) in LOCK_HELPERS
        and c.args
        and isinstance(c.args[0], ast.Constant)
        and isinstance(c.args[0].value, str)
    }


# ---------------------------------------------------------------------------
# The enumeration
# ---------------------------------------------------------------------------


def test_every_multiburst_archive_job_is_enumerated():
    found = set(multiburst_modules())
    new = found - set(ALLOWED)
    assert not new, (
        "writes the archive from inside a loop with no written-down"
        f" single-instance disposition: {sorted(new)} — take"
        " hyxlab.lockid.instance_lock_or_reason, or add it here as"
        " SELF/POLLER/SHORT with the reason"
    )
    gone = set(ALLOWED) - found
    assert not gone, f"ALLOWED names jobs that no longer write in a loop: {sorted(gone)}"


def test_every_disposition_is_one_of_the_three_with_a_reason():
    bad = {k: d for k, (d, _) in ALLOWED.items() if d not in DISPOSITIONS}
    assert not bad, bad
    thin = {k: why for k, (_, why) in ALLOWED.items() if len(why) < 10}
    assert not thin, f"a disposition without a real reason is a rubber stamp: {thin}"


@pytest.mark.parametrize("rel", [k for k, (d, _) in ALLOWED.items() if d == "SELF"])
def test_a_self_job_takes_an_instance_lock_and_refuses_to_run_without_it(rel):
    """Both halves, because either alone is decorative: a job that takes
    no lock is unguarded, and a job that takes one, logs the refusal and
    runs anyway is unguarded WITH a reassuring line in the journal."""
    jobs = instance_lock_jobs(rel)
    assert jobs, (
        f"{rel} is labelled SELF but main() never calls {LOCK_HELPERS} with a"
        " literal job name"
    )
    refusals = [
        n
        for n in ast.walk(_main_of(rel))
        if isinstance(n, ast.If)
        and isinstance(n.test, ast.Compare)
        and isinstance(n.test.ops[0], ast.Is)
        and isinstance(n.test.comparators[0], ast.Constant)
        and n.test.comparators[0].value is None
        and isinstance(n.test.left, ast.Name)
        and n.test.left.id == "lock"
    ]
    assert refusals, f"{rel} takes an instance lock but never branches on being refused"
    exits = [
        c
        for n in refusals
        for c in ast.walk(n)
        if (isinstance(c, ast.Call) and _called_name(c) == "exit")
        or isinstance(c, ast.Return)
    ]
    codes = [
        a.value
        for c in exits
        for a in (c.args if isinstance(c, ast.Call) else [c.value])
        if isinstance(a, ast.Constant)
    ]
    assert 75 in codes, (
        f"{rel} must leave EX_TEMPFAIL (75) when refused, as sweep does — systemd"
        f" records a failed run and the next firing resumes; got {codes}"
    )


def test_no_two_jobs_share_an_instance_lock():
    """The starvation bug this design exists to avoid: one archive-scoped
    lock would make the 04:15Z poly sweep exclude the 06:10Z sweep daily.
    A shared name is a mutual-exclusion decision, never an accident."""
    owner: dict[str, str] = {}
    for rel, (disp, _) in ALLOWED.items():
        if disp != "SELF":
            continue
        for job in instance_lock_jobs(rel):
            assert job not in owner, (
                f"{rel} and {owner[job]} both lock instance name {job!r} — they"
                " would exclude each other; give each job its own name"
            )
            owner[job] = rel


@pytest.mark.parametrize("rel", [k for k, (d, _) in ALLOWED.items() if d == "POLLER"])
def test_a_poller_really_is_a_cycle_loop(rel):
    """POLLER is the escape hatch a worklist job would reach for, so it
    is spent only by a module whose loop is over TIME, not over work."""
    tree = ast.parse((ROOT / rel).read_text())
    main = _main_of(rel)
    assert any(
        isinstance(n, ast.While) and isinstance(n.test, ast.Constant) and n.test.value is True
        for n in ast.walk(main)
    ), f"{rel} is labelled POLLER but main() has no `while True` cycle loop"
    assert any(
        isinstance(n, ast.Constant) and n.value == "--once" for n in ast.walk(tree)
    ), f"{rel} is labelled POLLER but offers no --once, so it is not a cycle job"


@pytest.mark.parametrize("rel", [k for k, (d, _) in ALLOWED.items() if d == "SHORT"])
def test_a_short_job_does_no_fetching(rel):
    """'Short' is exactly the claim a multi-hour fetcher would make, so
    it is checked against the imports rather than believed."""
    tree = ast.parse((ROOT / rel).read_text())
    imported = {
        (n.module or "").split(".")[0]
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom)
    } | {
        a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }
    hits = imported & set(FETCH_CLIENTS)
    assert not hits, (
        f"{rel} is labelled SHORT but imports {sorted(hits)} — a job that"
        " fetches is paced by the network, and pacing is what makes a"
        " duplicate expensive"
    )


def test_the_derived_set_still_sees_the_five_worklist_jobs():
    """Pinned so a refactor that empties the scanner reddens here instead
    of silently making every job's disposition unenforced — the mutator
    set is pinned in the writer-lock enumeration for the same reason."""
    found = multiburst_modules()
    for rel in (
        "collector/sweep.py",
        "collector/poly_sweep.py",
        "collector/trades_backfill.py",
        "collector/backfill.py",
        "collector/reconcile.py",
    ):
        assert found.get(rel), f"{rel} no longer reads as a multi-burst archive job"
