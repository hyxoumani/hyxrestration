"""Every write to the shared archive is enumerated here, with its lock.

`tests/test_connect_discipline.py` (EXP-1369) closed the question of who
ATTACHES the archive and how. It does not answer the other half: who
holds `data/writer.lock` while writing. That half was a rule stated in a
sentence — H1 of the 2026-07-11 deep review, "one rule for everybody:
all writers touch the DB only in open -> write -> close bursts", written
into `collector.sweep.writer_burst`'s docstring with an informal roll
call ("which poly_sweep, trades_backfill and signals already follow").

A roll call in prose is not an enumeration. `collector.backfill`
predates the rule, was never in the roll call, and held a read-write
`Store` on the live archive across every REST call of a multi-hour run
while taking the flock at NO point (EXP-1370). That is the H1 shape
verbatim — the one that dropped 421 of 3,706 capture cycles — with the
lock omitted on top, which is strictly worse than holding it too long: a
concurrent `collect` cycle WINS the advisory lock, then collides on
DuckDB's file lock, so the hole is not even recorded as a skip.
`hyxlab.migrate.main` had the same shape (schema writes, no lock).

So the enumeration is written down, and the dispositions that CAN be
checked mechanically are checked rather than trusted:

  BURST   — the write is lexically inside `with writer_burst(...)`.
            Verified against the AST.
  FLOCK   — the enclosing function takes `fcntl.LOCK_EX` on the writer
            lock itself. Verified against the AST.
  CALLER  — the function takes an already-open `store` parameter and
            opens nothing; the lock is its caller's job. NOT verifiable
            from this function alone, so the reason must NAME the caller
            and that caller must itself be BURST or FLOCK here.

The load-bearing invariant, and the one `backfill` broke, is
`test_a_writer_that_opens_the_archive_itself_must_hold_the_lock`: a
function that both opens the archive read-write AND writes to it must be
BURST or FLOCK. CALLER is available only to functions that open nothing.

Set equality both directions, as in the connect enumeration: a new write
site reddens, and a removed one reddens too.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ("collector", "simulator", "strategies", "hyxlab")
STORE_SRC = ROOT / "hyxlab" / "store.py"

#: `relpath::qualname` -> (disposition, why).
ALLOWED: dict[str, tuple[str, str]] = {
    # -- BURST: open -> write -> close, HTTP outside the lock -------------
    "collector/backfill.py::backfill_kalshi_series": (
        "BURST",
        "EXP-1370; flushes past sweep.FLUSH_ROWS so no single burst carries"
        " a whole series, as KXBTC's ~21 min burst did",
    ),
    "collector/backfill.py::backfill_iem": ("BURST", "EXP-1370; one burst per station"),
    "collector/breadth.py::collect_breadth_once": ("BURST", "reuses sweep.writer_burst"),
    "collector/reconcile.py::reconcile": (
        "BURST",
        "a batch that resolved to nothing takes no lock at all",
    ),
    "collector/sweep.py::sweep_series": ("BURST", "the H1 fix itself (2026-08-02)"),
    "collector/sweep.py::run_sweep": ("BURST", "per-series error rows"),
    # -- FLOCK: takes the writer lock in the writing function -------------
    "collector/poly_sweep.py::_flush": ("FLOCK", "the ~7h sweep's short write bursts"),
    "collector/trades_backfill.py::_flush": ("FLOCK", "short write burst per market batch"),
    "collector/signals.py::main": ("FLOCK", "one burst per pull, after all HTTP"),
    # -- CALLER: takes an open store; the lock belongs to the caller ------
    "collector/collect.py::write_cycle": (
        "CALLER",
        "collector/collect.py::main holds it (EXP-957: fetch first, lock second)",
    ),
    "collector/sweep.py::refresh_series": (
        "CALLER",
        "collector/sweep.py::run_sweep wraps it in a burst and fetches outside it",
    ),
    "collector/sweep.py::sweep_series.flush_buffers": (
        "CALLER",
        "collector/sweep.py::sweep_series passes the burst's store",
    ),
    "hyxlab/migrate.py::migrate": (
        "CALLER",
        "hyxlab/migrate.py::main holds it (EXP-1370); the tests call it on their own db",
    ),
}

DISPOSITIONS = ("BURST", "FLOCK", "CALLER")


# ---------------------------------------------------------------------------
# What counts as a write
# ---------------------------------------------------------------------------


def archive_mutators() -> set[str]:
    """`Store` methods that write. Derived from the source, not listed:
    a new mutator must not be able to enter the codebase invisible to
    this guard just because nobody remembered to add it here."""
    out: set[str] = set()
    for cls in ast.parse(STORE_SRC.read_text()).body:
        if not (isinstance(cls, ast.ClassDef) and cls.name == "Store"):
            continue
        for fn in cls.body:
            if not isinstance(fn, ast.FunctionDef) or fn.name == "__init__":
                continue  # __init__ is an ATTACH; test_connect_discipline owns it
            for n in ast.walk(fn):
                if (
                    isinstance(n, ast.Constant)
                    and isinstance(n.value, str)
                    and n.value.strip()
                    .upper()
                    .startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER"))
                ) or (
                    isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "insert_new"
                ):
                    out.add(fn.name)
    return out


def _lock_ex(node: ast.AST) -> bool:
    """Does this subtree take an exclusive flock?"""
    return any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "flock"
        and any(isinstance(a, ast.Attribute) and a.attr == "LOCK_EX" for a in ast.walk(n))
        for n in ast.walk(node)
    )


def _opens_for_write(node: ast.AST) -> bool:
    """Does this function attach the archive read-write itself? `Store(p)`
    or `open_retry(p)` with no literal `read_only=True`."""
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
        if name not in ("Store", "open_retry", "connect"):
            continue
        ro = any(
            k.arg == "read_only" and isinstance(k.value, ast.Constant) and k.value.value is True
            for k in n.keywords
        )
        if len(n.args) > 1 and isinstance(n.args[1], ast.Constant) and n.args[1].value is True:
            ro = True
        if not ro:
            return True
    return False


def _find_function(rel: str, qualname: str) -> ast.FunctionDef | None:
    """Resolve a `relpath::qualname` back to its def, nested names included."""
    node: ast.AST = ast.parse((ROOT / rel).read_text())
    for part in qualname.split("."):
        node = next(
            (
                c
                for c in ast.iter_child_nodes(node)
                if isinstance(c, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
                and c.name == part
            ),
            None,
        )
        if node is None:
            return None
    return node if isinstance(node, ast.FunctionDef) else None


#: The three ways a function can come to hold `data/writer.lock`. Raw
#: `fcntl.LOCK_EX` is the third and is handled by `_lock_ex`.
LOCK_HELPERS = ("writer_burst", "acquire_writer_lock")


def _holds_the_lock(rel: str, qualname: str) -> bool:
    """Does this function take the writer lock, by any of the three
    mechanisms in the repo? `collect.main` uses neither raw flock nor the
    burst helper — it calls `acquire_writer_lock`, which is where its
    lock-wait budget and its skip record live (EXP-957)."""
    fn = _find_function(rel, qualname)
    if fn is None:
        return False
    if _lock_ex(fn):
        return True
    return any(
        isinstance(n, ast.Call)
        and (n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", ""))
        in LOCK_HELPERS
        for n in ast.walk(fn)
    )


def write_sites() -> dict[str, dict]:
    """`relpath::qualname` -> facts about how that function writes."""
    mutators = archive_mutators()
    sites: dict[str, dict] = {}
    for pkg in PACKAGES:
        for path in sorted((ROOT / pkg).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if rel == "hyxlab/store.py":
                continue  # the mutators' own bodies
            tree = ast.parse(path.read_text())
            parents = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in mutators
                ):
                    continue
                chain: list[str] = []
                fn: ast.FunctionDef | None = None
                burst = False
                cur: ast.AST = node
                while cur in parents:
                    cur = parents[cur]
                    if isinstance(cur, ast.With) and any(
                        isinstance(i.context_expr, ast.Call)
                        and (
                            i.context_expr.func.attr
                            if isinstance(i.context_expr.func, ast.Attribute)
                            else getattr(i.context_expr.func, "id", "")
                        )
                        == "writer_burst"
                        for i in cur.items
                    ):
                        burst = True
                    if isinstance(cur, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                        chain.append(cur.name)
                        if fn is None and not isinstance(cur, ast.ClassDef):
                            fn = cur
                key = f"{rel}::{'.'.join(reversed(chain))}"
                site = sites.setdefault(
                    key,
                    {
                        # ANDed across every write in the function, never
                        # ORed: mutation-testing this guard showed that one
                        # burst-wrapped write otherwise launders every
                        # unlocked write beside it in the same function,
                        # which is exactly the shape backfill had.
                        "burst": True,
                        "flock": fn is not None and _lock_ex(fn),
                        "opens_rw": fn is not None and _opens_for_write(fn),
                        "takes_store": fn is not None
                        and any(a.arg == "store" for a in fn.args.args),
                        "line": node.lineno,
                    },
                )
                site["burst"] = site["burst"] and burst
    return sites


# ---------------------------------------------------------------------------
# The enumeration
# ---------------------------------------------------------------------------


def test_every_archive_write_is_enumerated():
    found = set(write_sites())
    new = found - set(ALLOWED)
    assert not new, (
        "archive write with no written-down lock disposition: "
        + ", ".join(sorted(new))
        + " — wrap it in collector.sweep.writer_burst, or add it to ALLOWED"
        " with BURST/FLOCK/CALLER and the reason"
    )
    gone = set(ALLOWED) - found
    assert not gone, (
        "ALLOWED names write sites that no longer exist: "
        + ", ".join(sorted(gone))
        + " — delete the entries so the enumeration stays honest"
    )


def test_every_disposition_is_one_of_the_three():
    bad = {k: d for k, (d, _) in ALLOWED.items() if d not in DISPOSITIONS}
    assert not bad, bad
    thin = {k: why for k, (_, why) in ALLOWED.items() if len(why) < 10}
    assert not thin, f"a disposition without a real reason is a rubber stamp: {thin}"


def test_burst_and_flock_claims_are_true_of_the_source():
    """The two checkable dispositions are CHECKED. An allowlist whose
    entries are only assertions about the code decays into a comment."""
    sites = write_sites()
    for key, (disp, _) in ALLOWED.items():
        facts = sites[key]
        if disp == "BURST":
            assert facts["burst"], (
                f"{key} is labelled BURST but at least one of its writes is"
                " outside writer_burst"
            )
        elif disp == "FLOCK":
            assert facts["flock"], f"{key} is labelled FLOCK but never takes fcntl.LOCK_EX"


def test_a_caller_disposition_names_a_caller_that_holds_the_lock():
    """CALLER is the only disposition this file cannot verify locally, so
    it is the only one that can be used to hide an unlocked write. Two
    guards: the function must open nothing itself, and the caller it
    names must appear here as BURST or FLOCK."""
    sites = write_sites()
    for key, (disp, why) in ALLOWED.items():
        if disp != "CALLER":
            continue
        assert not sites[key]["opens_rw"], (
            f"{key} claims its caller holds the lock, but it opens the archive"
            " read-write itself — that open IS the lock"
        )
        assert sites[key]["takes_store"], f"{key} is labelled CALLER but takes no `store` parameter"
        # The named caller is resolved in the SOURCE, not looked up in this
        # allowlist: `collect.main` and `migrate.main` hold the lock and
        # write nothing themselves, so they are not write sites at all.
        named = re.findall(r"[\w/]+\.py::[\w.]+", why)
        assert named, f"{key}: a CALLER reason must name the caller that holds the lock, got {why!r}"
        for caller in named:
            rel, _, qual = caller.partition("::")
            assert _find_function(rel, qual) is not None, (
                f"{key} names a caller that does not exist: {caller}"
            )
            assert _holds_the_lock(rel, qual), (
                f"{key} defers to {caller}, which takes neither writer_burst"
                " nor an exclusive flock — the write is unlocked after all"
            )


def test_a_writer_that_opens_the_archive_itself_must_hold_the_lock():
    """THE invariant, and the one collector.backfill broke for months: if
    a function opens the archive read-write and writes to it, the lock is
    its own responsibility and nobody else's."""
    offenders = [
        k
        for k, f in write_sites().items()
        if f["opens_rw"] and not (f["burst"] or f["flock"])
    ]
    assert not offenders, (
        "opens the archive read-write and writes with no writer lock: "
        + ", ".join(sorted(offenders))
        + " — a concurrent collect cycle wins the flock, then collides on"
        " DuckDB's file lock and loses a 5-min capture with no skip record"
    )


def test_backfill_does_not_hold_the_archive_across_its_rest_calls():
    """EXP-1370 by name, so a revert reads as what it is rather than as an
    allowlist edit. `main` must not open the archive read-write at all:
    its fetch loops run for hours between writes."""
    src = ast.parse((ROOT / "collector" / "backfill.py").read_text())
    main = next(
        n for n in src.body if isinstance(n, ast.FunctionDef) and n.name == "main"
    )
    assert not _opens_for_write(main), (
        "collector.backfill.main holds a read-write archive connection across"
        " every REST call of a multi-hour run — H1 of the 2026-07-11 review"
    )
    for fn_name in ("backfill_kalshi_series", "backfill_iem"):
        fn = next(n for n in src.body if isinstance(n, ast.FunctionDef) and n.name == fn_name)
        assert not any(a.arg == "store" for a in fn.args.args), (
            f"{fn_name} takes a Store — an open Store is a held file lock, and"
            " this function fetches for hours. It must take a path."
        )


# ---------------------------------------------------------------------------
# The guard must be able to see
# ---------------------------------------------------------------------------


def test_the_mutator_set_is_derived_and_complete():
    """Pinned so a Store method that starts writing cannot slip past the
    scanner, and so a rename that empties the set reddens instead of
    silently making every write site invisible."""
    found = archive_mutators()
    assert found == {
        "insert_breadth_snapshots",
        "insert_candles",
        "insert_forecasts",
        "insert_new",
        "insert_news",
        "insert_poly_prices",
        "insert_poly_stats",
        "insert_snapshots",
        "insert_trades",
        "insert_vintages",
        "log_sweep",
        "mark_trades_swept",
        "set_schema_version",
        "set_watermark",
        "upsert_markets",
        "upsert_observations",
        "upsert_series",
    }, sorted(found)


def test_the_classifier_can_actually_see_an_unlocked_write():
    """A guard that matches nothing passes forever. Pin the three facts the
    dispositions rest on, in both directions, on synthetic sources."""
    import textwrap

    def facts(src: str) -> dict:
        fn = ast.parse(textwrap.dedent(src)).body[0]
        parents = {c: n for n in ast.walk(fn) for c in ast.iter_child_nodes(n)}
        burst = True  # ANDed, exactly as write_sites does it
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr != "insert_candles":
                continue
            cur: ast.AST = node
            this = False
            while cur in parents:
                cur = parents[cur]
                if isinstance(cur, ast.With) and any(
                    isinstance(i.context_expr, ast.Call)
                    and getattr(i.context_expr.func, "id", "") == "writer_burst"
                    for i in cur.items
                ):
                    this = True
            burst = burst and this
        return {
            "burst": burst,
            "flock": _lock_ex(fn),
            "opens_rw": _opens_for_write(fn),
        }

    assert facts("""
        def f(db):
            with writer_burst(db) as store:
                store.insert_candles(rows)
    """) == {"burst": True, "flock": False, "opens_rw": False}

    assert facts("""
        def f(db):
            with open(LOCK_FILE, "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                store = open_retry(db)
                store.insert_candles(rows)
    """) == {"burst": False, "flock": True, "opens_rw": True}

    # The defect shape: opens read-write, writes, no lock of any kind.
    assert facts("""
        def f(db):
            store = Store(db)
            store.insert_candles(rows)
    """) == {"burst": False, "flock": False, "opens_rw": True}

    # A shared (non-exclusive) flock must NOT read as holding the lock.
    assert facts("""
        def f(db):
            fcntl.flock(lock, fcntl.LOCK_SH)
            store.insert_candles(rows)
    """) == {"burst": False, "flock": False, "opens_rw": False}

    # THE laundering case, found by mutation-testing this guard: a function
    # with one burst-wrapped write and one bare write beside it must NOT
    # read as BURST. Under the ORed version it did, and the original
    # `backfill` defect survived its own regression test.
    assert facts("""
        def f(db):
            store = Store(db)
            store.insert_candles(a)
            with writer_burst(db) as s2:
                s2.insert_candles(b)
    """) == {"burst": False, "flock": False, "opens_rw": True}

    # A read-only open is not a write attach, positionally or by keyword.
    assert facts("""
        def f(db):
            store = open_retry(db, read_only=True)
            store.insert_candles(rows)
    """) == {"burst": False, "flock": False, "opens_rw": False}
    assert facts("""
        def f(db):
            store = Store(db, True)
            store.insert_candles(rows)
    """) == {"burst": False, "flock": False, "opens_rw": False}
