"""`collector.rawlag`: receive lag per connection from an hour of raw frames."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from compression import zstd

from collector import rawlag

T0 = datetime(2026, 11, 22, 2, 0, 0, tzinfo=UTC)


def _line(at: datetime, label: str, lag_s: float | None, extra: str = "") -> str:
    sent = "" if lag_s is None else f',"sending_ts_ms":{round((at.timestamp() - lag_s) * 1000)}'
    return f'{at.isoformat()}\t{label}\t{{"type":"orderbook_delta","msg":{{"ts_ms":1}}{sent}}}{extra}\n'


def test_lag_is_receive_time_minus_the_venues_send_time_per_connection() -> None:
    lines = [_line(T0 + timedelta(seconds=i), "books-5", float(i)) for i in range(100)]
    lines += [_line(T0 + timedelta(minutes=15, seconds=i), "books-4", 0.5) for i in range(10)]
    got = rawlag.parse_hour(lines, every=1)
    assert got.frames == 110 and [c.label for c in got.conns] == [
        "books-5",
        "books-4",
    ]  # worst first
    b5, b4 = got.conns
    assert (b5.frames, b5.sampled) == (100, 100)
    assert (b5.min_s, b5.p50_s, b5.p90_s, b5.p99_s) == pytest.approx((0.0, 50.0, 90.0, 99.0))
    assert (b4.min_s, b4.p99_s) == pytest.approx((0.5, 0.5))
    assert [(b[0], b[1]) for b in got.buckets] == [(0, 100), (10, 10)]
    assert got.buckets[1][2:] == pytest.approx((0.5, 0.5))


def test_the_frame_count_is_exact_when_only_one_line_in_n_is_sampled() -> None:
    lines = [_line(T0 + timedelta(seconds=i), "ticker", 2.0) for i in range(100)]
    got = rawlag.parse_hour(lines, every=20)
    assert got.frames == 100 and got.conns[0].sampled == 5
    assert got.conns[0].p50_s == pytest.approx(2.0)


def test_a_frame_without_a_send_time_is_counted_and_never_read_as_zero_lag() -> None:
    lines = [_line(T0, "poly", None), "garbage with no tabs\n", "not-a-time\tx\t{}\n"]
    lines.append('not-a-time\tbooks-1\t{"sending_ts_ms":5}\n')
    got = rawlag.parse_hour(lines, every=1)
    assert got.frames == 3 and all(c.sampled == 0 for c in got.conns) and got.buckets == ()
    text = "\n".join(rawlag.render(got, "d/02-books.jsonl.zst", 1))
    assert (
        "poly" in text
        and "no sending_ts_ms: unmeasured" in text
        and "p50" not in text.split("\n")[1]
    )


def test_render_names_the_file_the_sampling_and_the_hours_profile() -> None:
    lines = [_line(T0 + timedelta(minutes=m), "books-5", 1.25) for m in range(0, 60, 5)]
    out = rawlag.render(rawlag.parse_hour(lines, every=1), "2026-11-22/02-books.jsonl.zst", 1)
    assert out[0] == (
        "[rawlag] 2026-11-22/02-books.jsonl.zst: 12 frames on 1 connection(s), receive time "
        "minus the venue's sending_ts_ms on 12 of them (1 in 1)"
    )
    assert out[1].split() == (
        [
            "books-5",
            "12",
            "frames",
            "100.0%",
            "lag",
            "min",
            "1.25",
            "p50",
            "1.25",
            "p90",
            "1.25",
            "p99",
            "1.25",
            "s",
        ]
    )
    assert out[2].endswith(
        ":00 1.25/1.25, :10 1.25/1.25, :20 1.25/1.25, :30 1.25/1.25, :40 1.25/1.25, :50 1.25/1.25"
    )
    assert rawlag.render(rawlag.parse_hour([]), "x", 20) == ["[rawlag] x: no frames"]


def test_the_newest_closed_hour_skips_the_running_one_and_one_still_settling(tmp_path) -> None:
    for hh in ("00", "01", "02"):
        d = tmp_path / "2026-11-22"
        d.mkdir(exist_ok=True)
        (d / f"{hh}-books.jsonl.zst").write_bytes(b"")
    at = lambda h, m, s=0: datetime(2026, 11, 22, h, m, s, tzinfo=UTC)  # noqa: E731
    assert rawlag.newest_closed_hour(at(2, 30), tmp_path, "books").name == "01-books.jsonl.zst"
    # 02:01:00 is inside the settle window of the 01 hour: the 00 file is the newest final one.
    assert rawlag.newest_closed_hour(at(2, 1), tmp_path, "books").name == "00-books.jsonl.zst"
    assert rawlag.newest_closed_hour(at(2, 30), tmp_path, "ticker") is None


def test_main_reads_a_real_zstd_hour_file_and_fails_plainly_without_one(tmp_path, capsys) -> None:
    d = tmp_path / "2026-11-22"
    d.mkdir()
    with zstd.open(d / "02-books.jsonl.zst", "wt", encoding="utf-8") as fh:
        fh.writelines(_line(T0 + timedelta(seconds=i), "books-7", 3.0) for i in range(40))
    assert rawlag.main(["2026-11-22/02", "--root", str(tmp_path), "--every", "2"]) == 0
    out = capsys.readouterr().out
    assert "40 frames on 1 connection(s)" in out and "on 20 of them (1 in 2)" in out
    assert "p50 3.00" in out
    assert rawlag.main(["2026-11-22/03", "--root", str(tmp_path)]) == 1
    assert "no hour file" in capsys.readouterr().err
