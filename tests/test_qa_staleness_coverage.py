"""Every archive table's INGEST STAMP must be asked its age by QA, or be
declared with a measured witness.

WHY THIS EXISTS ON TOP OF test_qa_table_coverage (2026-09-04, same day).
That test derives "every archive table is READ by QA", and it stated the
property it does not reach: read is weaker than watched. `markets` is read
only through `close_time` and `result` — BUSINESS columns of settled
markets — so `markets` could stop being written entirely and every check
in collector/qa.py would still read green off rows written weeks ago. That
is the breadth hole one notch weaker, and it is the shape that has now
landed twice.

So this derives the stronger comparison:

  the archive's tables + their stamps  <- CREATE TABLE in hyxlab/store.py
                                          (STAMP declares WHICH column;
                                           the column must exist there)
  QA's staleness readers               <- the SQL in collector/qa.py

where a staleness reader is one of the two shapes that UNAMBIGUOUSLY ask
"how old is the newest row":

  (a) _check_freshness(conn, name, table, col, ...)      — the helper
  (b) `... max(<col> ...) ... FROM <table>`              — newest-row age

THE RECENCY-WINDOW SHAPE IS DELIBERATELY NOT CREDITED, and that decision
came out of the measurement. `FROM <t> WHERE <col> > ? - INTERVAL ...` is
a third shape qa.py really uses, and crediting it would have marked both
of its live users watched — but they behave OPPOSITELY on an empty
window. `sweep ran in last 36h` counts the window and FAILS at zero;
the poly swept-universe tripwire guards on `len(runs) >= 2` and SKIPS at
zero, which is precisely what a dead sweep produces. No static test can
tell those apart, so the shape is dropped and both tables carry a
declared WITNESS instead. (That the skip is now PRINTED rather than
silent is a separate derivation — tests/test_qa_silent_guards.py — and it
does not make the shape an age check.) The derivation therefore errs
toward naming a table that IS watched (declare it) and never toward
missing one that is not.

WHAT THIS STILL DOES NOT CLAIM. That a credited reader FAILS when its
table is stale — only that the question is asked. Whether the answer is
loud is what the mutant tests in tests/test_hyxlab_qa.py are for.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STORE = REPO / "hyxlab" / "store.py"
QA = REPO / "collector" / "qa.py"

# The column whose max() answers "when did this table last receive data".
# None means the table HAS no ingest stamp — every timestamp it carries is a
# business time that a backfill can write out of order — which is a fact
# about the schema, not an excuse, and still needs an entry in WITNESS.
STAMP: dict[str, str | None] = {
    "markets": "updated_at",
    "snapshots": "ts",
    "breadth_snapshots": "ts",
    "nws_forecasts": "fetched_at",
    "candles": None,  # end_ts is the candle's period end, not its ingest
    "observations": None,  # obs_date is the observation's day
    "trades": None,  # ts is the exchange print time; ingest is trades_swept
    "trades_swept": "swept_at",
    "poly_prices": "ts",
    "poly_market_stats": "ts",
    "series": "updated_at",
    "sweep_log": "swept_at",
    "watermarks": None,  # last_close_ts is the swept market's close, not ingest
    "econ_vintages": "knowable_at",
    "news_items": "knowable_at",
    "schema_meta": None,  # one row, written by migrate; it has no time at all
}

# Tables whose staleness QA does not ask directly. Each names the WITNESS
# that makes the question redundant — not a reason it is hard.
WITNESS: dict[str, str] = {
    "markets": (
        "Written in the same transaction, from the same fetch, as the "
        "snapshots that witness it: collect.py's per-series try block builds "
        "cyc.infos and cyc.kalshi_snaps from ONE get_markets pair and "
        "write_cycle commits both or neither. Unlike the nws pull (its own "
        "try, hence its own check), `markets` cannot go stale while "
        "'collector fresh' passes — measured 2026-09-04, max(updated_at) is "
        "0.01h old, the 5-min cycle."
    ),
    "candles": (
        "No ingest stamp exists (end_ts is the period end, and the sweep "
        "backfills history, so max(end_ts) moves backwards on a legitimate "
        "run). Watched instead through the writer's own log, which does "
        "carry one: 'candle ingest landing (36h)' sums sweep_log.n_candles "
        "over the same window as 'sweep ran in last 36h'."
    ),
    "observations": (
        "NO LIVE WRITER — hand-run collector/backfill.py only; newest row "
        "2026-07-06, 1,827 rows (measured 2026-09-04). Already declared "
        "UNREAD in tests/test_qa_table_coverage.py for the same reason: a "
        "freshness check would fail forever about a table nothing fills."
    ),
    "trades": (
        "No ingest stamp (ts is the exchange print time; a backfill of an "
        "old market writes old ts). The ingest side IS stamped and IS "
        "watched: qa_tape_coverage reads max(trades_swept.swept_at) for the "
        "sweeper's pulse and judges per-market persistence, which is "
        "strictly stronger than a table-level max() that any one live market "
        "would keep green."
    ),
    "sweep_log": (
        "Asked in the recency-window shape this derivation does not credit: "
        "'sweep ran in last 36h' counts ok entries in the window and fails at "
        "zero, which is the same question max(swept_at) would ask and is "
        "strictly stronger (it ignores rows logged 'truncated' or failed)."
    ),
    "poly_market_stats": (
        "Same writer, same run, as poly_prices: collector/poly_sweep.py "
        "writes both inside one walk, and 'poly prices fresh (< 30h old)' "
        "reads max(poly_prices.ts). Measured 2026-09-04 the two move together "
        "(22.0h, the same sweep). Its OWN reader — the swept-universe "
        "tripwire — is explicitly not the witness: it guards on len(runs) >= "
        "2, so it measures nothing exactly when the sweep dies. Since "
        "2026-09-05 that guard at least SAYS so (a bounded SKIP escalating "
        "to FAIL, see tests/test_qa_silent_guards.py), but a skip is not an "
        "age check and poly_prices remains the witness."
    ),
    "series": (
        "Upserted whole by the 06:10Z sweep; its liveness IS the sweep's, "
        "which 'sweep ran in last 36h' already watches. Measured 2026-09-04 "
        "max(updated_at) = 06:10:01Z, the timer to the second."
    ),
    "watermarks": (
        "No ingest stamp, and the same witness as `series`: written by, and "
        "only by, the sweep run that 'sweep ran in last 36h' watches."
    ),
    "schema_meta": (
        "No time column and no live writer: one row, rewritten only by "
        "`python -m hyxlab.migrate`. Its staleness question is the VERSION, "
        "not the clock, and 'archive schema at current version' asks it."
    ),
}


def _create_table_columns() -> dict[str, set[str]]:
    """table -> its declared columns, from the schema DDL itself."""
    text = STORE.read_text()
    out: dict[str, set[str]] = {}
    for m in re.finditer(r"CREATE TABLE IF NOT EXISTS\s+([a-z_]+)\s*\((.*?)\n\);", text, re.S):
        out[m.group(1)] = set(re.findall(r"^\s{4}([a-z_]+)\s+\w", m.group(2), re.M))
    for m in re.finditer(r"ALTER TABLE\s+([a-z_]+)\s+ADD COLUMN IF NOT EXISTS\s+([a-z_]+)", text):
        out.setdefault(m.group(1), set()).add(m.group(2))
    return out


def _sql_strings(tree: ast.AST) -> list[str]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            out.append(
                "".join(p.value if isinstance(p, ast.Constant) else " ? " for p in node.values)
            )
    return out


def _freshness_helper_pairs(tree: ast.AST) -> set[tuple[str, str]]:
    """(table, col) pairs passed to _check_freshness — shape (a)."""
    pairs = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_check_freshness"
            and len(node.args) > 3
            and all(isinstance(a, ast.Constant) for a in node.args[2:4])
        ):
            pairs.add((node.args[2].value, node.args[3].value))
    return pairs


def _staleness_readers(source: str | None = None) -> set[tuple[str, str]]:
    """(table, col) pairs whose age qa.py asks. A query mentioning several
    tables is credited only for columns those tables actually declare — the
    tape-coverage join reads `markets.close_time` alongside `trades_swept`,
    and crediting every table with every column is how a join makes an
    unwatched table look watched.

    `source` overrides the module read from disk — qa.py has no multi-table
    max() query today, so the filter would otherwise sit unexercised until
    the day one lands, which is the day it has to work."""
    schema = _create_table_columns()
    tree = ast.parse(QA.read_text() if source is None else source)
    found = _freshness_helper_pairs(tree)
    for sql in _sql_strings(tree):
        flat = " ".join(sql.split())
        tables = set(re.findall(r"(?:FROM|JOIN)\s+([a-z_]+)\b", flat))
        if not tables:
            continue
        cols = set()
        for inner in re.findall(r"max\(([^)]*)", flat):  # shape (b)
            cols |= set(re.findall(r"[a-z_]+", inner))
        found |= {(t, c) for t in tables for c in cols if c in schema.get(t, ())}
    return found


def test_every_stamped_table_has_its_age_asked_or_a_witness():
    """The check `markets` needed and never had: not "is this table read"
    but "would anyone notice it stopping"."""
    readers = _staleness_readers()
    missing = sorted(
        t
        for t, col in STAMP.items()
        if col is not None and t not in WITNESS and (t, col) not in readers
    )
    assert not missing, (
        f"archive table(s) {missing} carry an ingest stamp that collector/qa.py never asks "
        "the age of — they can stop being written and every check stays green. Add a "
        "freshness check, or declare the table in WITNESS with the reader that covers it."
    )


def test_stamp_declares_a_column_that_exists():
    """STAMP is a hand-written claim about someone else's schema. A renamed
    or dropped column would silently make its table look watched."""
    schema = _create_table_columns()
    for table, col in STAMP.items():
        assert table in schema, f"STAMP names {table}, which is not an archive table"
        if col is not None:
            assert col in schema[table], f"{table}.{col} is not a column of {table}"


def test_stamp_covers_every_archive_table():
    schema = _create_table_columns()
    missing = sorted(set(schema) - set(STAMP))
    assert not missing, (
        f"archive table(s) {missing} have no STAMP entry: name the column whose max() "
        "answers 'when did this last receive data', or None if it genuinely has none."
    )


def test_every_witness_is_a_real_named_witness():
    """A witness that names no reader is the omission wearing a hat — the
    same bar tests/test_qa_table_coverage.py holds UNREAD to."""
    for table, reason in WITNESS.items():
        assert table in STAMP, f"WITNESS names {table}, which has no STAMP entry"
        assert len(reason) > 120, f"{table}: name the reader that makes the question redundant"


def test_unstamped_tables_are_declared_too():
    """A None stamp is a claim about the schema, not a pass: the table still
    has to say what watches it (candles is watched through sweep_log)."""
    missing = sorted(t for t, col in STAMP.items() if col is None and t not in WITNESS)
    assert not missing, f"table(s) {missing} declare no ingest stamp and no witness either"


def test_the_derivation_is_not_vacuous():
    """A parser that found nothing would report every table unwatched and
    fail loudly; one that matched anything would pass while seeing nothing.
    Pin both ends against pairs whose status is known by hand."""
    readers = _staleness_readers()
    for pair in (
        ("snapshots", "ts"),  # (a) helper
        ("breadth_snapshots", "ts"),  # (a) helper
        ("nws_forecasts", "fetched_at"),  # (a) helper
        ("poly_prices", "ts"),  # (b) max()
        ("news_items", "knowable_at"),  # (b) max()
        ("econ_vintages", "knowable_at"),  # (b) max() around a cast
        ("trades_swept", "swept_at"),  # (b) max()
    ):
        assert pair in readers, f"{pair} is asked its age by qa.py — the derivation missed it"
    # The other end: tables qa.py mentions but never asks the age of. If one
    # of these turns up credited, the parser is matching noise — most likely
    # a JOIN lending one table another's column.
    for pair in (
        ("markets", "updated_at"),
        ("sweep_log", "swept_at"),  # counted in a window, never max()d
        ("poly_market_stats", "ts"),  # ditto, and its check skips when empty
    ):
        assert pair not in readers, (
            f"the derivation now claims {pair} is watched for staleness; if a real check "
            "was added, drop the table from WITNESS — if not, the parser is matching noise"
        )


def test_a_join_does_not_lend_one_table_anothers_stamp():
    """The filter above, exercised on the query shape that would defeat this
    derivation: an age asked about ONE table inside a join that mentions
    several. Without it, `markets` rides in on trades_swept's stamp and the
    hole this whole file exists for reads as covered."""
    joined = _staleness_readers(
        source='q = "SELECT max(s.swept_at) FROM markets m JOIN trades_swept s'
        " ON s.market_id = m.market_id\""
    )
    assert ("trades_swept", "swept_at") in joined
    assert ("markets", "swept_at") not in joined, (
        "a join credited `markets` with trades_swept's stamp — every unwatched table "
        "joined against a watched one now reads as covered"
    )
