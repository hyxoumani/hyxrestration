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
import json
import re
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from collector import rawsizes
from collector.rawsizes import RAW_FILE_RE, SETTLE_S
from hyxlab import lagledger

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
    #: Every sampled line of the hour as one population (label `*`); None
    #: when no line carried `sending_ts_ms`.
    overall: ConnLag | None = None


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
    every_lag = sorted(v for vals in lags.values() for v in vals)
    overall = (
        ConnLag(
            "*",
            sum(frames.values()),
            len(every_lag),
            every_lag[0],
            _q(every_lag, 0.5),
            _q(every_lag, 0.9),
            _q(every_lag, 0.99),
        )
        if every_lag
        else None
    )
    return HourLag(sum(frames.values()), tuple(conns), tuple(buckets), overall)


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


# --- the per-hour ledger (`hyxlab.lagledger` is its reader) -----------------

#: Channels the 5-minute collector ledgers: one connection each, 0.5 s and
#: ~2 s to read per hour file (measured 2026-11-22 on the 02Z hour). A books
#: hour is 20-60 s of decompression and stays a by-hand reading.
LEDGER_CHANNELS = ("trades", "ticker")
#: Per-call bounds, so the ledger can never cost the collector a cycle: at
#: most this many hour files, and none started after this many seconds. The
#: backlog (15 held days x 24 h x 2 channels) drains newest first over ~15 h.
MAX_FILES = 4
BUDGET_S = 10.0
SCAN_DAYS = 16


def ledger_row(day: str, hh: str, ch: str, hour: HourLag, every: int) -> dict:
    row: dict = {"day": day, "hh": hh, "ch": ch, "frames": hour.frames, "every": every}
    o = hour.overall
    row["sampled"] = o.sampled if o else 0
    if o:
        row.update(
            min=round(o.min_s, 3),
            p50=round(o.p50_s, 3),
            p90=round(o.p90_s, 3),
            p99=round(o.p99_s, 3),
        )
        worst = hour.conns[0]
        row.update(worst=worst.label, worst_p50=round(worst.p50_s, 3))
    return row


def record_closed(
    raw_dir: str | Path | None = None,
    ledger: str | Path | None = None,
    now: datetime | None = None,
    channels: tuple[str, ...] = LEDGER_CHANNELS,
    max_files: int = MAX_FILES,
    budget_s: float = BUDGET_S,
    every: int = 20,
    clock=time.monotonic,
    open_lines=None,
) -> int:
    """Ledger the lag of closed, settled, not-yet-ledgered hour files of
    `channels`, NEWEST first, within `max_files` and `budget_s`. Each row is
    appended as it is read, so a killed cycle keeps what it finished. A file
    that cannot be read is ledgered as an `err` row -- unmeasured to every
    reader, and not re-read every five minutes for the fourteen days it is
    held. Returns the number of lines appended; never raises on a missing
    tree."""
    from compression import zstd  # a corrupt frame raises its own error class

    now = now or datetime.now(UTC)
    path = Path(lagledger.LEDGER if ledger is None else ledger)
    # Read at call time, as the ledger path is: a test's redirect of either
    # constant must reach here, or the suite reads production's frames into
    # production's ledger (it did, once, on this function's first suite run).
    root = Path(rawsizes.RAW_FRAMES_DIR if raw_dir is None else raw_dir)
    opener = _open_lines if open_lines is None else open_lines
    try:
        days = sorted((p for p in root.iterdir() if p.is_dir()), reverse=True)[:SCAN_DAYS]
    except OSError:
        return 0
    have = lagledger.load(path)
    t0 = clock()
    done = 0
    for d in days:
        try:
            entries = sorted(d.iterdir(), reverse=True)
        except OSError:
            continue
        for f in entries:
            m = RAW_FILE_RE.match(f.name)
            if m is None or m.group(2) not in channels or (d.name, m.group(1), m.group(2)) in have:
                continue
            try:
                hour_end = datetime.strptime(f"{d.name} {m.group(1)}", "%Y-%m-%d %H").replace(
                    tzinfo=UTC
                ) + timedelta(hours=1)
            except ValueError:
                continue
            if (now - hour_end).total_seconds() < SETTLE_S:
                continue
            if done >= max_files or clock() - t0 >= budget_s:
                return done
            try:
                row = ledger_row(
                    d.name, m.group(1), m.group(2), parse_hour(opener(f), every), every
                )
            except (OSError, EOFError, ValueError, zstd.ZstdError) as exc:
                row = {"day": d.name, "hh": m.group(1), "ch": m.group(2), "err": type(exc).__name__}
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as fh:
                fh.write(json.dumps(row) + "\n")
            done += 1
    return done


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("hour", nargs="?", help="YYYY-MM-DD/HH (UTC); default the newest closed hour")
    ap.add_argument("--channel", default="books")
    ap.add_argument("--every", type=int, default=20)
    ap.add_argument("--root", default=None, help="default: the raw-frame tree")
    args = ap.parse_args(argv)
    root = Path(args.root or rawsizes.RAW_FRAMES_DIR)
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
