"""The hand-run `collector.backfill` and `collector.poly_tail_backfill`
measure every hold they take on the 5-minute collector's file (2026-10-16).

Their writes always went through `collector.sweep.writer_burst`, which is
`held_open`; the one bare `open_retry` read in each is what these pin, and
the line after it is the run's WHOLE exclusion of the file -- bursts
included -- rendered once (#95). First end-to-end drives of either `main`.
"""

from __future__ import annotations

import io

from collector import backfill as bf
from collector import poly_tail_backfill as tf
from hyxlab.models import MarketInfo
from hyxlab.store import Store

LINE = (
    "2026-09-12T09:18:31-05:00 hyz python[349689]: [poly] trades_tail "
    "0xb23f06d3d85c91da stopped early at 500 prints (non-list body, status 429)"
)


def _attach_lines(out: str, prefix: str) -> list[str]:
    return [ln for ln in out.splitlines() if ln.startswith(f"{prefix} attach_wait:")]


def test_backfill_s_closing_census_is_a_measured_hold(tmp_path, capsys, monkeypatch):
    db = tmp_path / "lab.duckdb"
    Store(db).close()
    monkeypatch.setattr(bf, "instance_lock_or_reason", lambda name: (io.StringIO(), ""))
    bf.main(["--db", str(db), "--skip-kalshi", "--skip-iem"])
    out = capsys.readouterr().out
    lines = _attach_lines(out, "[backfill]")
    assert len(lines) == 1 and "on lab.duckdb 1 attaches" in lines[0], out
    assert "held 1 for" in lines[0] and "0 unmeasured" in lines[0], lines[0]
    assert "[backfill] db=" in out


def test_tail_backfill_commit_counts_the_resolve_and_the_burst(tmp_path, capsys, monkeypatch):
    db = tmp_path / "lab.duckdb"
    store = Store(db)
    store.upsert_markets(
        [MarketInfo(venue="polymarket", market_id="0xb23f06d3d85c91da00", title="t")]
    )
    store.close()
    monkeypatch.setattr(tf, "read_journal", lambda unit: LINE)
    monkeypatch.setattr("collector.sweep.LOCK_FILE", str(tmp_path / "writer.lock"))
    assert tf.main(["--db", str(db), "--commit"]) == 0
    out = capsys.readouterr().out
    assert "inserted 1 new row(s)" in out, out
    lines = _attach_lines(out, "[tailfill]")
    assert len(lines) == 1 and "on lab.duckdb 2 attaches" in lines[0], out
    assert "held 2 for" in lines[0] and "0 unmeasured" in lines[0], lines[0]


def test_tail_backfill_dry_run_prints_its_one_hold_too(tmp_path, capsys, monkeypatch):
    db = tmp_path / "lab.duckdb"
    Store(db).close()
    monkeypatch.setattr(tf, "read_journal", lambda unit: LINE)
    assert tf.main(["--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "dry run" in out
    lines = _attach_lines(out, "[tailfill]")
    assert len(lines) == 1 and "held 1 for" in lines[0] and "0 unmeasured" in lines[0], out
