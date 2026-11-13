"""The as-written size ledger for raw frame files (mistakes #188).

The sibling's recompressor rewrites closed raw frame files in place; the
collector ledgers each file's size while it is still as the writer left it,
and the digest reads a rewritten file's bytes from that ledger.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from collector import health, rawsizes

NOW = datetime(2026, 11, 9, 8, 15, tzinfo=UTC)
_AS_WRITTEN = b"\x28\xb5\x2f\xfd\x00\x60"  # window 2^22
_LONG27 = b"\x28\xb5\x2f\xfd\x04\x88"  # window 2^27


def _file(root: Path, day: str, name: str, n: int, hdr: bytes = _AS_WRITTEN) -> Path:
    d = root / day
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_bytes(hdr + b"\0" * (n - 6))
    return f


def test_a_closed_as_written_file_is_ledgered_once(tmp_path: Path) -> None:
    raw, ledger = tmp_path / "raw", tmp_path / "sizes.jsonl"
    _file(raw, "2026-11-08", "23-books.jsonl.zst", 1000)
    _file(raw, "2026-11-09", "07-ticker.jsonl.zst", 500)
    assert rawsizes.record_closed(raw, ledger, NOW) == 2
    assert rawsizes.record_closed(raw, ledger, NOW) == 0
    assert rawsizes.load(ledger) == {
        ("2026-11-08", "23", "books"): 1000,
        ("2026-11-09", "07", "ticker"): 500,
    }
    assert len(ledger.read_text().splitlines()) == 2


def test_an_open_or_unsettled_hour_is_not_ledgered(tmp_path: Path) -> None:
    raw, ledger = tmp_path / "raw", tmp_path / "sizes.jsonl"
    _file(raw, "2026-11-09", "08-books.jsonl.zst", 700)  # the running hour at 08:15Z
    assert rawsizes.record_closed(raw, ledger, NOW) == 0
    # 60 s after the hour ends the writer may not have closed it yet.
    assert rawsizes.record_closed(raw, ledger, datetime(2026, 11, 9, 9, 1, tzinfo=UTC)) == 0
    assert rawsizes.record_closed(raw, ledger, datetime(2026, 11, 9, 9, 5, tzinfo=UTC)) == 1


def test_a_file_already_rewritten_is_never_ledgered(tmp_path: Path) -> None:
    raw, ledger = tmp_path / "raw", tmp_path / "sizes.jsonl"
    _file(raw, "2026-11-08", "10-books.jsonl.zst", 400, _LONG27)
    assert rawsizes.record_closed(raw, ledger, NOW) == 0
    assert not ledger.exists()


def test_the_first_size_wins_after_the_rewrite(tmp_path: Path) -> None:
    raw, ledger = tmp_path / "raw", tmp_path / "sizes.jsonl"
    f = _file(raw, "2026-11-08", "10-books.jsonl.zst", 1000)
    rawsizes.record_closed(raw, ledger, NOW)
    f.write_bytes(_LONG27 + b"\0" * 494)  # the recompressor: same name, half
    assert rawsizes.record_closed(raw, ledger, NOW) == 0
    # A forged later line for the same key does not displace the first.
    with ledger.open("a") as fh:
        fh.write(json.dumps({"day": "2026-11-08", "hh": "10", "ch": "books", "b": 500}) + "\n")
        fh.write("{torn\n")
    assert rawsizes.load(ledger) == {("2026-11-08", "10", "books"): 1000}


def test_only_the_newest_scan_days_are_walked_and_a_missing_tree_is_zero(tmp_path: Path) -> None:
    raw, ledger = tmp_path / "raw", tmp_path / "sizes.jsonl"
    assert rawsizes.record_closed(raw, ledger, NOW) == 0
    for dd in range(1, 10):  # 11-01 .. 11-09
        _file(raw, f"2026-11-{dd:02d}", "00-books.jsonl.zst", 100)
    assert rawsizes.record_closed(raw, ledger, NOW) == rawsizes.SCAN_DAYS
    assert min(k[0] for k in rawsizes.load(ledger)) == "2026-11-05"


def test_the_digest_reads_a_rewritten_file_at_its_ledgered_size(tmp_path: Path) -> None:
    raw, ledger = tmp_path / "raw", tmp_path / "sizes.jsonl"
    f = _file(raw, "2026-11-08", "10-books.jsonl.zst", 1000)
    g = _file(raw, "2026-11-08", "11-books.jsonl.zst", 1000)
    rawsizes.record_closed(raw, ledger, NOW)
    f.write_bytes(_LONG27 + b"\0" * 494)
    (day,) = health.read_raw_frames(raw, ledger)
    assert day.by_channel == {"books": 2000} and not day.rewritten
    # No ledger line: the file counts at rest and is named.
    g.write_bytes(_LONG27 + b"\0" * 494)
    ledger.write_text(ledger.read_text().splitlines()[0] + "\n")
    (day,) = health.read_raw_frames(raw, ledger)
    assert day.by_channel == {"books": 1500} and day.rewritten == {("11", "books")}


def test_the_collector_ledgers_from_the_redirected_defaults(tmp_path: Path) -> None:
    # conftest points both constants into tmp_path: the call the cycle makes,
    # with no arguments, must not reach the live tree.
    assert str(tmp_path) in rawsizes.LEDGER and str(tmp_path) in rawsizes.RAW_FRAMES_DIR
    _file(Path(rawsizes.RAW_FRAMES_DIR), "2020-01-01", "00-books.jsonl.zst", 100)
    assert rawsizes.record_closed() == 1
    assert rawsizes.load() == {("2020-01-01", "00", "books"): 100}
