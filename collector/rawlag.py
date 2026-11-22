"""Receive lag per connection, read from one hour of raw frames.

Every Kalshi frame carries `sending_ts_ms`, the venue's own clock at the
moment it SENT the frame, and the raw-frame writer stamps each line with the
box's receive time. Their difference is the transport lag of that connection
-- the path, the venue's send queue for it, and this daemon's loop -- with
the market's own activity taken out, which `recv_ts - ts` (QA's daily trade
reading) cannot do. Measured 2026-11-22 on the 01Z hour, WiFi path saturated
(mistakes #230, #231): every one of 20 books connections ran a median 0.8-2.8
s late and ~10 s at p99, and no row of the digest said so -- the rows were
complete, only late.

    python -m collector.rawlag                 # newest closed hour, books
    python -m collector.rawlag 2026-11-22/01 --channel ticker

Reads the hour file and nothing else: no archive is opened and nothing is
held. A books hour is ~0.7 GB compressed and takes about a minute; `--every
N` parses the timestamps of one line in N (the frame COUNT is exact). The
box's clock offset is inside every number: read the minimum beside the
median -- a negative or large positive floor is the clock, not the path.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from collector.rawsizes import RAW_FRAMES_DIR, SETTLE_S

_SENT_RE = re.compile(r'"sending_ts_ms":(\d+)')
#: Minutes per bucket of the hour's own profile.
BUCKET_MIN = 10


@dataclass(frozen=True)
class ConnLag:
    label: str
    frames: int
    #: Lines whose lag was read (one in `every`, and only those that carry
    #: `sending_ts_ms`), and that lag's quantiles in seconds.
    sampled: int
    min_s: float
    p50_s: float
    p90_s: float
    p99_s: float


@dataclass(frozen=True)
class HourLag:
    frames: int
    conns: tuple[ConnLag, ...]
    #: (minute of the hour the bucket starts at, sampled lines, p50, p99).
    buckets: tuple[tuple[int, int, float, float], ...]


def _q(sorted_vals: list[float], frac: float) -> float:
    return sorted_vals[min(len(sorted_vals) - 1, int(frac * len(sorted_vals)))]


def parse_hour(lines: Iterable[str], every: int = 20, bucket_min: int = BUCKET_MIN) -> HourLag:
    """Pure: raw-frame lines (`recv_iso<TAB>label<TAB>frame`) -> the reading.
    A line that is not three fields is skipped; a frame with no
    `sending_ts_ms` (Polymarket, a truncated tail) is counted, not sampled."""
    frames: dict[str, int] = {}
    lags: dict[str, list[float]] = {}
    by_bucket: dict[int, list[float]] = {}
    for i, line in enumerate(lines):
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        label = parts[1]
        frames[label] = frames.get(label, 0) + 1
        if i % every:
            continue
        m = _SENT_RE.search(parts[2])
        if not m:
            continue
        try:
            recv = datetime.fromisoformat(parts[0])
        except ValueError:
            continue
        lag = recv.timestamp() - int(m.group(1)) / 1000
        lags.setdefault(label, []).append(lag)
        by_bucket.setdefault(recv.minute // bucket_min * bucket_min, []).append(lag)
    conns = []
    for label, n in frames.items():
        vals = sorted(lags.get(label, []))
        if vals:
            conns.append(
                ConnLag(label, n, len(vals), vals[0], _q(vals, 0.5), _q(vals, 0.9), _q(vals, 0.99))
            )
        else:
            conns.append(ConnLag(label, n, 0, 0.0, 0.0, 0.0, 0.0))
    conns.sort(key=lambda c: (-c.p99_s, c.label))
    buckets = []
    for start in sorted(by_bucket):
        vals = sorted(by_bucket[start])
        buckets.append((start, len(vals), _q(vals, 0.5), _q(vals, 0.99)))
    return HourLag(sum(frames.values()), tuple(conns), tuple(buckets))


def render(hour: HourLag, name: str, every: int) -> list[str]:
    """The reading as lines: worst connection first, then the hour's profile."""
    if hour.frames == 0:
        return [f"[rawlag] {name}: no frames"]
    sampled = sum(c.sampled for c in hour.conns)
    out = [
        f"[rawlag] {name}: {hour.frames:,} frames on {len(hour.conns)} connection(s), receive "
        f"time minus the venue's sending_ts_ms on {sampled:,} of them (1 in {every})"
    ]
    for c in hour.conns:
        if c.sampled == 0:
            out.append(f"  {c.label:<10} {c.frames:>10,} frames  no sending_ts_ms: unmeasured")
            continue
        out.append(
            f"  {c.label:<10} {c.frames:>10,} frames {c.frames / hour.frames:5.1%}  lag min "
            f"{c.min_s:.2f} p50 {c.p50_s:.2f} p90 {c.p90_s:.2f} p99 {c.p99_s:.2f} s"
        )
    prof = ", ".join(f":{start:02d} {p50:.2f}/{p99:.2f}" for start, _, p50, p99 in hour.buckets)
    out.append(f"  by {BUCKET_MIN} min, all connections, p50/p99 s: {prof}")
    return out


def newest_closed_hour(now: datetime, root: Path, channel: str) -> Path | None:
    """The newest hour file of `channel` whose hour ended `SETTLE_S` ago."""
    hour = (now - timedelta(seconds=SETTLE_S)).replace(minute=0, second=0, microsecond=0)
    for back in range(1, 49):
        t = hour - timedelta(hours=back)
        path = root / f"{t:%Y-%m-%d}" / f"{t:%H}-{channel}.jsonl.zst"
        if path.exists():
            return path
    return None


def _open_lines(path: Path) -> Iterable[str]:
    from compression import zstd  # stdlib since 3.14, as the writer uses

    with zstd.open(path, "rt", encoding="utf-8", errors="replace") as fh:
        yield from fh


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("hour", nargs="?", help="YYYY-MM-DD/HH (UTC); default the newest closed hour")
    ap.add_argument("--channel", default="books")
    ap.add_argument("--every", type=int, default=20)
    ap.add_argument("--root", default=RAW_FRAMES_DIR)
    args = ap.parse_args(argv)
    root = Path(args.root)
    if args.hour:
        day, _, hh = args.hour.partition("/")
        path: Path | None = root / day / f"{hh}-{args.channel}.jsonl.zst"
    else:
        path = newest_closed_hour(datetime.now(UTC), root, args.channel)
    if path is None or not path.exists():
        print(f"[rawlag] no hour file ({path})", file=sys.stderr)
        return 1
    name = f"{path.parent.name}/{path.name}"
    try:
        hour = parse_hour(_open_lines(path), max(1, args.every))
    except (OSError, EOFError) as exc:
        print(f"[rawlag] {name}: unreadable ({type(exc).__name__}: {exc})", file=sys.stderr)
        return 1
    print("\n".join(render(hour, name, max(1, args.every))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
