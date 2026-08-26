"""Who owns the two SIM-SIDE databases, written down (EXP-1372).

The three enumerations below this one all stop at the archive:
`test_connect_discipline.py` (EXP-1369) says who attaches
`hyxlab.duckdb`, `test_writer_lock_discipline.py` (EXP-1370) says who
holds `data/writer.lock` while writing it, and
`test_instance_lock_discipline.py` (EXP-1371) says which archive jobs
must not run twice. `data/hyxstream.duckdb` and `data/hyxshadow.duckdb`
are written by neither `Store` nor the writer lock, so all three are
blind to them, and what stood in for a guard was a claim: the connect
enumeration records "the stream daemon owns hyxstream.duckdb" and "the
daemon owns hyxshadow.duckdb". Nothing made either sentence true.

**THE FILE LOCK EXCLUDES NOTHING BETWEEN BURSTS, MEASURED.** Both
writers open their database, write, and CLOSE it again — `StreamStore.
flush` every ~15 s, `ShadowLedger.persist` once per ~20 s poll — so
DuckDB's exclusive file lock exists only for those milliseconds. On
2026-08-26 two StreamStore writers on one file completed 20 and 15
flushes with ZERO declines between them (`test_the_file_lock_excludes_
nothing_between_bursts` reproduces it). A second `streamd` is not
refused; it INTERLEAVES. `book_events` and `stream_trades` have no key,
no anti-join and no dedupe anywhere in the store — and a duplicated
`delta` is not a wasted row, it is a book that replays wrong for every
consumer downstream. The data is unrecoverable (neither venue serves
historical books), so there is nothing to repair afterwards.

systemd looks like the guard again and again is not: `hyxlab-stream` and
`hyxlab-shadow` are `Restart=always` daemons, but both are plain CLIs an
operator (or an agent) can start by hand from either worktree, and an
ad-hoc copy racing the unit is outside systemd's guarantee entirely —
the same route EXP-1371 found into the archive jobs.

**THE LOCK IS FILE-SCOPED, NOT JOB-SCOPED, AND THAT IS THE DESIGN
DIFFERENCE FROM THE RUNG BELOW.** Instance locks are job-scoped because
two archive jobs legitimately share one archive and must not exclude each
other. Here the resource IS the file: two writers of one database must
exclude each other, and a run pointed at another path (a test tmpdir, a
side experiment on a copied ledger) must not be blocked by the daemon.
So the lock is `<db>.owner.lock` and `test_two_databases_do_not_exclude_
each_other` pins it.

The halves, as in every enumeration below this one:

  derived — a module writes an owned database when it opens a non-archive
            DuckDB file read-write. Computed from the AST, so a third
            owned database cannot appear unenumerated.
  OWNER   — an entrypoint that constructs a writer: `main` must take the
            owner lock for the file it is about to open AND leave 75 when
            refused. Both halves verified, and the refusal is verified by
            RUNNING main, not by reading it.
  LIBRARY — a writer class with no `main`; its owner is whichever
            entrypoint constructs it. Verified to have no entrypoint of
            its own.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

from hyxlab import lockid

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ("collector", "simulator", "strategies", "hyxlab")

#: The archive has three enumerations of its own; this one is about the
#: databases they cannot see.
ARCHIVE_KERNEL = "hyxlab/store.py"

#: relpath -> (disposition, why). Every module that opens an owned
#: (non-archive) DuckDB file read-write.
ALLOWED: dict[str, tuple[str, str]] = {
    "hyxlab/streamstore.py": (
        "LIBRARY",
        "the stream store IS the writer; its owner is whoever constructs it",
    ),
    "collector/streamd.py": (
        "OWNER",
        "the stream daemon; a second copy interleaves undeduped book_events"
        " into an archive neither venue will serve again",
    ),
    "simulator/shadow.py": (
        "OWNER",
        "defines the ledger AND runs it; a second runner anchors its own"
        " run_id off the same stream head and doubles the read-attach rate",
    ),
}

DISPOSITIONS = ("OWNER", "LIBRARY")
LOCK_HELPERS = ("db_owner_lock_or_reason", "acquire_owner_lock")


# ---------------------------------------------------------------------------
# Derived: who opens a non-archive DuckDB read-write
# ---------------------------------------------------------------------------


def _called_name(call: ast.Call) -> str:
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")


def _kw(call: ast.Call, name: str):
    return next((k.value for k in call.keywords if k.arg == name), None)


def _is_rw_open(call: ast.Call) -> bool:
    """A read-WRITE attach: `duckdb.connect(...)` or `connect_retry(...)`
    that does not literally pass `read_only=True`.

    `connect_retry` defaults to read_only=True, so it counts only when a
    caller turns the default off — the helper's whole point is that a
    reader cannot become a writer by accident.
    """
    name = _called_name(call)
    ro = _kw(call, "read_only")
    literal_true = isinstance(ro, ast.Constant) and ro.value is True
    if name == "connect":
        f = call.func
        if not (isinstance(f, ast.Attribute) and getattr(f.value, "id", "") == "duckdb"):
            return False
        return not literal_true
    if name == "connect_retry":
        return isinstance(ro, ast.Constant) and ro.value is False
    return False


def _module_paths() -> list[str]:
    return [
        path.relative_to(ROOT).as_posix()
        for pkg in PACKAGES
        for path in sorted((ROOT / pkg).rglob("*.py"))
    ]


def owned_db_writers() -> dict[str, set[str]]:
    """relpath -> the classes (or "<module>") that open a file read-write."""
    out: dict[str, set[str]] = {}
    for rel in _module_paths():
        if rel == ARCHIVE_KERNEL:
            continue
        tree = ast.parse((ROOT / rel).read_text())
        owners: set[str] = set()
        for cls in ast.walk(tree):
            if not isinstance(cls, ast.ClassDef):
                continue
            if any(isinstance(c, ast.Call) and _is_rw_open(c) for c in ast.walk(cls)):
                owners.add(cls.name)
        in_class = {
            id(c) for cls in ast.walk(tree) if isinstance(cls, ast.ClassDef) for c in ast.walk(cls)
        }
        if any(
            isinstance(c, ast.Call) and _is_rw_open(c) and id(c) not in in_class
            for c in ast.walk(tree)
        ):
            owners.add("<module>")
        if owners:
            out[rel] = owners
    return out


def _funcs_by_key(tree: ast.AST) -> dict[str, ast.AST]:
    """Callable name -> body, with `__init__` keyed by its CLASS name so a
    `Foo(...)` construction resolves to the code it actually runs."""
    out: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for m in node.body:
                if isinstance(m, ast.FunctionDef | ast.AsyncFunctionDef):
                    out.setdefault(node.name if m.name == "__init__" else m.name, m)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            out.setdefault(node.name, node)
    return out


def owning_entrypoints(writer_classes: set[str]) -> set[str]:
    """Modules whose `main()` reaches the construction of a writer class.

    Fixpoint, so a helper layer between `main` and the constructor (as in
    `main -> ShadowRunner -> ShadowLedger`) does not hide the ownership.
    """
    out: set[str] = set()
    for rel in _module_paths():
        tree = ast.parse((ROOT / rel).read_text())
        funcs = _funcs_by_key(tree)
        if "main" not in funcs:
            continue
        reaching = {
            key
            for key, fn in funcs.items()
            if any(_called_name(c) in writer_classes for c in ast.walk(fn) if isinstance(c, ast.Call))
        }
        changed = True
        while changed:
            changed = False
            for key, fn in funcs.items():
                if key in reaching:
                    continue
                if any(_called_name(c) in reaching for c in ast.walk(fn) if isinstance(c, ast.Call)):
                    reaching.add(key)
                    changed = True
        if "main" in reaching:
            out.add(rel)
    return out


def _main_of(rel: str) -> ast.FunctionDef:
    tree = ast.parse((ROOT / rel).read_text())
    fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
    assert fn is not None, f"{rel} writes an owned database with no main()"
    return fn


def owner_lock_args(rel: str) -> list[str]:
    """The unparsed first argument of each owner-lock call in `main`."""
    return [
        ast.unparse(c.args[0])
        for c in ast.walk(_main_of(rel))
        if isinstance(c, ast.Call) and _called_name(c) in LOCK_HELPERS and c.args
    ]


# ---------------------------------------------------------------------------
# The enumeration
# ---------------------------------------------------------------------------


def writer_classes() -> set[str]:
    return {c for cs in owned_db_writers().values() for c in cs if c != "<module>"}


def test_every_owned_db_writer_and_its_owner_are_enumerated():
    """Both ends, because either alone leaves the question open: the module
    that OPENS the file, and the entrypoint that RUNS it. streamd opens
    nothing itself — it constructs a StreamStore — and an enumeration that
    only followed `duckdb.connect` would have declared the stream archive
    covered while its daemon took no lock."""
    found = set(owned_db_writers()) | owning_entrypoints(writer_classes())
    new = found - set(ALLOWED)
    assert not new, (
        "writes a non-archive DuckDB, or runs something that does, with no"
        f" written-down owner: {sorted(new)} — take"
        " hyxlab.lockid.db_owner_lock_or_reason in the entrypoint, or add it"
        " here as OWNER/LIBRARY with the reason"
    )
    gone = set(ALLOWED) - found
    assert not gone, f"ALLOWED names modules that no longer write an owned db: {sorted(gone)}"


def test_every_disposition_is_one_of_the_two_with_a_reason():
    bad = {k: d for k, (d, _) in ALLOWED.items() if d not in DISPOSITIONS}
    assert not bad, bad
    thin = {k: why for k, (_, why) in ALLOWED.items() if len(why) < 10}
    assert not thin, f"a disposition without a real reason is a rubber stamp: {thin}"


def test_the_derived_scan_still_sees_both_writer_classes():
    """Pinned so a refactor that empties the scanner reddens here instead
    of quietly making every disposition unenforced — the mutator set is
    pinned in the writer-lock enumeration for the same reason."""
    found = owned_db_writers()
    assert "StreamStore" in found.get("hyxlab/streamstore.py", set())
    assert "ShadowLedger" in found.get("simulator/shadow.py", set())


def test_every_entrypoint_that_opens_an_owned_db_is_an_enumerated_owner():
    entry = owning_entrypoints(writer_classes())
    declared = {k for k, (d, _) in ALLOWED.items() if d == "OWNER"}
    assert entry == declared, (
        f"main() constructs a writer of an owned database: {sorted(entry)};"
        f" declared OWNER: {sorted(declared)}"
    )


@pytest.mark.parametrize("rel", [k for k, (d, _) in ALLOWED.items() if d == "LIBRARY"])
def test_a_library_writer_has_no_entrypoint_of_its_own(rel):
    """LIBRARY means "someone else owns this"; a module that can be RUN
    owns what it opens and cannot delegate the question."""
    assert rel in owned_db_writers(), f"{rel} is labelled LIBRARY but opens nothing"
    tree = ast.parse((ROOT / rel).read_text())
    assert not any(
        isinstance(n, ast.FunctionDef) and n.name == "main" for n in tree.body
    ), f"{rel} is labelled LIBRARY but has a main() — it is an OWNER"


@pytest.mark.parametrize("rel", [k for k, (d, _) in ALLOWED.items() if d == "OWNER"])
def test_an_owner_locks_a_path_not_a_job_name(rel):
    """File-scoped on purpose. A literal job name here would exclude a run
    pointed at a different database — a tmpdir, a copied ledger — which is
    the starvation shape EXP-1371 designed the instance lock away from."""
    args = owner_lock_args(rel)
    assert args, f"{rel} is labelled OWNER but main() never calls {LOCK_HELPERS}"
    for a in args:
        is_literal = a.startswith(("'", '"'))
        assert (not is_literal) or a.strip("'\"").endswith(".duckdb"), (
            f"{rel} locks {a!r}, which is a bare name, not the database it opens"
        )


def test_streamd_locks_the_very_database_it_opens():
    """The one entrypoint where both expressions are visible in `main`, so
    "lock the file you open" is checkable rather than asserted: a daemon
    that locks the default path and opens `--db` is unguarded on exactly
    the runs an operator starts by hand."""
    main = _main_of("collector/streamd.py")
    opened = {
        ast.unparse(c.args[0])
        for c in ast.walk(main)
        if isinstance(c, ast.Call) and _called_name(c) == "StreamStore" and c.args
    }
    assert opened, "streamd.main no longer constructs StreamStore — re-derive this test"
    assert opened <= set(owner_lock_args("collector/streamd.py")), (
        f"streamd opens {sorted(opened)} but locks {owner_lock_args('collector/streamd.py')}"
    )


# ---------------------------------------------------------------------------
# Behaviour: what the file lock does, and what the owner lock does
# ---------------------------------------------------------------------------


_PROBE = """
import sys, time
sys.path.insert(0, {root!r})
from datetime import UTC, datetime
from hyxlab.streamstore import StreamStore, BookEvent
db, tag, cycles = sys.argv[1], sys.argv[2], int(sys.argv[3])
# Cold start retries, because that is what the second copy really does:
# `hyxlab-stream` is Restart=always, and an operator starting it by hand
# tries again. Only the STEADY state is under test — whether a writer
# already flushing keeps the newcomer out between its bursts.
for attempt in range(60):
    try:
        s = StreamStore(db); break
    except Exception:
        time.sleep(0.05)
else:
    print("0 60"); raise SystemExit(0)
ok = declined = 0
for i in range(cycles):
    s.append_events([BookEvent(venue="kalshi", market_id=f"{{tag}}-{{i}}", recv_ts=datetime.now(UTC),
                    src_ts=None, sid=1, seq=i, kind="delta", side="yes", price=0.5, qty=1.0)])
    try:
        s.flush(); ok += 1
    except Exception:
        declined += 1
    time.sleep(0.2)
print(f"{{ok}} {{declined}}", flush=True)
"""


def test_the_file_lock_excludes_nothing_between_bursts(tmp_path):
    """The measurement this whole enumeration rests on.

    DuckDB's file lock is held only while a burst is open, so a second
    daemon that starts between two flushes is never refused — it writes
    duplicate rows into tables with no key and no dedupe. If this test
    ever goes RED because the second writer is excluded, the hazard is
    gone and the owner lock can be reconsidered; until then it is real.
    """
    db = tmp_path / "stream.duckdb"
    script = tmp_path / "probe.py"
    script.write_text(_PROBE.format(root=str(ROOT)))
    first = subprocess.Popen(
        [sys.executable, str(script), str(db), "A", "10"], stdout=subprocess.PIPE, text=True
    )
    try:
        # Start the second writer while the first is between flushes —
        # the steady state of a 24/7 daemon, not a cold start.
        second = subprocess.run(
            [sys.executable, str(script), str(db), "B", "6"],
            capture_output=True,
            text=True,
            timeout=120,
        )
    finally:
        first.communicate(timeout=120)
    assert second.returncode == 0, second.stderr
    b_ok, b_declined = (int(x) for x in second.stdout.split())
    assert b_ok == 6 and b_declined == 0, (
        "a second stream daemon was refused by the file lock — if this is"
        " now true the guard changed underneath this enumeration"
    )
    with duckdb.connect(str(db), read_only=True) as conn:
        rows = dict(
            conn.execute(
                "SELECT split_part(market_id,'-',1), count(*) FROM book_events GROUP BY 1"
            ).fetchall()
        )
    assert rows.get("A") and rows.get("B"), (
        f"both writers must land rows for this hazard to be the one described: {rows}"
    )


def test_the_owner_lock_refuses_a_second_writer_of_the_same_file(tmp_path):
    db = str(tmp_path / "stream.duckdb")
    held, why = lockid.db_owner_lock_or_reason(db)
    assert held is not None and why == ""
    refused, why = lockid.db_owner_lock_or_reason(db)
    assert refused is None
    assert db in why and "pid=" in why and "since" in why, why
    held.close()
    again, _ = lockid.db_owner_lock_or_reason(db)
    assert again is not None, "the lock must be re-acquirable once released"
    again.close()


def test_two_databases_do_not_exclude_each_other(tmp_path):
    """File-scoped, so the daemon does not block a run on a copied ledger
    and the stream daemon does not block the shadow daemon."""
    a, _ = lockid.db_owner_lock_or_reason(str(tmp_path / "hyxstream.duckdb"))
    b, _ = lockid.db_owner_lock_or_reason(str(tmp_path / "hyxshadow.duckdb"))
    assert a is not None and b is not None
    a.close()
    b.close()


def test_a_refused_streamd_exits_75_before_it_opens_anything(tmp_path, monkeypatch, capsys):
    """Run it, do not read it. A daemon that logs the refusal and starts
    anyway is unguarded WITH a reassuring line in the journal, and the
    file must be untouched: creating the schema is already an attach."""
    from collector import streamd

    db = tmp_path / "stream.duckdb"
    held, _ = lockid.db_owner_lock_or_reason(str(db))
    assert held is not None
    monkeypatch.setattr(sys, "argv", ["streamd", "--db", str(db), "--smoke", "1"])
    with pytest.raises(SystemExit) as exc:
        streamd.main()
    assert exc.value.code == 75
    assert "pid=" in capsys.readouterr().out
    assert not db.exists(), "streamd attached the database it had just been refused"
    held.close()


def test_a_refused_shadow_exits_75_before_it_trades(tmp_path, monkeypatch, capsys):
    from simulator import shadow

    ledger = tmp_path / "hyxshadow.duckdb"
    monkeypatch.setattr(shadow, "SHADOW_DB", str(ledger))
    held, _ = lockid.db_owner_lock_or_reason(str(ledger))
    assert held is not None
    monkeypatch.setattr(sys, "argv", ["shadow", "--duration", "1"])
    with pytest.raises(SystemExit) as exc:
        shadow.main()
    assert exc.value.code == 75
    assert not ledger.exists(), "shadow opened the ledger it had just been refused"
    held.close()


def test_the_owner_lock_is_named_after_the_database():
    assert lockid.owner_lock_path("data/hyxstream.duckdb") == "data/hyxstream.duckdb.owner.lock"
    assert lockid.owner_lock_path("a.duckdb") != lockid.owner_lock_path("b.duckdb")
