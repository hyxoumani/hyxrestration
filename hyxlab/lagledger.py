"""Receive lag by clock hour, as a ledger any reader of `recv_ts` can cite.

`collector.rawlag` reads one raw-frame hour file and says how late the box
received what the venue sent. That reading lived only in a terminal: on
2026-11-21 17Z..11-22 03Z the WiFi path was saturated, every stream row's
`recv_ts` ran a median 0.8-2.3 s behind the venue for eleven hours, and a
divergence or queue-position report over those hours had no way to say so
(mistakes #231, #232). The 5-minute collector now appends one line per
closed hour file (`collector.rawlag.record_closed`) and this module is the
one reader and the one renderer -- it lives in the shared kernel because
the simulator's reports are the readers that need it and may not import the
collector.

Measured 2026-11-19..22 on the trades connection, p50 by hour: 0.01-0.03 s
on the wire, 0.07-0.10 s on an idle WiFi path, 0.13-0.29 s as the saturated
path drained, 0.77-2.28 s saturated. `LATE_P50_S` sits in the empty band
between the last two. The p99 separates a different thing -- the radio's
300 s background scan put 5-8 s on it in hours whose median stayed 0.02 s --
so the tail is counted apart (`TAIL_P99_S`), never folded into "late".

An hour with no ledger line is UNMEASURED, never clean: the raw copy yields
the disk under 6 GB free (mistakes #227) and writes no file to read.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

LEDGER = "data/rawlag_hours.jsonl"
#: An hour whose median frame arrived this late is LATE (see the docstring).
LATE_P50_S = 0.5
#: An hour whose p99 reached this has a stalled tail (scan stalls, roams).
TAIL_P99_S = 2.0
#: The connection a span is read from by default: one connection, present in
#: every hour, 0.5 s to read -- a proxy for the path, not for each books shard.
DEFAULT_CHANNEL = "trades"

Key = tuple[str, str, str]  # (YYYY-MM-DD, HH, channel)


def load(ledger: str | Path | None = None) -> dict[Key, dict]:
    """Every ledgered hour; the first line for a key wins. A missing ledger
    is empty and a torn line is skipped."""
    out: dict[Key, dict] = {}
    try:
        with open(LEDGER if ledger is None else ledger) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                    out.setdefault((r["day"], r["hh"], r["ch"]), r)
                except (ValueError, KeyError, TypeError):
                    continue
    except OSError:
        pass
    return out


@dataclass(frozen=True)
class HourReading:
    hour: datetime  # naive UTC, start of the clock hour
    p50_s: float
    p99_s: float


@dataclass(frozen=True)
class SpanLag:
    channel: str
    #: Clock hours the span touches -- what a reading was OWED for.
    owed: int
    measured: tuple[HourReading, ...]
    late_p50_s: float = LATE_P50_S
    tail_p99_s: float = TAIL_P99_S

    @property
    def late(self) -> tuple[HourReading, ...]:
        return tuple(h for h in self.measured if h.p50_s >= self.late_p50_s)

    @property
    def tail(self) -> tuple[HourReading, ...]:
        return tuple(h for h in self.measured if h.p99_s >= self.tail_p99_s)


def _naive_utc(t: datetime) -> datetime:
    return t.astimezone(UTC).replace(tzinfo=None) if t.tzinfo else t


def span_lag(
    start: datetime,
    end: datetime,
    rows: dict[Key, dict],
    channel: str = DEFAULT_CHANNEL,
    late_p50_s: float = LATE_P50_S,
    tail_p99_s: float = TAIL_P99_S,
) -> SpanLag:
    """Pure: the ledger's reading of every clock hour `[start, end)` touches.
    An hour with no row, an error row, or no sampled frame is owed and not
    measured."""
    start, end = _naive_utc(start), _naive_utc(end)
    hour = start.replace(minute=0, second=0, microsecond=0)
    owed = 0
    measured: list[HourReading] = []
    while hour < end:
        owed += 1
        r = rows.get((f"{hour:%Y-%m-%d}", f"{hour:%H}", channel))
        if r is not None and r.get("sampled") and "p50" in r and "p99" in r:
            measured.append(HourReading(hour, float(r["p50"]), float(r["p99"])))
        hour += timedelta(hours=1)
    return SpanLag(channel, owed, tuple(measured), late_p50_s, tail_p99_s)


def _hh(h: HourReading) -> str:
    return f"{h.hour:%m-%d %H}Z"


def span_text(span: SpanLag) -> str:
    """The one renderer of a span's receive lag."""
    head = f"{span.channel} connection, hourly from the raw frames: "
    if not span.measured:
        return (
            head + f"UNMEASURED -- none of the span's {span.owed} hour(s) is ledgered"
            " (no raw-frame file, or not read yet); recv_ts lateness is unknown, not zero"
        )
    n = len(span.measured)
    late, tail = span.late, span.tail
    worst = max(span.measured, key=lambda h: (h.p50_s, h.p99_s))
    if late:
        out = (
            f"{len(late)} of {n} measured hour(s) LATE (median frame >= {span.late_p50_s:g} s"
            f" behind the venue's send time), {_hh(late[0])}..{_hh(late[-1])}, worst"
            f" {_hh(worst)} p50 {worst.p50_s:.2f} / p99 {worst.p99_s:.2f} s -- recv_ts in"
            " those hours is the path, not the market"
        )
    else:
        out = (
            f"0 of {n} measured hour(s) late (worst median {worst.p50_s:.2f} s at {_hh(worst)},"
            f" bar {span.late_p50_s:g} s)"
        )
    tail_only = [h for h in tail if h.p50_s < span.late_p50_s]
    if tail_only:
        w = max(tail_only, key=lambda h: h.p99_s)
        out += (
            f"; {len(tail_only)} more with a stalled tail (p99 >= {span.tail_p99_s:g} s, worst"
            f" {w.p99_s:.2f} s at {_hh(w)})"
        )
    if span.owed > n:
        out += f"; {span.owed - n} of {span.owed} hour(s) UNMEASURED (no ledger line)"
    return head + out


def span_block(span: SpanLag) -> dict:
    """The same reading as an artifact block (a report's JSON)."""
    worst = max(span.measured, key=lambda h: (h.p50_s, h.p99_s), default=None)
    return {
        "channel": span.channel,
        "hours_owed": span.owed,
        "hours_measured": len(span.measured),
        "late_p50_s": span.late_p50_s,
        "hours_late": len(span.late),
        "late_first": str(span.late[0].hour) if span.late else None,
        "late_last": str(span.late[-1].hour) if span.late else None,
        "tail_p99_s": span.tail_p99_s,
        "hours_tail": len(span.tail),
        "worst_hour": str(worst.hour) if worst else None,
        "worst_p50_s": worst.p50_s if worst else None,
        "worst_p99_s": worst.p99_s if worst else None,
    }
