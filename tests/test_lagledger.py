"""The receive-lag ledger: `collector.rawlag.record_closed` writes it,
`hyxlab.lagledger` reads and renders it, the digest and the divergence
report cite it (mistakes #232)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from compression import zstd

from collector import health, rawlag
from hyxlab import lagledger

NOW = datetime(2026, 11, 22, 8, 15, tzinfo=UTC)


def _line(at: datetime, label: str, lag_s: float) -> str:
    sent = round((at.timestamp() - lag_s) * 1000)
    return f'{at.isoformat()}\t{label}\t{{"type":"trade","sending_ts_ms":{sent}}}\n'


def _hour_file(root: Path, day: str, hh: int, ch: str, lag_s: float, n: int = 40) -> Path:
    d = root / day
    d.mkdir(parents=True, exist_ok=True)
    t0 = datetime.fromisoformat(f"{day}T{hh:02d}:00:00+00:00")
    f = d / f"{hh:02d}-{ch}.jsonl.zst"
    with zstd.open(f, "wt", encoding="utf-8") as fh:
        fh.writelines(_line(t0 + timedelta(seconds=i), ch, lag_s) for i in range(n))
    return f


def _rows(spec: dict[tuple[str, int], tuple[float, float]], ch: str = "trades") -> dict:
    return {
        (day, f"{hh:02d}", ch): {"sampled": 10, "p50": p50, "p99": p99}
        for (day, hh), (p50, p99) in spec.items()
    }


# --- the writer ---------------------------------------------------------------


def test_ledgers_closed_hours_newest_first_within_the_file_bound(tmp_path: Path) -> None:
    root, ledger = tmp_path / "raw", tmp_path / "lag.jsonl"
    for hh in (4, 5, 6, 7):
        _hour_file(root, "2026-11-22", hh, "trades", 0.1 * hh)
    _hour_file(root, "2026-11-21", 23, "trades", 9.0)
    _hour_file(root, "2026-11-22", 7, "books", 1.0)  # not a ledgered channel
    assert rawlag.record_closed(root, ledger, NOW, max_files=2, every=1) == 2
    got = [json.loads(x) for x in ledger.read_text().splitlines()]
    assert [(r["day"], r["hh"], r["ch"]) for r in got] == [
        ("2026-11-22", "07", "trades"),
        ("2026-11-22", "06", "trades"),
    ]
    assert got[0]["frames"] == 40 and got[0]["sampled"] == 40
    assert abs(got[0]["p50"] - 0.7) < 0.002 and abs(got[0]["p99"] - 0.7) < 0.002
    assert got[0]["worst"] == "trades"
    # The next cycles finish the backlog and then read nothing twice.
    assert rawlag.record_closed(root, ledger, NOW, max_files=2, every=1) == 2
    assert rawlag.record_closed(root, ledger, NOW, max_files=2, every=1) == 1
    assert rawlag.record_closed(root, ledger, NOW, max_files=2, every=1) == 0
    assert len(ledger.read_text().splitlines()) == 5
    assert ("2026-11-21", "23", "trades") in lagledger.load(ledger)


def test_the_running_hour_is_not_read(tmp_path: Path) -> None:
    root, ledger = tmp_path / "raw", tmp_path / "lag.jsonl"
    _hour_file(root, "2026-11-22", 8, "trades", 0.1)  # NOW is 08:15: still open
    _hour_file(root, "2026-11-22", 7, "ticker", 0.1)
    assert rawlag.record_closed(root, ledger, NOW, every=1) == 1
    assert list(lagledger.load(ledger)) == [("2026-11-22", "07", "ticker")]
    # ... and one settle period after its hour ends, it is.
    late = datetime(2026, 11, 22, 9, 0, tzinfo=UTC) + timedelta(seconds=rawlag.SETTLE_S)
    assert rawlag.record_closed(root, ledger, late - timedelta(seconds=1), every=1) == 0
    assert rawlag.record_closed(root, ledger, late, every=1) == 1


def test_the_seconds_budget_stops_before_the_next_file(tmp_path: Path) -> None:
    root, ledger = tmp_path / "raw", tmp_path / "lag.jsonl"
    for hh in (5, 6, 7):
        _hour_file(root, "2026-11-22", hh, "trades", 0.1)
    ticks = iter([0.0, 0.0, 11.0, 11.0, 11.0])
    n = rawlag.record_closed(root, ledger, NOW, budget_s=10.0, clock=lambda: next(ticks), every=1)
    assert n == 1 and len(ledger.read_text().splitlines()) == 1


def test_an_unreadable_file_is_ledgered_once_as_unmeasured(tmp_path: Path) -> None:
    root, ledger = tmp_path / "raw", tmp_path / "lag.jsonl"
    f = _hour_file(root, "2026-11-22", 7, "trades", 0.1)
    f.write_bytes(f.read_bytes()[:20])  # a torn tail
    _hour_file(root, "2026-11-22", 6, "trades", 0.2)
    assert rawlag.record_closed(root, ledger, NOW, every=1) == 2
    rows = lagledger.load(ledger)
    assert "err" in rows[("2026-11-22", "07", "trades")]
    assert rawlag.record_closed(root, ledger, NOW, every=1) == 0  # not re-read
    span = lagledger.span_lag(datetime(2026, 11, 22, 6), datetime(2026, 11, 22, 8), rows)
    assert span.owed == 2 and [h.hour.hour for h in span.measured] == [6]


def test_a_missing_tree_is_no_rows_and_no_error(tmp_path: Path) -> None:
    assert rawlag.record_closed(tmp_path / "absent", tmp_path / "lag.jsonl", NOW) == 0
    assert lagledger.load(tmp_path / "lag.jsonl") == {}


# --- the reader ---------------------------------------------------------------


def test_a_span_owes_every_clock_hour_it_touches() -> None:
    rows = _rows({("2026-11-21", 23): (0.02, 0.1), ("2026-11-22", 0): (1.3, 8.4)})
    span = lagledger.span_lag(datetime(2026, 11, 21, 22, 59), datetime(2026, 11, 22, 1, 0), rows)
    assert span.owed == 3  # 22, 23, 00 -- an end ON the hour does not owe 01
    assert [h.hour.hour for h in span.measured] == [23, 0]
    assert [h.hour.hour for h in span.late] == [0]
    aware = lagledger.span_lag(
        datetime(2026, 11, 21, 22, 59, tzinfo=UTC), datetime(2026, 11, 22, 1, 0, 1), rows
    )
    assert aware.owed == 4


def test_late_is_the_median_and_the_tail_is_counted_apart() -> None:
    rows = _rows(
        {
            ("2026-11-21", 15): (0.08, 6.5),  # the scan: tail only
            ("2026-11-21", 16): (0.10, 0.4),
            ("2026-11-21", 17): (0.77, 8.3),
            ("2026-11-21", 19): (2.28, 9.0),
        }
    )
    span = lagledger.span_lag(datetime(2026, 11, 21, 15), datetime(2026, 11, 21, 20), rows)
    text = lagledger.span_text(span)
    assert "2 of 4 measured hour(s) LATE" in text and "11-21 17Z..11-21 19Z" in text
    assert "worst 11-21 19Z p50 2.28 / p99 9.00 s" in text
    assert "1 more with a stalled tail" in text and "6.50 s at 11-21 15Z" in text
    assert "1 of 5 hour(s) UNMEASURED" in text
    block = lagledger.span_block(span)
    assert (block["hours_owed"], block["hours_measured"], block["hours_late"]) == (5, 4, 2)
    assert block["hours_tail"] == 3 and block["worst_p50_s"] == 2.28
    assert block["late_first"] == "2026-11-21 17:00:00"
    # The bar is the boundary: exactly on it is late, just under is not.
    edge = _rows({("2026-11-21", 15): (lagledger.LATE_P50_S, 0.6), ("2026-11-21", 16): (0.49, 0.6)})
    s2 = lagledger.span_lag(datetime(2026, 11, 21, 15), datetime(2026, 11, 21, 17), edge)
    assert [h.hour.hour for h in s2.late] == [15] and not s2.tail


def test_a_clean_span_names_its_worst_hour_and_an_empty_one_is_not_clean() -> None:
    rows = _rows({("2026-11-19", 3): (0.02, 0.5), ("2026-11-19", 4): (0.03, 0.6)})
    clean = lagledger.span_text(
        lagledger.span_lag(datetime(2026, 11, 19, 3), datetime(2026, 11, 19, 5), rows)
    )
    assert "0 of 2 measured hour(s) late (worst median 0.03 s at 11-19 04Z" in clean
    assert "UNMEASURED" not in clean and "stalled" not in clean
    none = lagledger.span_lag(datetime(2026, 11, 23, 3), datetime(2026, 11, 23, 5), rows)
    text = lagledger.span_text(none)
    assert "UNMEASURED -- none of the span's 2 hour(s)" in text and "late" not in text.lower()[:60]
    assert lagledger.span_block(none)["worst_hour"] is None
    # Another channel's rows do not answer for this one.
    other = lagledger.span_lag(
        datetime(2026, 11, 19, 3), datetime(2026, 11, 19, 5), rows, channel="ticker"
    )
    assert not other.measured


# --- the digest row -----------------------------------------------------------


def test_streamlag_reads_the_last_24_closed_hours_and_says_whether_it_is_late_now() -> None:
    rows = _rows({("2026-11-22", 2): (1.74, 9.18), ("2026-11-22", 7): (0.13, 5.1)})
    rows |= _rows({("2026-11-22", 7): (0.16, 4.7), ("2026-11-21", 7): (9.0, 9.0)}, ch="ticker")
    rows |= _rows({("2026-11-22", 8): (5.0, 5.0)})  # the running hour: not in the window
    line = health.stream_lag_line(rows, NOW)
    trades, ticker = line.split(" | ")
    assert trades.startswith("24h: trades connection")
    assert "1 of 2 measured hour(s) LATE" in trades and "22 of 24 hour(s) UNMEASURED" in trades
    assert trades.endswith("newest measured 11-22 07Z p50 0.13 / p99 5.10 s (on time)")
    # 11-21 07Z is 25 h back: outside the ticker's window, so it is not late.
    assert "0 of 1 measured hour(s) late" in ticker and "(on time)" in ticker
    late_now = health.stream_lag_line(_rows({("2026-11-22", 7): (0.9, 5.0)}), NOW)
    assert "(LATE NOW)" in late_now.split(" | ")[0]
    assert "UNMEASURED -- none" in health.stream_lag_line({}, NOW)


def test_the_default_paths_are_read_at_call_time(tmp_path: Path, monkeypatch) -> None:
    """A redirect of the raw-frame tree or the ledger must reach the writer:
    the first suite run with this function wired into the collector read
    production's frames into production's ledger through an imported copy
    of `RAW_FRAMES_DIR`."""
    from collector import rawsizes

    root = tmp_path / "raw"
    _hour_file(root, "2026-11-22", 7, "trades", 0.3)
    monkeypatch.setattr(rawsizes, "RAW_FRAMES_DIR", str(root))
    monkeypatch.setattr(lagledger, "LEDGER", str(tmp_path / "lag.jsonl"))
    assert rawlag.record_closed(now=NOW, every=1) == 1
    assert list(lagledger.load()) == [("2026-11-22", "07", "trades")]


def test_a_span_with_no_hours_is_unmeasured_not_an_error() -> None:
    at = datetime(2026, 11, 22, 7, 30)
    span = lagledger.span_lag(at, at, {})
    assert span.owed == 1 and "UNMEASURED" in lagledger.span_text(span)
