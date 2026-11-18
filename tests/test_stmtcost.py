"""A long write burst says which KIND of statement held the archive and
whether the hold was worked or waited (mistakes #212). The poly flush's
`trades 73.1` (2026-11-18) is one `Store` call covering a Python
normalisation, a bind into a temp table and an anti-join insert; the same
call on a replay of the same batch takes 10.2 s, and a per-call number
cannot say which third production spends the difference in."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from collector import poly_sweep as ps
from collector import sweep as sweep_mod
from collector.stmtcost import PARTS, StatementCost, kind
from hyxlab.store import Store


def _trades(n, tag="t"):
    ts = datetime(2026, 11, 18, 4, 25, tzinfo=UTC)
    return [("polymarket", f"m{i % 3}", f"{tag}{i}", ts, 0.5, 1.0, "yes", False) for i in range(n)]


def test_the_stores_own_statements_file_under_their_kinds(tmp_path):
    store = Store(str(tmp_path / "lab.duckdb"))
    cost = StatementCost.attach(store)
    assert store.insert_trades(_trades(50)) == 50
    assert store.insert_trades(_trades(50)) == 0, "the proxy must not change the dedup"
    store.close()
    p = cost.parts()
    # Both real statements ran through the proxy and were told apart: the
    # classifier is tested against the SQL `Store` actually issues, not a copy.
    assert p["stage"] > 0.0 and p["dedup"] > 0.0 and p["close"] > 0.0
    assert set(p) <= set(PARTS)


def test_kinds():
    assert kind("INSERT INTO _staging SELECT unnest(?::VARCHAR[])") == "stage"
    assert kind("INSERT INTO _mkstage VALUES (?,?)") == "stage"
    assert (
        kind("INSERT INTO trades SELECT s.* FROM _staging s\n WHERE NOT EXISTS (SELECT 1)")
        == "dedup"
    )
    assert kind("SELECT count(*) FROM trades") == "other"
    assert kind("INSERT INTO poly_market_stats VALUES (?,?,?,?)") == "other"


class _Conn:
    def __init__(self, clock):
        self.clock, self.closed = clock, False

    def execute(self, sql, *a):
        self.clock[0] += 61.0 if sql.startswith("INSERT INTO _") else 11.9
        if "boom" in sql:
            raise RuntimeError("boom")
        return "r"

    def close(self):
        self.clock[0] += 0.3
        self.closed = True

    def fetchone(self):
        return "passed through"


@pytest.fixture
def clocks(monkeypatch):
    wall, main, proc = [100.0], [5.0], [7.0]
    monkeypatch.setattr("collector.stmtcost.time.monotonic", lambda: wall[0])
    monkeypatch.setattr("collector.stmtcost.time.thread_time", lambda: main[0])
    monkeypatch.setattr("collector.stmtcost.time.process_time", lambda: proc[0])
    return wall, main, proc


def test_the_clause_splits_wall_by_kind_and_names_both_cpu_clocks(clocks):
    wall, main, proc = clocks
    cost = StatementCost(_Conn(wall))
    wall[0] += 0.6  # python-side work before the first statement
    assert cost.execute("INSERT INTO _staging SELECT 1") == "r"
    assert cost.execute("INSERT INTO trades SELECT 1 WHERE NOT EXISTS (SELECT 1)") == "r"
    main[0] += 9.1
    proc[0] += 24.0
    cost.close()
    # The clocks froze at close: what the caller does before it renders the
    # line (release the flock, sum its steps) is not the burst's.
    wall[0] += 500.0
    main[0] += 500.0
    proc[0] += 500.0
    assert cost.fetchone() == "passed through"
    assert cost.clause() == (
        "stage 61.0, dedup 11.9, close 0.3, python 0.6; cpu 9.1s main thread, 24.0s process"
    )


def test_a_statement_that_raises_is_charged(clocks):
    wall, _, _ = clocks
    cost = StatementCost(_Conn(wall))
    with pytest.raises(RuntimeError):
        cost.execute("INSERT INTO _staging boom")
    assert cost.parts()["stage"] == pytest.approx(61.0)


def test_an_empty_burst_has_no_clause(clocks):
    wall, _, _ = clocks
    assert StatementCost(_Conn(wall)).clause() == ""


def test_the_poly_long_flush_line_carries_the_clause(clocks):
    wall, main, proc = clocks
    cost = StatementCost(_Conn(wall))
    cost.execute("INSERT INTO _staging SELECT 1")
    proc[0] += 3.0
    cost.close()
    line = ps.long_flush_line({"open": 0.1, "trades": 61.0, "close": 0.3}, 0.0, 5000, 300000, cost)
    assert line == (
        "[poly] long flush: held 61.3s after 0.0s queued (open 0.1, trades 61.0, close 0.3)"
        " for 5000 prices + 300000 trades staged"
        " [by statement: stage 61.0, close 0.3; cpu 0.0s main thread, 3.0s process]"
    )


def test_a_real_poly_flush_prints_the_split(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ps, "LOCK_FILE", str(tmp_path / "writer.lock"))
    monkeypatch.setattr(ps, "LONG_FLUSH_S", 0.0)
    monkeypatch.setattr("collector.stmtcost.MIN_PART_S", 0.0)
    db = str(tmp_path / "lab.duckdb")
    Store(db).close()
    batch = {"infos": [], "stats": [], "prices": [], "trades": _trades(40), "tail_stops": []}
    batch.update(n_prices=0, n_trades=0, n_tail_stops=0)
    ps._flush(db, batch)
    assert batch["n_trades"] == 40
    (line,) = [ln for ln in capsys.readouterr().out.splitlines() if "long flush" in ln]
    tail = line.split(" [by statement: ")[1]
    assert tail.startswith("stage ") and ", dedup " in tail and ", close " in tail
    assert "; cpu " in tail and tail.endswith("s process]")


def test_a_real_kalshi_burst_prints_the_split(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sweep_mod, "LONG_BURST_S", 0.0)
    monkeypatch.setattr("collector.stmtcost.MIN_PART_S", 0.0)
    db = str(tmp_path / "lab.duckdb")
    Store(db).close()
    with sweep_mod.writer_burst(db, lock_file=str(tmp_path / "writer.lock")) as store:
        rows = [("kalshi", *r[1:]) for r in _trades(40, "k")]
        assert store.insert_trades(rows) == 40
    (line,) = [ln for ln in capsys.readouterr().out.splitlines() if "long write burst" in ln]
    assert line.startswith("[burst] long write burst: held lab.duckdb ")
    tail = line.split("s [by statement: ")[1]
    assert tail.startswith("stage ") and ", dedup " in tail and ", close " in tail
    assert tail.endswith("s process]")
