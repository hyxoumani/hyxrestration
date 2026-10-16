"""The hand-run `simulator.run_backtest`, `simulator.run_sim` and
`simulator.pair_candidates` measure the hold they take on the 5-minute collector's file (2026-10-16).

First end-to-end drives of either `main`: an EMPTY archive is enough,
because the reading under test is the attach line, which prints after
the release on every path -- including `run_backtest`'s ABORT, which
until now was the only path whose `store.close()` ran before the print.
"""

from __future__ import annotations

from hyxlab.store import Store


def _empty_archive(tmp_path):
    db = tmp_path / "lab.duckdb"
    Store(db).close()  # born current, every table present, no rows
    return db


def _attach_lines(out: str, prefix: str) -> list[str]:
    return [ln for ln in out.splitlines() if ln.startswith(f"{prefix} attach_wait:")]


def test_run_backtest_prints_its_hold_even_on_the_abort_path(tmp_path, capsys):
    from simulator.run_backtest import main

    main(["--db", str(_empty_archive(tmp_path))])
    out = capsys.readouterr().out
    assert "ABORT" in out  # no forecasts -> the documented sanity gate fires
    lines = _attach_lines(out, "[backtest]")
    assert len(lines) == 1 and "1 attaches" in lines[0], out
    assert "held 1 for" in lines[0] and "0 unmeasured" in lines[0], lines[0]
    assert "attach_wait: on lab.duckdb 1 attaches" in lines[0], lines[0]


def test_pair_candidates_prints_its_hold_and_writes_the_lead_list(tmp_path, capsys):
    from simulator.pair_candidates import main

    out_dir = tmp_path / "pairs"
    main(["--db", str(_empty_archive(tmp_path)), "--out", str(out_dir)])
    out = capsys.readouterr().out
    lines = _attach_lines(out, "[pairs]")
    assert len(lines) == 1 and "held 1 for" in lines[0] and "0 unmeasured" in lines[0], out
    assert "[pairs] 0 candidates" in out
    assert len(list(out_dir.glob("*.json"))) == 1 and len(list(out_dir.glob("*.md"))) == 1


def test_run_sim_prints_its_hold_before_the_replay(tmp_path, capsys):
    from simulator.run_sim import main

    wl = tmp_path / "watchlist.json"
    wl.write_text("{}\n")
    main(["--db", str(_empty_archive(tmp_path)), "--watchlist", str(wl)])
    out = capsys.readouterr().out
    lines = _attach_lines(out, "[run_sim]")
    assert len(lines) == 1 and "held 1 for" in lines[0] and "0 unmeasured" in lines[0], out
    assert "attach_wait: on lab.duckdb 1 attaches" in lines[0], lines[0]
    # The line is printed BEFORE the replay's own output: the hold is the
    # load, and the file is already released when the Simulator starts.
    assert out.index(lines[0]) < out.index("replaying 0 snapshots"), out
