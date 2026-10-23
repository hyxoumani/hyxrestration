"""The one-shot repair for markets a pre-86a4414 sweep archived resolved on
a fetch that raised (mistakes #131).

The old `except` kept the Gamma row, so `markets.result` was set for a
closed market that had no prices and no tail, and `poly_resolved_complete`
skipped it on every later run. These pin what the repair must not do:
guess an id, touch a market that carries no result, read the stamp as UTC,
or write anything on a dry run.
"""

from datetime import datetime, timedelta

import pytest

from collector import poly_unmark_errors as um
from hyxlab.models import MarketInfo
from hyxlab.store import Store

LINE = (
    "2026-10-21T04:04:23-05:00 hyz python[2255959]: [poly] HTTPError at 0x9b37b2134772e6: "
    "500 Server Error: Internal Server Error for url: https://clob.polymarket.com/pri"
)


def test_a_real_journal_line_parses_to_prefix_stamp_and_class():
    (prefix, at, cls) = um.parse_errors(LINE)[0]
    assert (prefix, cls) == ("0x9b37b2134772e6", "HTTPError")
    assert at == datetime(2026, 10, 21, 9, 4, 23)


def test_the_line_s_own_offset_is_applied_not_assumed_utc():
    (_, at, _) = um.parse_errors(LINE)[0]
    naive = datetime.fromisoformat(LINE.split()[0]).replace(tzinfo=None)
    assert at == naive + timedelta(hours=5)


def test_an_unreadable_error_line_is_a_refusal_not_a_skip():
    broken = LINE.replace("HTTPError at", "HTTP Error at")
    with pytest.raises(ValueError, match="unreadable error line"):
        um.parse_errors(broken)


def test_progress_and_done_lines_are_ignored_silently():
    text = (
        "2026-10-21T04:34:27-05:00 hyz python[1]: [poly] 200/18543 | 9994 prices\n"
        '2026-10-21T06:51:28-05:00 hyz python[1]: [poly] done: {"markets": 18543}\n'
    )
    assert um.parse_errors(text) == []


def _store(tmp_path, infos):
    store = Store(tmp_path / "a.duckdb")
    store.upsert_markets(infos)
    return store


def test_only_markets_carrying_a_result_are_selected(tmp_path):
    store = _store(
        tmp_path,
        [
            MarketInfo(venue="polymarket", market_id="0xaa", title="t", result="yes"),
            MarketInfo(venue="polymarket", market_id="0xbb", title="t", result=""),
            MarketInfo(venue="kalshi", market_id="0xcc", title="t", result="yes"),
        ],
    )
    got = um.resolved_among(store, ["0xaa", "0xbb", "0xcc", "0xdd"])
    store.close()
    assert got == ["0xaa"]


def test_commit_clears_the_result_and_counts_both_holds(tmp_path, capsys, monkeypatch):
    db = tmp_path / "lab.duckdb"
    _store(
        tmp_path,
        [MarketInfo(venue="polymarket", market_id="0x9b37b2134772e6ff", title="t", result="no")],
    ).close()
    db = tmp_path / "a.duckdb"
    monkeypatch.setattr(um, "read_journal", lambda unit, since, until: LINE)
    monkeypatch.setattr("collector.sweep.LOCK_FILE", str(tmp_path / "writer.lock"))
    assert um.main(["--db", str(db), "--since", "x", "--until", "y", "--commit"]) == 0
    out = capsys.readouterr().out
    assert "cleared result on 1 market(s)" in out, out
    lines = [ln for ln in out.splitlines() if ln.startswith("[unmark] attach_wait:")]
    assert len(lines) == 1 and "on a.duckdb 2 attaches" in lines[0], out
    assert "held 2 for" in lines[0] and "0 unmeasured" in lines[0], lines[0]
    store = Store(db, read_only=True)
    assert store.conn.execute("SELECT result FROM markets").fetchone() == ("",)
    store.close()


def test_dry_run_writes_nothing_and_still_prints_its_hold(tmp_path, capsys, monkeypatch):
    db = tmp_path / "a.duckdb"
    _store(
        tmp_path,
        [MarketInfo(venue="polymarket", market_id="0x9b37b2134772e6ff", title="t", result="no")],
    ).close()
    monkeypatch.setattr(um, "read_journal", lambda unit, since, until: LINE)
    assert um.main(["--db", str(db), "--since", "x", "--until", "y"]) == 0
    out = capsys.readouterr().out
    assert "1 archived with result set -> to clear" in out and "dry run" in out, out
    assert "on a.duckdb 1 attaches" in out, out
    store = Store(db, read_only=True)
    assert store.conn.execute("SELECT result FROM markets").fetchone() == ("no",)
    store.close()


def test_an_ambiguous_prefix_is_dropped_never_cleared(tmp_path, capsys, monkeypatch):
    db = tmp_path / "a.duckdb"
    _store(
        tmp_path,
        [
            MarketInfo(venue="polymarket", market_id="0x9b37b2134772e601", title="t", result="no"),
            MarketInfo(venue="polymarket", market_id="0x9b37b2134772e602", title="t", result="no"),
        ],
    ).close()
    monkeypatch.setattr(um, "read_journal", lambda unit, since, until: LINE)
    monkeypatch.setattr("collector.sweep.LOCK_FILE", str(tmp_path / "writer.lock"))
    assert um.main(["--db", str(db), "--since", "x", "--until", "y", "--commit"]) == 0
    out = capsys.readouterr().out
    assert "DROPPED 1 prefix(es)" in out and "0 archived with result set" in out, out
    store = Store(db, read_only=True)
    assert store.conn.execute("SELECT count(*) FROM markets WHERE result = 'no'").fetchone() == (2,)
    store.close()
