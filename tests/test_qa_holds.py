"""QA is a daily reader of two files a 24/7 writer owns, and it published no
hold at all.

THE DEFECT (2026-09-28, ladder item (6)). `collector.qa` opens each of its
three sections' databases read-only and keeps the connection for the whole
section: `hyxstream.duckdb` (`collector.streamd`'s file), `hyxlab.duckdb` (the
5-minute collector's, where a hold is a DROPPED capture cycle rather than a
late report -- `collector.sweep.writer_burst` measured 421 of 3,706) and the
shadow ledger. DuckDB's lock is taken by the OPEN, so an idle connection
excludes the writer exactly as hard as a busy one -- the 2026-09-25 hold that
cost streamd 387,856 rows was afterwards measured 97% idle (mistakes #91).
Those attaches have recorded ledger rows since `duck_connect` became ledgered
(2026-09-28, #96) and were exactly the `held_unknown_n` debt that field
counts: measured, never published, because QA emitted no block.

WHAT THESE TESTS PIN, in the order the reading can go wrong:
  - the hold spans the section BODY, not the open (a timer started at the
    first query would have called the 2026-09-25 replay innocent);
  - both files are named, per file, because QA writes no JSON and the journal
    line is the whole artifact -- and scalars summed over different locks are
    not a quantity (mistakes #101);
  - a refused ladder charges nothing: it never opened the file, so its zero is
    MEASURED and not unknown (mistakes #94), and the SKIP line is its witness;
  - the line survives a section that raised, which is the run whose holds most
    need a witness.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import collector.qa as qa
from collector.venues.kalshi_ws import parse_message
from hyxlab.store import Store, attach_wait_block, attach_wait_line, reset_attach_waits
from hyxlab.streamstore import StreamStore

NOW = datetime.now(UTC)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(qa, "STATE", tmp_path / "sections.json")
    qa._failures.clear()
    qa._skipped.clear()
    reset_attach_waits()
    yield
    qa._failures.clear()
    qa._skipped.clear()
    reset_attach_waits()


def _stream(path):
    store = StreamStore(path)
    frame = {
        "type": "trade",
        "sid": 1,
        "seq": 1,
        "msg": {
            "market_ticker": "M1",
            "yes_price_dollars": "0.4000",
            "count_fp": "5.00",
            "taker_side": "yes",
            "ts": int(NOW.timestamp()),
            "ts_ms": int(NOW.timestamp() * 1000),
        },
    }
    store.append_trades(parse_message(frame, NOW)[1])
    store.flush()
    return path


def _archive(path):
    Store(path).close()
    return path


def _only_qas_attaches() -> None:
    """Seeding a fixture attaches too -- `StreamStore` and `Store` are
    ledgered helpers -- so the population is reset after the ARRANGE phase.
    Otherwise the fixture's own opens arrive as `held_unknown_n`, which is the
    debt this work paid off, in a test written to prove it is gone."""
    reset_attach_waits()


def test_the_hold_spans_the_section_body_not_the_open(tmp_path, monkeypatch):
    """The property #91 is about. `_held_ro` releases when the section
    returns, so a slow body IS a long hold -- and 0.05s of checks must show up
    as 0.05s of exclusion, charged to the file the section opened."""
    path = _stream(tmp_path / "s.duckdb")
    _only_qas_attaches()
    monkeypatch.setattr(qa, "_stream_checks", lambda *a, **k: time.sleep(0.05))
    qa.qa_stream(26.0, path=str(path))

    block = attach_wait_block(rows=False)
    assert block is not None, "QA attached the stream archive and recorded nothing"
    assert block["held_n"] == 1
    assert block["held_unknown_n"] == 0, "a QA attach went unmeasured"
    assert block["held_by_db"]["s.duckdb"]["held_s_total"] >= 0.05, (
        "the hold was shorter than the section body it covers — it is timing"
        " the open, not the exclusion"
    )


def test_the_line_names_every_file_qa_held(tmp_path):
    """Two sections, two live files, and the line is QA's whole artifact: it
    must attribute the seconds per file. A single `held_s_total` here would be
    a sum over two different locks (mistakes #101), one of them the 5-minute
    collector's."""
    stream = _stream(tmp_path / "s.duckdb")
    archive = _archive(tmp_path / "a.duckdb")
    _only_qas_attaches()
    qa.qa_stream(26.0, path=str(stream))
    qa.qa_archive(26.0, path=str(archive))

    block = attach_wait_block(rows=False)
    assert set(block["held_by_db"]) == {"s.duckdb", "a.duckdb"}
    assert block["held_s_total"] is None, "a sum over two different locks was published"
    assert block["held_s_max"] is None
    line = attach_wait_line(block, prefix="[qa]")
    assert "across 2 files" in line
    for name in ("s.duckdb", "a.duckdb"):
        assert name in line, f"the line does not say it held {name}"


def test_a_refused_ladder_charges_no_hold(tmp_path, monkeypatch):
    """`_connect_ro` degrades to None so a live writer is a SKIP, not a
    failure. That attach opened nothing, so there is no hold to charge and no
    unknown to count -- folding it in either way would report a file this run
    never touched."""
    monkeypatch.setattr(qa, "_connect_ro", lambda *a, **k: None)
    with qa._held_ro("data/nothing.duckdb") as conn:
        assert conn is None
    block = attach_wait_block(rows=False)
    assert block is None or block["held_n"] == 0


def test_a_section_that_raised_still_publishes_its_hold(tmp_path, monkeypatch):
    """`charge_hold` records in `finally`, and the run that died mid-section
    held the file for every second up to the exception. The reading a failed
    run leaves behind is the one nobody can re-take."""
    path = _stream(tmp_path / "s.duckdb")
    _only_qas_attaches()

    def boom(*a, **k):
        time.sleep(0.02)
        raise RuntimeError("mid-section")

    monkeypatch.setattr(qa, "_stream_checks", boom)
    with pytest.raises(RuntimeError):
        qa.qa_stream(26.0, path=str(path))

    block = attach_wait_block(rows=False)
    assert block["held_n"] == 1
    assert block["held_by_db"]["s.duckdb"]["held_s_total"] >= 0.02


def test_main_publishes_the_line_on_the_failure_path_too(tmp_path, monkeypatch):
    """The line is in `main`'s `finally` for the same reason. A QA run that
    exits 1 is the run whose contention most needs a witness, and a line
    printed only on the green path would be missing from every one of them."""
    src = Path(qa.__file__).read_text()
    body = src.split("def main() -> None:", 1)[1]
    # Asserted before the indexes so removing the `finally` fails with the
    # reason rather than with a ValueError from `str.index`.
    for token in ("    try:", "    finally:", "attach_wait_line("):
        assert token in body, (
            f"main no longer has {token.strip()!r}: the hold line is not"
            " published from a path a raising section cannot skip"
        )
    try_at = body.index("    try:")
    finally_at = body.index("    finally:")
    line_at = body.index("attach_wait_line(")
    failures_at = body.index("if _failures:")
    assert try_at < finally_at < line_at < failures_at, (
        "the attach line is not published from main's finally before the exit"
        " paths — a run that failed or raised would print no hold at all"
    )


def test_the_published_block_is_this_runs_attaches(tmp_path):
    """`reset_attach_waits()` at the top of `main`, or the block carries
    whatever an import, a test or a previous section did first — the same
    reason every other publisher in the repo calls it."""
    src = Path(qa.__file__).read_text()
    body = src.split("def main() -> None:", 1)[1]
    assert body.index("reset_attach_waits()") < body.index("qa_stream("), (
        "main publishes a block it never reset"
    )


def test_the_stale_stream_still_trips_its_check_through_the_seam(tmp_path):
    """Non-vacuity for the refactor: moving the checks into `_stream_checks`
    must not have moved them out of the section. A hold measured around a body
    that no longer runs is the worst of both."""
    store = StreamStore(tmp_path / "s.duckdb")
    old = NOW - timedelta(hours=2)
    frame = {
        "type": "trade",
        "sid": 1,
        "seq": 1,
        "msg": {
            "market_ticker": "M1",
            "yes_price_dollars": "0.4000",
            "count_fp": "5.00",
            "taker_side": "yes",
            "ts": int(old.timestamp()),
            "ts_ms": int(old.timestamp() * 1000),
        },
    }
    store.append_trades(parse_message(frame, old)[1])
    store.flush()
    qa.qa_stream(26.0, path=str(tmp_path / "s.duckdb"))
    assert any("stream fresh" in f for f in qa._failures), (
        "the stream freshness check did not run inside the held section"
    )
    assert datetime.now(UTC) > NOW - timedelta(days=1)  # clock sanity, not a claim
