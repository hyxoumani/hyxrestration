"""Stream daemon (B7): live WS capture from both venues into the stream
archive. This data is unrecoverable — neither venue serves historical
books or full prints — so the daemon's one job is: never lose what it saw,
and mark honestly what it missed (stream_gaps).

    python -m collector.streamd [--db data/hyxstream.duckdb]
                             [--watchlist hyxlab/watchlist.json]
                             [--raw-frames data/raw_frames | --raw-frames '']
                             [--env .env]
                             [--smoke SECONDS]   # bounded run, then exit

Connections (each an independent task; one failing never stops others):
- kalshi-trades: exchange-wide trade firehose (~105 ev/s observed).
- kalshi-books-N: orderbook_delta for open markets of the watchlist's
  `kalshi_series` AND `kalshi_book_series` (books only -- the REST
  collector polls only the first), sharded across connections of at most
  `kalshi_book_shard_max` markets (collector/bookcov.py). Since 2026-10-20
  the set follows market_lifecycle_v2 and is edited in place with
  `update_subscription`; a ticker-set change no longer reconnects. REST
  reconciliation every RECONCILE_SECS is the backstop.
- kalshi-lifecycle: exchange-wide market_lifecycle_v2 (stream_lifecycle).
- kalshi-ticker: exchange-wide ticker channel (stream_ticker).
KXMVE* combos are dropped from both tables unless the watchlist opts in
(`kalshi_ticker_include_mve`, `kalshi_lifecycle_include_mve`). Every Kalshi
text frame is also kept verbatim in hourly compressed files
(collector/rawframes.py), off the DuckDB write path.
- poly-books: market channel for watchlist polymarket_pairs tokens;
  skipped while the pair list is empty (pairs land with B3.5).

Gap discipline: a gap row is written for every reconnect, every Kalshi
seq jump, daemon downtime (mark_startup_gap), and — bounded to the first
frame actually received — every Kalshi ws sequence reset ('seq_reset':
each (re)connect starts a fresh sid/seq, and a process restart loses the
in-process last_recv, so the row starts from the last PERSISTED recv_ts;
S-188 loss class 3). Replay treats books as unknown inside gaps until
the next snapshot.

Writes go to the stream store's own DuckDB (see streamstore.py for why
not the main archive), flushed every FLUSH_SECS.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import time as _time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import websockets

from collector.bookcov import SHARD_MAX, BookCoverage, Shard
from collector.rawframes import RETAIN_DAYS as RAW_RETAIN_DAYS
from collector.rawframes import RawFrameWriter
from collector.venues import kalshi, kalshi_ws, polymarket_ws
from hyxlab.lockid import db_owner_lock_or_reason
from hyxlab.memcap import cgroup_sock_throttled
from hyxlab.store import lock_holder
from hyxlab.streamstore import SHARD_SID_STRIDE, StreamStore

FLUSH_SECS = 15.0
STATS_SECS = 300.0
TICKER_REFRESH_SECS = 3600.0  # poly token-set refresh
# Kalshi book-set REST reconciliation, the backstop under the lifecycle
# channel. 15 min, not the old hourly: a 15-minute series lists a market per
# period, so a missed lifecycle frame costs at most one period's book. One
# get_markets call per series, paced by SERIES_PAUSE_S like the old refresh.
RECONCILE_SECS = 900.0
# An added market's book needs a snapshot. If none arrives this long after
# `add_markets` (the docs do not say add_markets sends one), ask for it with
# `get_snapshot`. The counters `books_snap_auto` / `books_snap_requested`
# say which the venue actually does.
SNAPSHOT_WAIT_S = 10.0
# Coalesce lifecycle adds for this long before sending one update command,
# so a burst of listings is one command, not one per market (code 27 is a
# per-subscription command rate limit).
BOOK_BATCH_S = 1.0
# How long a quiet books shard has to answer its liveness probe (one
# `get_snapshot`) before it is declared dead air.
PROBE_WAIT_S = 60.0
# Combos are ~95% of exchange-wide markets (1,217,514 of 1,259,025 traded on
# 2026-10-20) and would dominate the ticker and lifecycle tables. Raw frames
# keep them regardless.
MVE_PREFIX = "KXMVE"
POLY_PING_SECS = 10.0
POLY_TOP_MARKETS = 50  # busiest books streamed even without verified pairs
BACKOFF_MAX = 60.0
# Reconnect a Kalshi channel that goes silent this long. Both channels
# chatter continuously (trades ~105 ev/s exchange-wide; books across
# ~500 open tickers), so real silence means a half-dead connection:
# WS pings keep the TCP session alive while the subscription is gone,
# and recv() otherwise blocks forever — the trades channel came back
# from a 07:00Z reconnect unsubscribed and archived zero trades for
# 75+ minutes with no error (2026-08-13).
DEAD_AIR_SECS = 300.0
# Channels that are legitimately quieter. The exchange-wide lifecycle stream
# is driven by listings and determinations; the 15-minute series alone put
# several on it every 15 minutes, so 20 min of silence is still a dead
# subscription, but 5 min need not be. (A quiet BOOKS shard is probed rather
# than given a longer timeout -- see PROBE_WAIT_S.)
DEAD_AIR_BY_CHANNEL = {"lifecycle": 1200.0}
# Space the per-series ticker-refresh calls apart (see open_tickers).
# 1.0s since 2026-10-23 (was 0.35): the book walk grew from 32 to 76 series
# and a staging boot at 0.35s drew six 429s in ~30s on a key the live
# trader and the hylshi collector share. The same pause now spaces PAGES
# within a series too (KXNCAAFSPREAD alone lists >2,000 open markets).
SERIES_PAUSE_S = float(os.environ.get("HYXLAB_SERIES_PAUSE_S", "1.0"))
# One series' open-market walk: Kalshi's largest /markets page, and enough
# pages that no series this daemon books comes near the end. The old walk
# took get_markets' default 10 x 200 and silently kept the first 2,000.
OPEN_TICKERS_PAGE = 1000
OPEN_TICKERS_MAX_PAGES = 100
# A restart boots the book set from the last reconciliation's result when it
# is at most this old, so a restart costs no REST burst at all; the first
# reconciliation then runs when the cached one would have been due (but not
# sooner than BOOT_RECONCILE_MIN_S, so a crash loop cannot become a walk loop).
TICKER_CACHE_MAX_AGE_S = 3600.0
BOOT_RECONCILE_MIN_S = 120.0
# Retry waits when a book task's subscription set comes back EMPTY (venue
# REST down at boot); the last rung repeats until the set is non-empty.
# Subscribing empty captures nothing — and the hourly in-loop refresh may
# never fire if the venue drops the empty subscription — so neither book
# task ever subscribes with an empty set.
EMPTY_SET_RETRY_LADDER = (10, 30, 60, 120)

# Flush-stall ledger. A failed flush is already journalled, but the journal
# is the WRONG instrument for the question "how long does the backlog get":
# it records only the FAILURES, never the success that ends an episode, so a
# duration read off it is an interpolation between the first and last failure
# line; it rolls at the host's retention (7d here); and it is per-boot-session
# text nothing downstream can aggregate. (The ledger fixes the aggregation and
# the end time; it does NOT give a short hold a duration -- it samples only
# when it has rows to write, and cannot resolve below that spacing, which is
# what `hold_lower_s` and `sample_gap_max_s` publish. A SAMPLE IS A FLUSH THAT
# OPENED THE FILE: a tick with an empty buffer never attaches and so is
# evidence about nothing, which is why the flusher gates `ok()` on
# `FlushOutcome.touched` (EXP-1390, mistakes #103). Nothing here bounds the
# gap between the lock being taken and the first sample that noticed, so an
# episode's span is NOT an upper bound on a hold -- only the holder measures
# that.) Measured over the 7 days to
# 2026-09-11 -- 101 episodes, 76 of them a single failed flush whose ~15s span
# is this ledger's floor rather than a stall length, three ~30 min,
# and TWO of those three reached SPILL_CAP (34,691 and 286 rows moved to the
# sidecar) -- so the tail this ledger exists to bound is real and recurring,
# and the cause is a long-lived READ-ONLY reader: a duckdb read-only handle
# takes a shared lock the writer cannot upgrade past (the 09-09 and 09-10
# episodes name `simulator.shadow` as the holder).
STALL_LOG = "data/stream_stalls.jsonl"
# A stall that outlives the daemon (OOM, restart, host crash) must still be
# on disk, so an episode past this age writes an interim `open` record and
# refreshes it at this cadence. Short episodes -- the 75% that are one failed
# flush -- write exactly one `closed` record and nothing else, so the quiet
# case stays quiet. 300s is well inside the ~30 min tail above and 20x
# FLUSH_SECS, i.e. it cannot fire for the common case.
STALL_HEARTBEAT_S = 300.0

# Wall-clock the SHUTDOWN drain is allowed to spend flushing, and the reason
# a shutdown path needs one at all: systemd stops this unit with SIGTERM and
# SIGKILLs whatever is left `TimeoutStopSec` later. Until 2026-09-18 the unit
# set no `TimeoutStopSec`, so that deadline was systemd's default 90s -- a
# number nothing in this repo had written down, let alone budgeted against.
#
# The drain's cost is `rows / StreamStore.FLUSH_ROWS_PER_S` (7,100/s,
# measured), so 90s bought ~639,000 rows, and the drain's OWN bound is much
# larger than that: `flush()` writes the buffer AND the whole sidecar in one
# transaction, and the sidecar is unbounded -- the 7h poly-sweep wedge its
# own comment prices is 6.3M rows, ~890s, 10x the deadline. Measured
# end-to-end at 2M rows: 285.6s.
#
# What a SIGKILL mid-drain costs is the point. DuckDB rolls the transaction
# back and the sidecar survives (it is unlinked only after commit) -- but
# `flush()` has already moved the in-memory buffers into locals by then, and
# SIGKILL runs no `except BaseException` to put them back and no `spill_all`
# to save them. Up to SPILL_CAP rows (~27 min of tape) die with no gap row
# to mark the hole, which is precisely the loss `_final_drain` exists to
# prevent. So the drain now REFUSES a flush it cannot finish and spills
# instead: `spill_all` moved the same 400,000 rows in 1.11s against
# `flush()`'s 56.46s (51x), is equally lossless, and the next boot drains
# the sidecar ahead of the buffer, by design.
#
# 90s is a deliberate 1.6x over the in-memory worst case (SPILL_CAP/
# FLUSH_ROWS_PER_S = 56.3s), so a full buffer ALWAYS flushes and only a
# sidecar backlog can trip the refusal. `TimeoutStopSec=180` in the unit is
# 2x this, leaving the rest of the stop path -- websocket close, task
# cancellation, a periodic flush still in flight on its worker thread -- a
# budget of its own. `tests/test_drain_budget.py` pins both ratios.
#
# 2026-10-23, coverage expansion: the figures above are the `executemany`
# era. The staged-CSV insert raised FLUSH_ROWS_PER_S to 100,000 (half the
# slowest temp-DB reading; see its comment), so a SPILL_CAP buffer (2M
# rows since the same date) flushes in ~20s and 90s buys ~9M rows. The budget is deliberately NOT cut
# to match: the new rate is a temp-database measurement until it is
# re-measured on the live file, and an over-generous budget only means a
# drain is attempted that would have been declined -- inside a
# TimeoutStopSec that still leaves 90s for the rest of the stop path,
# including the raw-frame writer's close (RAW_CLOSE_S).
DRAIN_BUDGET_S = 90.0

# How long the shutdown drain will wait for a PERIODIC flush to get out of
# its way, out of the budget above.
#
# `flusher()` runs `store.flush()` via `asyncio.to_thread`, and `run()`'s
# `finally` cancels that task before calling `_final_drain`. Cancelling a
# task awaiting `to_thread` returns in 0.00s while the worker thread keeps
# running -- it is joined only at interpreter exit (measured 2026-09-18).
# So without a lock the drain writes CONCURRENTLY with a flush already in
# flight, on one buffer and one sidecar: measured, 400 sidecar rows became
# 800 archived rows, and a 60-row `spill_all` was deleted by the in-flight
# flush's post-commit unlink -- absent from archive, sidecar and buffer
# alike, with the gap rows that would have marked the hole sitting in the
# same lost batch. `StreamStore._flush_lock` closes that; this is the
# drain's half, because it CANNOT wait forever -- SIGKILL arrives
# `TimeoutStopSec` after SIGTERM whether the flusher finished or not.
#
# The wait is charged against DRAIN_BUDGET_S rather than added to it: a
# drain that waited 30s has 30s less to write in, since the deadline runs
# from SIGTERM. 30s leaves 60s = 6,000,000 rows at the post-2026-10-23
# rate (426,000 at the old one), still over the SPILL_CAP buffer (20s at
# 2M rows; 56.3s at 400k then) the drain is REQUIRED to flush, so waiting never turns an
# ordinary restart into a sidecar handoff. `tests/test_flush_concurrency.py`
# pins that composition and the unit's `TimeoutStopSec` covers the sum.
DRAIN_LOCK_WAIT_S = 30.0

# The raw-frame writer's close on the stop path: drain its queue and close
# the hour's files. Runs AFTER the final drain, inside the TimeoutStopSec
# margin over DRAIN_BUDGET_S (180 - 90 s). The queue is bounded at 64 MB of
# frame text, which zstd compresses in well under a second, so this is a
# ceiling for a stalled disk, not an expected duration.
RAW_CLOSE_S = 10.0


def load_env(path: str | Path = ".env") -> None:
    """Minimal .env loader: KEY=VALUE lines, no quoting; existing
    environment wins (systemd EnvironmentFile= takes this same file)."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def _log(msg: str) -> None:
    print(f"[streamd] {datetime.now(UTC):%H:%M:%S} {msg}", flush=True)


class SockThrottleWatch:
    """The cgroup's `sock_throttled` counter, as a DELTA per stats line.

    `scripts/cgmem.sh` prints the counter once, at stop, as a total over the
    whole run: `hyxlab-stream`'s 2026-10-10 .. 10-14 run (3d18h) left
    `sock_throttled=441` and the next run (5h56m) left 0, and nothing can
    say WHEN the 441 happened -- during the 02:48Z hold that buffered
    393,435 rows, during a sidecar drain, or spread over four days. The
    kernel throttles a cgroup's sockets when the cgroup is under memory
    pressure (ops.md: this unit lives at its cap against page cache), and a
    throttled socket on a daemon whose job is to RECEIVE is events arriving
    late or a venue closing a slow consumer -- the cost that `max` reclaim
    events alone say nothing about. So the daemon reads its own counter at
    every stats line and publishes the increment beside the event counts it
    may have cost, the way a stall is ledgered next to the flush it failed:
    measured at the victim, with a timestamp the victim holds.

    Unreadable is `?`, never 0 -- a `--smoke` run outside a cgroup, or a
    kernel without the key, must not read as "no throttling".
    """

    def __init__(self, reader: Callable[[], int | None] = cgroup_sock_throttled) -> None:
        self._reader = reader
        self.last: int | None = None

    def note(self) -> tuple[str, int]:
        """`(suffix for the stats line, increment since the last call)`.
        The first call's increment is 0: the counter's past belongs to the
        previous stats period, which was never read, not to this one."""
        cur = self._reader()
        if cur is None:
            self.last = None
            return "sock_throttled=?", 0
        delta = 0 if self.last is None else max(0, cur - self.last)
        self.last = cur
        return f"sock_throttled={cur} (+{delta})", delta


def _venue_clock_note(exc: BaseException, now: datetime) -> str:
    """For a REJECTED handshake, the box clock against the venue's `Date`.

    The 2026-10-05 kalshi outage (mistakes #108) was 56 minutes of
    `HTTP 401` on every handshake from a box clock +41.3s fast, and the
    journal said only "401" -- the cause was found from the timesyncd step
    that ended it. A rejection still carries the venue's `Date` header
    (CloudFront sets it on the 401), so the next skew outage names itself.

    `Date` is WHOLE seconds, floored at the server, and `now` is read after
    the response crossed the network, so `now - Date` is an UPPER bound on
    the box's lead and the lead is at least that minus 1s minus the RTT.
    Published as that interval, never as a bare offset. "" when there is
    no response or no parseable `Date` -- absence is not a zero."""
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None)
    raw = headers.get("Date") if headers is not None else None
    if not raw:
        return ""
    try:
        venue = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return ""
    if venue.tzinfo is None:
        return ""
    off = (now - venue).total_seconds()
    return f" [box - venue Date {off:+.1f}s; box lead in [{off - 1:+.1f}s - rtt, {off:+.1f}s]]"


async def _fetch_until_nonempty(fetch, channel: str, what: str):
    """Run `fetch` in a thread, retrying on EMPTY_SET_RETRY_LADDER (last
    rung repeats forever) until the result is non-empty."""
    items = await asyncio.to_thread(fetch)
    rung = 0
    while not items:
        wait = EMPTY_SET_RETRY_LADDER[min(rung, len(EMPTY_SET_RETRY_LADDER) - 1)]
        _log(f"{channel}: empty {what}; retrying in {wait}s")
        await asyncio.sleep(wait)
        items = await asyncio.to_thread(fetch)
        rung += 1
    return items


def open_tickers_detail(
    series_list: list[str], pause_s: float = SERIES_PAUSE_S
) -> tuple[set[str], set[str]]:
    """Open market tickers for the given series (REST), and the series whose
    walk did NOT complete -- raised, or ran out of pages with the cursor
    still live. A reconciliation must not read an incomplete series as
    "these markets closed": that is how a 429 would unsubscribe a series.

    `pause_s` spaces the calls APART and, passed through to get_markets,
    the pages within one series. The burst is what matters: measured
    2026-08-02 at 23 series the refresh drew 429s from /markets
    (KXPAYROLLS, KXU3), and on 2026-10-23 a 76-series staging boot at
    0.35s drew six in ~30s. kalshi._get_with_429_retry recovers from
    those, but recovering from a burst we chose to send is worse than not
    sending it: retries land in the same window and the walk stretches out
    anyway, on a key other processes are trading on."""
    out: set[str] = set()
    incomplete: set[str] = set()
    for i, s in enumerate(series_list):
        if i and pause_s:
            _time.sleep(pause_s)
        try:
            markets, truncated = kalshi.get_markets(
                series_ticker=s,
                status="open",
                limit=OPEN_TICKERS_PAGE,
                max_pages=OPEN_TICKERS_MAX_PAGES,
                pause_s=pause_s,
                with_truncated=True,
            )
        except Exception as exc:  # one bad series must not sink the refresh
            _log(f"kalshi-books: ticker refresh failed for {s}: {exc}")
            incomplete.add(s)
            continue
        out.update(m["ticker"] for m in markets)
        if truncated:
            _log(f"kalshi-books: {s} walk TRUNCATED at {len(markets)} markets")
            incomplete.add(s)
    return out, incomplete


def open_tickers(series_list: list[str], pause_s: float = SERIES_PAUSE_S) -> set[str]:
    """`open_tickers_detail` without the completeness half."""
    return open_tickers_detail(series_list, pause_s)[0]


class FlushStalls:
    """Append-only ledger of flush-stall EPISODES (see STALL_LOG).

    One episode = the span from the first failed flush to the next successful
    one. The daemon is the only process that can see both ends, which is the
    whole reason this is written here and not derived by a reader from the
    journal.

    Two record shapes, and the difference is load-bearing:

      closed  the episode ENDED at `at`; `duration_s` is the exact SPAN
              between the two ledger events. That is not the hold, and the
              difference is the instrument's sampling period -- read
              `hold_lower_s`/`period_s` in `_record` before comparing
              durations, especially at the short end where the span is a
              constant.
      open    the episode had lasted `duration_s` as of `at` and the daemon
              had not yet seen it end. It is a LOWER BOUND, never a claim
              that the stall is still running now -- the daemon may have been
              killed a second later, and a reader that treats `open` as
              "ongoing" would report a stall that ended weeks ago as live.

    An episode that ends normally therefore writes `closed` whether or not it
    also wrote heartbeats, and a reader keyed on `started` takes the longest
    record it has for that episode.

    Writes are best-effort: an unwritable ledger must never take down the
    daemon whose one job is not losing what it saw.
    """

    #: Distinct holders retained per episode. Two is the most any episode in
    #: the 250-episode ledger has ever shown; the headroom is for a stall long
    #: enough to outlive several readers, and the bound is what keeps one
    #: record from growing with the stall's duration.
    HOLDERS_MAX = 8

    def __init__(self, path: str | Path | None = None, period_s: float = FLUSH_SECS) -> None:
        # Resolved at WRITE time, never bound here: STALL_LOG is relative to
        # the working directory and the suite patches it (the same default-arg
        # trap `collector.collect.acquire_writer_lock` documents).
        self._path = path
        # The SAMPLING PERIOD this ledger observes holds through, recorded on
        # every episode so a reader can tell a measurement from a bound (see
        # `_record`). Injected rather than read off the module at write time
        # because a record's resolution is whatever the flusher ran at when
        # the episode happened, not whatever the constant says today.
        self._period_s = period_s
        self.started: datetime | None = None
        self.fails = 0
        # The LAST failed attempt of the open episode, and the widest spacing
        # between two consecutive ones. Both are measured; `period_s` above is
        # only what the flusher SLEEPS, and the loop is `sleep(period_s)` THEN
        # the attempt, so the observed spacing is that plus the attempt's own
        # cost plus event-loop lag. Measured 2026-09-29 over the 21 failures of
        # the 02:34:45Z episode: 15.2 s to 93.0 s apart, mean 24.2 s -- so a
        # bound derived from the constant is off by up to 6x (EXP-1390).
        self.last_fail_at: datetime | None = None
        self.sample_gap_max_s = 0.0
        self.peak_pending = 0
        self.spilled = 0
        # The last failure's message, carried onto the CLOSED record too: an
        # episode that ended is the one an operator reads after the fact, and
        # "30 minutes" without "who held the lock" is half the finding.
        self.last_error = ""
        # Distinct lock holders seen during the open episode, resolved to a
        # systemd unit at the moment of the collision (see `observe`).
        self.holders: list[str] = []
        self._last_written: datetime | None = None

    def _write(self, rec: dict) -> None:
        path = Path(self._path or STALL_LOG)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
        except OSError as exc:
            _log(f"stall ledger unwritable ({type(exc).__name__}: {exc})")

    def _record(self, state: str, now: datetime, **extra) -> None:
        assert self.started is not None
        self._write(
            {
                "at": now.isoformat(),
                "state": state,
                "started": self.started.isoformat(),
                "duration_s": round((now - self.started).total_seconds(), 1),
                "fails": self.fails,
                # THE RESOLUTION OF THE INSTRUMENT, and why `duration_s` is not
                # a hold. This ledger learns the archive is unwritable only by
                # ATTEMPTING a flush, and it attempts one every `period_s`. So
                # an episode's span is the hold ROUNDED UP to the next sampling
                # point, and the rounding error is the whole quantity for a
                # short hold: measured 2026-09-27 over this ledger's 275 closed
                # episodes, 254 of them (92%) have `fails == 1` and a span of
                # 18.56s +/- 1.63s -- a CONSTANT, `period_s` plus the ending
                # flush's own duration. Three holds that morning of ~0.1s,
                # 7.141s (the holder's own `held_s`, mistakes #91) and ~0.1s
                # all logged ~19.3s. Nothing distinguished them here.
                #
                # `period_s` IS THE NOMINAL CADENCE AND NOT THE OBSERVED ONE.
                # The flusher sleeps `period_s` and THEN attempts, so a real
                # spacing is that plus the attempt's own cost plus event-loop
                # lag: measured 2026-09-29 over the 21 failures of the
                # 02:34:45Z episode, 15.2 s to 93.0 s apart, mean 24.2 s
                # (EXP-1390). `sample_gap_max_s` is the widest spacing this
                # episode actually saw -- the resolution of the instrument,
                # measured, which is what a reader must difference against.
                "period_s": self._period_s,
                "sample_gap_max_s": round(self.sample_gap_max_s, 1),
                # THE HOLD BOUND IS NOW MEASURED, NOT DERIVED. The file was
                # unwritable at the first failed attempt and at the last, so
                # the hold spanned at least the interval BETWEEN THEM -- two
                # timestamps this ledger holds, and no constant. It used to be
                # `(fails - 1) * period_s`, which read 300.0 s for that same
                # 02:34:45Z episode against a measured 483.1 s: safe, being a
                # lower bound, and wrong by 183 s of a 13-minute exclusion.
                # For the 92% single-fail case both give 0.0 -- the honest
                # reading, and the reason this field is published instead of
                # left to the reader: a single-fail episode bounds the hold
                # ABOVE and says nothing else.
                "hold_lower_s": round(self._hold_lower_s(), 1),
                "peak_pending": self.peak_pending,
                "spilled": self.spilled,
                "error": self.last_error,
                # Always present, empty list included: "nothing named a live
                # holder" is a reading (a disk error, or a lock left behind by
                # a dead process), and a key that vanishes in that case would
                # be indistinguishable from an old record written before the
                # field existed.
                "holders": list(self.holders),
                **extra,
            }
        )
        self._last_written = now

    def _hold_lower_s(self) -> float:
        """Seconds the archive is PROVEN to have been unwritable: the span
        from the first failed attempt to the last. 0.0 for a single-fail
        episode, which proves nothing below the sampling gap."""
        if self.started is None or self.last_fail_at is None:
            return 0.0
        return max(0.0, (self.last_fail_at - self.started).total_seconds())

    def arm(self, now: datetime) -> None:
        """Announce that the producer is alive, from now.

        WHY A LEDGER NEEDS AN EPOCH. The reader decides an EMPTY ledger by
        asking the journal whether any flush failed; without an epoch, every
        failure the journal remembers from BEFORE this code was deployed --
        or from before the daemon was restarted onto it -- counts as evidence
        that the producer is dead. The first QA run after any deployment
        would read INERT on a perfectly healthy daemon, which is the alarm a
        producer-liveness check exists to make believable.

        Written on every start and never rotated away by the reader: a daemon
        up for 30 days must still be able to say when it armed, so this record
        is looked up OUTSIDE the reader's window.
        """
        self._write({"at": now.isoformat(), "state": "armed"})

    def observe(self, pending: int, spilled: int, exc: BaseException, now: datetime) -> None:
        """Account one failed flush against the episode, writing nothing.

        Split out of `failed` because the shutdown path needs the accounting
        WITHOUT the heartbeat: `interrupted` writes its own record at the
        same instant, and two records for one episode at the same duration
        leave the reader (which keeps the LONGEST per `started`) picking
        between them on a tie -- where the one it must not pick is the one
        without the handoff count.

        THE HOLDER IS RESOLVED HERE AND CAN BE RESOLVED NOWHERE ELSE. The
        name lives in `/proc/<pid>/cgroup`, which stops existing when the
        holder exits -- so an episode whose holder is not named at the
        collision can never be attributed afterwards. It was not, and the
        cost came due on 2026-09-25: a 3,057 s stall spilled 387,856 rows,
        and the unit responsible was named a pass later by eliminating the
        timers that had NOT fired in the window. That inference happened to
        be right (`hyxlab-divergence`, PID 3026564, confirmed against the
        journal), and it could not have separated any of the three long
        holders this box actually has -- the divergence replay, the shadow
        daemon, and an autonomous pass's own probes -- because DuckDB names
        the holder's EXECUTABLE and all three are `/usr/bin/python3.14`.
        """
        if self.started is None:
            self.started = now
            self.fails = 0
            self._last_written = None
            self.last_fail_at = None
            self.sample_gap_max_s = 0.0
        if self.last_fail_at is not None:
            gap = (now - self.last_fail_at).total_seconds()
            self.sample_gap_max_s = max(self.sample_gap_max_s, gap)
        self.last_fail_at = now
        self.fails += 1
        self.last_error = _short_err(exc)
        # DISTINCT and first-seen order, because a holder CHANGES mid-episode:
        # the 09-20 20:19Z episode was held by PID 2828640 at 300 s and by
        # 2830076 at 600 s, so a single-valued field would have reported
        # whichever end of the stall the reader happened to look at. Capped
        # so a pathological episode cannot grow the record without bound --
        # and the cap also ends the per-failure /proc lookups once reached.
        if len(self.holders) < self.HOLDERS_MAX:
            # Best-effort exactly as the ledger's writes are, and for a
            # stronger reason: the lookup reads a filesystem that is racing
            # the holder's exit, from inside the flush-FAILURE path of the
            # daemon whose one job is not losing what it saw. It may cost an
            # episode its name; it may not cost the tape.
            try:
                who = lock_holder(exc)
            except Exception as exc2:  # noqa: BLE001 — see above
                _log(f"stall holder unresolvable ({type(exc2).__name__}: {exc2})")
                who = None
            if who and who not in self.holders:
                self.holders.append(who)
        self.peak_pending = max(self.peak_pending, pending)
        self.spilled = max(self.spilled, spilled)

    def failed(self, pending: int, spilled: int, exc: BaseException, now: datetime) -> None:
        """One failed flush. Opens an episode if none is open."""
        self.observe(pending, spilled, exc, now)
        assert self.started is not None
        since = self._last_written or self.started
        if (now - self.started).total_seconds() >= STALL_HEARTBEAT_S and (
            now - since
        ).total_seconds() >= STALL_HEARTBEAT_S:
            self._record("open", now)

    def interrupted(self, now: datetime, handoff: int = 0, spilled: int | None = None) -> None:
        """Record an episode this process will not see the end of.

        Written `open` and never `closed`: the drain that would have ended
        the stall is the thing that just failed, so the stall outlived the
        daemon and `duration_s` is a lower bound. Forced regardless of
        `STALL_HEARTBEAT_S` — shutdown is the last chance this episode has
        to be written down at all, and a 20 s stall that ate a restart is
        exactly the one no heartbeat would ever have reached.

        `handoff` is the slice of `spilled` that went to the sidecar at
        SHUTDOWN rather than at SPILL_CAP. The reader needs the two apart: a
        cap spill says a stall ran an hour and the daemon is at its memory
        bound, a condition to fix; a handoff says a restart landed during a
        stall and the sidecar caught what the process would otherwise have
        dropped, which is the design working. Failing on the second would
        raise the alarm exactly when the safety net held. Carried as a count
        beside `spilled`, not as a separate state, so `spilled` stays the
        total that left memory.
        """
        if self.started is None:
            return
        # `spilled` on this record is the ledger's copy, advanced only by
        # `observe` -- which the DECLINED and SKIPPED branches never call
        # (no flush failed there). Without this, a handoff of N rows was
        # recorded beside a `spilled` that did not include it, and QA's
        # `spilled - handoff` read a design-working handoff as negative cap
        # rows. The FAILED branch observes first and passes nothing.
        if spilled is not None:
            self.spilled = max(self.spilled, spilled)
        self._record("open", now, interrupted=True, handoff=handoff)

    def ok(self, now: datetime) -> None:
        """A flush succeeded. Closes an open episode; a no-op otherwise."""
        if self.started is None:
            return
        self._record("closed", now)
        self.started = None
        self.peak_pending = 0
        self.spilled = 0
        self.last_error = ""
        self.holders = []


def _short_err(exc: BaseException) -> str:
    """First line of the exception, bounded. DuckDB's lock error is a
    paragraph with a URL in it; the ledger needs the class and the holder,
    not the manual."""
    first = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    return f"{type(exc).__name__}: {first}"[:200]


class Daemon:
    def __init__(
        self, store: StreamStore, watchlist: dict, raw: RawFrameWriter | None = None
    ) -> None:
        self.store = store
        self.watchlist = watchlist
        # Raw-frame copy; None (tests, `--raw-frames ''`) keeps nothing.
        self.raw = raw
        self.books: BookCoverage | None = None
        self._book_tasks: dict[int, asyncio.Task] = {}
        self._book_wake: asyncio.Event | None = None
        self._reconcile_now = False
        self._cmd_id = 1  # 1 is every connection's subscribe
        self.key_id = os.environ.get("KALSHI_API_KEY_ID", "")
        pem_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
        self.pem = Path(pem_path).read_bytes() if pem_path and Path(pem_path).exists() else b""
        self.stats: dict[str, int] = {}
        self.sock_throttle = SockThrottleWatch()
        self._spill_corrupt_seen = 0
        self.stalls = FlushStalls()

    def _count(self, key: str, n: int) -> None:
        self.stats[key] = self.stats.get(key, 0) + n

    def _gap(self, venue: str, channel: str, since: datetime | None, reason: str) -> None:
        now = datetime.now(UTC)
        self.store.append_gap(venue, channel, since or now, now, reason)
        _log(f"{venue}/{channel} GAP ({reason})")

    def _clock_check(self, venue: str, channel: str, recv_ts: datetime, last: datetime | None):
        """recv_ts moving backwards = the system clock stepped (e.g. NTP
        kicking in on a skewed box). Not lost coverage, but timestamps
        around the step are non-monotonic — record it so replay knows."""
        if last is not None and recv_ts < last:
            self._gap(
                venue, channel, recv_ts, f"clock_step_{(recv_ts - last).total_seconds():.1f}s"
            )

    # -- connection loops --------------------------------------------------

    async def kalshi_trades(self) -> None:
        await self._kalshi_loop("trades", lambda: kalshi_ws.subscribe_trades(), None)

    async def kalshi_lifecycle(self) -> None:
        await self._kalshi_loop("lifecycle", lambda: kalshi_ws.subscribe_lifecycle(), None)

    async def kalshi_ticker(self) -> None:
        await self._kalshi_loop("ticker", lambda: kalshi_ws.subscribe_ticker(), None)

    def _book_series(self) -> list[str]:
        """Books-only series join the REST-polled ones. `kalshi_book_series`
        exists so the stream can carry a series' books WITHOUT the collector
        (which reads only `kalshi_series`) starting to poll it."""
        return sorted(
            set(self.watchlist.get("kalshi_series", []))
            | set(self.watchlist.get("kalshi_book_series", []))
        )

    def _include_mve(self, table: str) -> bool:
        return bool(self.watchlist.get(f"kalshi_{table}_include_mve", False))

    def _wake_books(self) -> None:
        if self._book_wake is not None:
            self._book_wake.set()

    async def kalshi_books(self) -> None:
        """Own the book set: boot it from REST, follow the lifecycle channel,
        reconcile against REST every RECONCILE_SECS, and keep one connection
        task per shard. Returns only when every shard's loop has returned on
        its own (missing credentials) -- a shard closed for being empty is
        not that."""
        series = self._book_series()
        if not series:
            _log("kalshi-books: no series in watchlist; task idle")
            return
        # An empty INITIAL set (REST down at boot) would leave books dark
        # until the next reconciliation — keep retrying until tickers exist.
        mono = asyncio.get_event_loop().time
        cached, age = self._read_ticker_cache(series)
        if cached:
            tickers = cached
            next_reconcile = mono() + max(BOOT_RECONCILE_MIN_S, RECONCILE_SECS - age)
            _log(f"kalshi-books: booting from the {age:.0f}s-old open-ticker cache")
        else:
            tickers = await _fetch_until_nonempty(
                lambda: open_tickers(series), "kalshi-books", "initial ticker set"
            )
            next_reconcile = mono() + RECONCILE_SECS
            self._write_ticker_cache(series, set(tickers))
        cov = BookCoverage(set(series), int(self.watchlist.get("kalshi_book_shard_max", SHARD_MAX)))
        self.books = cov
        self._book_wake = asyncio.Event()
        self._reconcile_now = False
        for sh in cov.boot(set(tickers)):
            self._start_shard(sh)
        _log(
            f"kalshi-books: {len(tickers)} open tickers across {len(series)} series"
            f" on {len(cov.shards)} connection(s) (<= {cov.shard_max} each)"
        )
        while True:
            live = [t for t in self._book_tasks.values() if not t.done()]
            if self._book_tasks and not live:
                return
            wait_s = max(0.0, next_reconcile - mono())
            if cov.pending_open:
                wait_s = min(wait_s, 1.0)
            waker = asyncio.ensure_future(self._book_wake.wait())
            done, _ = await asyncio.wait(
                [*live, waker], timeout=wait_s, return_when=asyncio.FIRST_COMPLETED
            )
            waker.cancel()
            if waker in done:
                self._book_wake.clear()
                await asyncio.sleep(BOOK_BATCH_S)  # coalesce a burst of listings
            cov.due_opens()
            if mono() >= next_reconcile or self._reconcile_now:
                self._reconcile_now = False
                next_reconcile = mono() + RECONCILE_SECS
                rest, incomplete = await asyncio.to_thread(open_tickers_detail, series)
                if rest:
                    cov.reconcile(set(rest), _time.monotonic(), incomplete)
                else:
                    _log("kalshi-books: reconciliation read nothing; keeping the held set")
                await self._apply_books()
                if rest and not incomplete:
                    self._write_ticker_cache(series, cov.held)
                continue
            await self._apply_books()

    def _ticker_cache_path(self) -> Path:
        return Path(f"{self.store.path}.book_tickers.json")

    def _read_ticker_cache(self, series: list[str]) -> tuple[set[str], float]:
        """(tickers, age_s) from the last complete walk, or (empty, 0) when
        there is none, it is older than TICKER_CACHE_MAX_AGE_S, or it was
        written for a different series list. Never raises."""
        try:
            rec = json.loads(self._ticker_cache_path().read_text())
            age = (datetime.now(UTC) - datetime.fromisoformat(rec["at"])).total_seconds()
            if rec.get("series") == sorted(series) and 0 <= age <= TICKER_CACHE_MAX_AGE_S:
                return set(rec["tickers"]), age
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return set(), 0.0

    def _write_ticker_cache(self, series: list[str], tickers: set[str]) -> None:
        path = self._ticker_cache_path()
        rec = {
            "at": datetime.now(UTC).isoformat(),
            "series": sorted(series),
            "tickers": sorted(tickers),
        }
        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(rec))
            os.replace(tmp, path)
        except OSError as exc:
            _log(f"kalshi-books: open-ticker cache unwritable ({exc})")

    def _start_shard(self, sh: Shard) -> None:
        self._book_tasks[sh.idx] = asyncio.create_task(
            self._kalshi_loop(
                "books", lambda sh=sh: kalshi_ws.subscribe_books(sorted(sh.assigned)), sh
            ),
            name=f"kalshi-{sh.label}",
        )

    async def _apply_books(self) -> None:
        cov = self.books
        if cov is None:
            return
        adds, removes = cov.take()
        if adds or removes:
            touched, created, emptied = cov.apply(adds, removes)
            for sh in emptied:
                # Deliberate close, not a failure: no gap row. Nothing it
                # carried is wanted any more.
                task = self._book_tasks.pop(sh.idx, None)
                if task is not None:
                    task.cancel()
                _log(f"kalshi-{sh.label}: no markets left; connection closed")
            for sh in created:
                self._start_shard(sh)
                _log(f"kalshi-{sh.label}: opened for {len(sh.assigned)} market(s)")
            for idx in touched:
                sh = cov.find(idx)
                if sh is not None and sh not in created:
                    await self._sync_shard(sh)
        self.stats["books_shards"] = len(cov.shards)
        self.stats["books_held"] = len(cov.held)

    def _next_cmd(self) -> int:
        self._cmd_id = getattr(self, "_cmd_id", 1) + 1
        return self._cmd_id

    async def _sync_shard(self, sh: Shard) -> None:
        """Send the shard the difference between what it should carry and
        what the venue was told. Nothing to do until the connection's sid is
        known: the reconnect path subscribes the whole assigned set."""
        if sh.ws is None or sh.sid is None:
            return
        mono = asyncio.get_event_loop().time
        for action, tickers in (
            ("delete_markets", sh.subscribed - sh.assigned),
            ("add_markets", sh.assigned - sh.subscribed),
        ):
            if not tickers:
                continue
            cid = self._next_cmd()
            sh.pending[cid] = (action, set(tickers))
            try:
                await sh.ws.send(
                    kalshi_ws.update_subscription(sh.sid, sorted(tickers), action, cid)
                )
            except Exception as exc:  # the recv side fails too and reconnects
                _log(f"kalshi-{sh.label}: {action} send failed ({_short_err(exc)})")
                return
            if action == "add_markets":
                sh.subscribed |= tickers
                due = mono() + SNAPSHOT_WAIT_S
                sh.awaiting.update(dict.fromkeys(tickers, due))
                self._count("books_added", len(tickers))
            else:
                sh.subscribed -= tickers
                for t in tickers:
                    sh.awaiting.pop(t, None)
                self._count("books_removed", len(tickers))

    async def _request_snapshots(self, sh: Shard, now: float) -> None:
        late = sorted(t for t, due in sh.awaiting.items() if due <= now)
        if not late or sh.ws is None or sh.sid is None:
            return
        for t in late:
            del sh.awaiting[t]
        cid = self._next_cmd()
        sh.pending[cid] = ("get_snapshot", set(late))
        with contextlib.suppress(Exception):
            await sh.ws.send(kalshi_ws.update_subscription(sh.sid, late, "get_snapshot", cid))
            self._count("books_snap_requested", len(late))

    async def _on_control(self, channel: str, sh: Shard | None, frame: dict) -> None:
        """React to a protocol frame: learn a shard's sid, retire acknowledged
        commands, and handle rejections. Errors are logged on every channel --
        until 2026-10-20 a refused subscribe parsed to nothing and the
        connection idled into dead air."""
        typ, cid, sid, code = kalshi_ws.control_info(frame)
        label = sh.label if sh else channel
        if typ == "subscribed" and sh is not None:
            sh.sid = sid
            await self._sync_shard(sh)
        elif typ == "ok" and sh is not None and cid is not None:
            sh.pending.pop(cid, None)
        elif typ == "error":
            self._count(f"kalshi_{channel}_errors", 1)
            _log(f"kalshi-{label}: venue error {json.dumps(frame.get('msg'))[:200]} (cmd {cid})")
            if sh is None or self.books is None:
                return
            if cid == 1 and code == kalshi_ws.ERR_MARKET_LIMIT:
                # The SUBSCRIBE itself was over the per-subscription cap.
                self.books.split(sh)
                self._wake_books()
                raise ConnectionError("subscribe over the market limit; resubscribing halved")
            action, tickers = (
                sh.pending.pop(cid, (None, set())) if cid is not None else (None, set())
            )
            if action == "add_markets":
                if code == kalshi_ws.ERR_MARKET_LIMIT:
                    self.books.over_limit(sh, tickers)
                    _log(
                        f"kalshi-{label}: market limit at {len(sh.assigned)}; shard cap"
                        f" now {self.books.shard_max}, {len(tickers)} market(s) re-homed"
                    )
                    self._wake_books()
                else:
                    sh.subscribed -= tickers  # retried by the next sync

    def _store_frame(self, channel: str, sh: Shard | None, frame: dict, recv_ts, raw) -> None:
        """Parse one data frame into its table."""
        if channel == "lifecycle":
            row = kalshi_ws.parse_lifecycle(frame, recv_ts, raw)
            if row is None:
                return
            if self.books is not None and row.msg_type == "market_lifecycle_v2":
                mono = _time.monotonic()  # the clock BookCoverage's bars run on
                if self.books.on_lifecycle(row.market_id, row.event_type, row.open_ts, mono):
                    self._wake_books()
            if row.market_id.startswith(MVE_PREFIX) and not self._include_mve("lifecycle"):
                self._count("kalshi_lifecycle_mve_dropped", 1)
                return
            self.store.append_lifecycles([row])
            self._count("kalshi_lifecycle", 1)
            return
        if channel == "ticker":
            row = kalshi_ws.parse_ticker(frame, recv_ts)
            if row is None:
                return
            if row.market_id.startswith(MVE_PREFIX) and not self._include_mve("ticker"):
                self._count("kalshi_ticker_mve_dropped", 1)
                return
            self.store.append_tickers([row])
            self._count("kalshi_ticker", 1)
            return
        events, trades = kalshi_ws.parse_message(frame, recv_ts)
        if events:
            if sh is not None:
                if sh.idx:
                    off = sh.idx * SHARD_SID_STRIDE
                    for e in events:
                        if e.sid is not None:
                            e.sid += off
                if (
                    sh.awaiting
                    and frame.get("type") == "orderbook_snapshot"
                    and sh.awaiting.pop(events[0].market_id, None) is not None
                ):
                    self._count("books_snap_auto", 1)
            self.store.append_events(events)
            self._count(f"kalshi_{channel}_events", len(events))
        if trades:
            self.store.append_trades(trades)
            self._count("kalshi_trades", len(trades))

    async def _kalshi_loop(self, channel: str, make_subscribe, shard: Shard | None = None) -> None:
        """Shared connect/read/reconnect loop for every Kalshi connection.

        `channel` names the stream (gap rows, stats, raw-frame file); `shard`
        is set for a books connection, whose market set `kalshi_books` edits
        while it runs. A connection reconnects only on a real failure -- an
        exception, a seq jump, dead air -- never because its set changed."""
        if not (self.key_id and self.pem):
            _log(f"kalshi-{channel}: missing KALSHI_API_KEY_ID/KALSHI_PRIVATE_KEY_PATH; idle")
            return
        label = shard.label if shard is not None else channel
        dead_air = DEAD_AIR_BY_CHANNEL.get(channel, DEAD_AIR_SECS)
        backoff, last_recv, first = 1.0, None, True
        # Bound for the first connection's seq_reset row: in-process
        # `last_recv` does not survive a restart (S-188 class 3), so read
        # the channel's last PERSISTED recv_ts once, up front — never in
        # the frame path, where a transient reader lock would cost the
        # frame in hand and force a reconnect. Best-effort: a failed read
        # only shortens the startup row, and the '*' daemon_start row
        # still marks the downtime coarsely. A books shard opened MID-RUN
        # skips it: its markets were covered by nothing before it, and the
        # channel's last frame came from another shard a moment ago.
        resume_since = None
        if shard is None or shard.at_boot:
            try:
                resume_since = await asyncio.to_thread(self.store.last_recv_ts, "kalshi", channel)
            except Exception as exc:
                _log(f"kalshi-{label}: last_recv_ts unavailable ({type(exc).__name__}: {exc})")
        while True:
            try:
                headers = kalshi_ws.auth_headers(self.key_id, self.pem)
                async with websockets.connect(
                    kalshi_ws.WS_URL, additional_headers=headers, max_size=2**23
                ) as ws:
                    if shard is not None:
                        shard.ws, shard.sid = ws, None
                        shard.subscribed = set(shard.assigned)
                        shard.pending.clear()
                        shard.awaiting.clear()
                    await ws.send(make_subscribe())
                    if not first:
                        self._gap("kalshi", channel, last_recv, "reconnect")
                        if channel == "lifecycle":
                            # Lifecycle frames were missed: let REST catch
                            # the book set up now, not at the next period.
                            self._reconcile_now = True
                            self._wake_books()
                    first, backoff = False, 1.0
                    _log(f"kalshi-{label}: connected")
                    tracker = kalshi_ws.SeqTracker()
                    seeded = False  # first frame of THIS connection not yet seen
                    mono = asyncio.get_event_loop().time
                    last_frame = mono()
                    probe_due: float | None = None  # books: liveness probe sent
                    while True:
                        deadline = probe_due or (last_frame + dead_air)
                        if shard is not None and shard.awaiting:
                            deadline = min(deadline, min(shard.awaiting.values()))
                        try:
                            raw = await asyncio.wait_for(
                                ws.recv(), timeout=max(1.0, deadline - mono())
                            )
                        except TimeoutError:
                            now = mono()
                            if shard is not None:
                                await self._request_snapshots(shard, now)
                            silent = now - last_frame > dead_air
                            if silent and shard is not None and probe_due is None and shard.sid:
                                # A books shard can be legitimately quiet (a
                                # few illiquid markets overnight). Ask for one
                                # snapshot: an answer proves the subscription
                                # is alive, silence proves it is not.
                                probe = sorted(shard.assigned)[:1]
                                with contextlib.suppress(Exception):
                                    await ws.send(
                                        kalshi_ws.update_subscription(
                                            shard.sid, probe, "get_snapshot", self._next_cmd()
                                        )
                                    )
                                probe_due = now + PROBE_WAIT_S
                                self._count("books_probes", 1)
                                continue
                            if silent and (probe_due is None or now >= probe_due):
                                self._gap("kalshi", channel, last_recv, "dead_air")
                                raise ConnectionError(
                                    f"no frames for {dead_air:.0f}s (dead air)"
                                ) from None
                            continue
                        last_frame, probe_due = mono(), None
                        recv_ts = datetime.now(UTC)
                        if self.raw is not None:
                            self.raw.put(channel, label, recv_ts, raw)
                        if not seeded:
                            # Every (re)connect resets the ws sequence, so
                            # anything between the last frame we archived and
                            # this connection's FIRST frame is unrecoverable.
                            # The reconnect row above ends at connect time and
                            # a process restart loses `last_recv` entirely
                            # (S-188 class 3) — bound the hole honestly:
                            # [last in-process recv, or last PERSISTED recv on
                            # a fresh process, -> first post-reset frame].
                            seeded = True
                            since = last_recv or resume_since
                            if since is not None:
                                self.store.append_gap(
                                    "kalshi", channel, since, recv_ts, "seq_reset"
                                )
                                _log(f"kalshi/{label} GAP (seq_reset)")
                        self._clock_check("kalshi", channel, recv_ts, last_recv)
                        frame = json.loads(raw)
                        typ = frame.get("type")
                        if typ in kalshi_ws.CONTROL_TYPES:
                            await self._on_control(channel, shard, frame)
                            sid, seq = frame.get("sid"), frame.get("seq")
                            if typ == "ok" and seq is not None and tracker.last(sid) == seq:
                                # An update ack that REPEATS the sid's current
                                # seq consumed none: not a gap, not a row.
                                self._count(f"kalshi_{channel}_ok_seq_repeat", 1)
                                last_recv = recv_ts
                                continue
                        # Continuity check on the raw frame (sid/seq are
                        # frame-level); a jump means missed messages, so
                        # log the gap and reconnect to re-seed books.
                        if tracker.observe(frame.get("sid"), frame.get("seq")):
                            self._gap("kalshi", channel, last_recv, "seq_gap")
                            raise ConnectionError("seq gap; reconnecting to re-seed")
                        self._store_frame(channel, shard, frame, recv_ts, raw)
                        if shard is not None and shard.awaiting:
                            await self._request_snapshots(shard, mono())
                        last_recv = recv_ts
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                note = _venue_clock_note(exc, datetime.now(UTC))
                _log(f"kalshi-{label}: {type(exc).__name__}: {exc}{note}; retry in {backoff:.0f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)
            finally:
                if shard is not None:
                    shard.ws, shard.sid = None, None

    def _poly_token_set(self) -> list[str]:
        """Watchlist pair tokens + top-volume markets' tokens (data-first:
        stream the busiest books even before any pair is hand-verified)."""
        pairs = self.watchlist.get("polymarket_pairs", [])
        assets = {tok for pair in pairs for tok in pair[1:3]}
        try:
            from collector.venues import polymarket as poly

            top = poly.iter_markets_by_volume(
                0.0, max_pages=1, want_top_n=True
            )  # one page, vol-desc: truncation IS the request here
            for m in top[:POLY_TOP_MARKETS]:
                tp = poly.token_pair(m)
                if tp:
                    assets.update(tp)
        except Exception as exc:
            _log(f"poly-books: top-volume refresh failed: {exc}")
        return sorted(assets)

    async def poly_books(self) -> None:
        # An empty initial set (watchlist empty AND Gamma unreachable at
        # boot) must not idle the task forever — retry until tokens exist.
        assets = await _fetch_until_nonempty(
            self._poly_token_set, "poly-books", "token set (watchlist empty, Gamma unreachable)"
        )
        _log(f"poly-books: {len(assets)} tokens (top {POLY_TOP_MARKETS} by volume + pairs)")
        backoff, last_recv, first = 1.0, None, True
        while True:
            try:
                async with websockets.connect(polymarket_ws.WS_URL, max_size=2**23) as ws:
                    await ws.send(polymarket_ws.subscribe_market(assets))
                    if not first:
                        self._gap("polymarket", "market", last_recv, "reconnect")
                    first, backoff = False, 1.0
                    _log(f"poly-books: connected ({len(assets)} tokens)")
                    next_refresh = asyncio.get_event_loop().time() + TICKER_REFRESH_SECS
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=POLY_PING_SECS)
                        except TimeoutError:
                            await ws.send("PING")  # idle keepalive
                            if asyncio.get_event_loop().time() >= next_refresh:
                                next_refresh = asyncio.get_event_loop().time() + TICKER_REFRESH_SECS
                                new = await asyncio.to_thread(self._poly_token_set)
                                if new and new != assets:
                                    _log(
                                        f"poly-books: token set {len(assets)} -> {len(new)};"
                                        " reconnecting"
                                    )
                                    assets = new
                                    break  # reconnect re-seeds via fresh books
                            continue
                        recv_ts = datetime.now(UTC)
                        self._clock_check("polymarket", "market", recv_ts, last_recv)
                        events, trades = polymarket_ws.parse_message(raw, recv_ts)
                        if events:
                            self.store.append_events(events)
                            self._count("poly_events", len(events))
                            # Void rows are archived like any other event, but
                            # they are counted APART: folded into poly_events
                            # a frame type the parser stopped understanding
                            # reads as capture, which is the failure mode that
                            # hid #82 for 78 days. The 5-min stats line is the
                            # operator's fastest reader of a wire change; QA's
                            # void check is the slow one.
                            voids = sum(1 for e in events if e.kind == "void")
                            if voids:
                                self._count("poly_void", voids)
                        if trades:
                            self.store.append_trades(trades)
                            self._count("poly_trades", len(trades))
                        last_recv = recv_ts
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log(f"poly-books: {type(exc).__name__}: {exc}; retry in {backoff:.0f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)

    # -- persistence -------------------------------------------------------

    async def flusher(self) -> None:
        last_stats = asyncio.get_event_loop().time()
        while True:
            await asyncio.sleep(FLUSH_SECS)
            try:
                out = await asyncio.to_thread(self.store.flush_outcome)
            except Exception as exc:
                # pending size makes a wedged-reader buildup visible in
                # the journal long before it could OOM the daemon. Past
                # SPILL_CAP pending plateaus, so show sidecar growth too.
                n_pending = self.store.pending
                n_spilled = self.store.spilled
                sev = "CRITICAL " if n_pending > self.store.PENDING_ALARM else ""
                spill = f" (+{n_spilled} spilled to sidecar)" if n_spilled else ""
                _log(
                    f"{sev}flush FAILED ({type(exc).__name__}: {exc});"
                    f" {n_pending} rows held for retry{spill}"
                )
                # The journal line above says a flush failed; the ledger says
                # how long the episode ran and how big it got. Written from
                # here because this is the only scope that sees BOTH ends.
                self.stalls.failed(n_pending, n_spilled, exc, datetime.now(UTC))
                continue
            # ONLY A FLUSH THAT OPENED THE FILE ENDS A STALL. `flush_outcome`
            # returns without attaching when there is nothing to write, and
            # that tick says nothing about whether the archive was writable:
            # closing an episode on it reports a recovery the daemon never
            # observed, mid-hold, while the reader still holds the lock
            # (EXP-1390, mistakes #103). An episode therefore ends at the
            # next flush with rows -- so its span understates a hold at the
            # FRONT too, by however long the buffer stayed empty, which is
            # not `period_s` and is not bounded by it.
            if out.touched:
                self.stalls.ok(datetime.now(UTC))
            # A drain that skipped sidecar records is a real archive hole
            # (torn append from a host crash). The store no longer stalls on
            # it, so the journal is the only place it can surface (EXP-936).
            if self.store.spill_corrupt != self._spill_corrupt_seen:
                lost = self.store.spill_corrupt - self._spill_corrupt_seen
                self._spill_corrupt_seen = self.store.spill_corrupt
                _log(
                    f"CRITICAL sidecar drain skipped {lost} unreadable row(s)"
                    f" — archive hole (total {self.store.spill_corrupt})"
                )
            now = asyncio.get_event_loop().time()
            if now - last_stats >= STATS_SECS:
                sock, throttled = self.sock_throttle.note()
                _log(f"stats {self.stats} (flushed {out.rows} this round) {sock}")
                if throttled:
                    # Its own line, so a grep for the word finds the PERIOD
                    # it landed in, not every stats line ever printed.
                    _log(
                        f"WARNING sock_throttled +{throttled} this period: the cgroup's"
                        " memory pressure clamped this daemon's sockets (reclaim with"
                        " a cost -- check the next GAP lines and the stall ledger)"
                    )
                last_stats = now

    def _install_stop(self) -> asyncio.Task | None:
        """Make SIGTERM reach the shutdown drain instead of killing the
        process on the spot.

        systemd stops this unit with SIGTERM, and Python's default
        disposition for SIGTERM is the OS one: terminate immediately, no
        `finally`, no drain. MEASURED 2026-09-12 on the live box — 4
        `starting (db=` lines in 14 days of `hyxlab-stream` journal and
        ZERO `shutdown; stats` lines. The final drain that exists so the
        daemon never loses what it saw had not run in production once; it
        was reachable only from Ctrl-C and from `--smoke`. Every
        `systemctl restart` (promote.sh does one whenever streamd changes)
        dropped the buffer instead — a flush interval normally, up to
        SPILL_CAP rows if a stall was in progress, which is the restart a
        wedged archive makes most likely.

        Returns None where the loop cannot take signal handlers (Windows,
        a non-main thread, an embedded loop in the suite): the daemon then
        behaves exactly as before rather than refusing to start.
        """
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        self._stop_signals = []
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError, ValueError):
                continue
            self._stop_signals.append(sig)
        if not self._stop_signals:
            return None
        return asyncio.create_task(stop.wait(), name="stop")

    def _release_stop(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in getattr(self, "_stop_signals", []):
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.remove_signal_handler(sig)
        self._stop_signals = []

    def _final_drain(self, elapsed: Callable[[], float] | None = None) -> None:
        """The last write of this process's life, and the one that must not
        raise.

        Four archive holes live here. (a) and (b) only open on the path
        where the archive is unreachable — which at shutdown is the LIKELY
        path, because a wedged archive is the usual reason a restart is
        happening. (c) opens on the OPPOSITE path: an archive that is
        reachable but has a wedge's worth of backlog queued behind it,
        where the flush runs past systemd's SIGKILL rather than failing.
        (a) `store.flush()` raising means every buffered row dies with the
        process; the sidecar exists exactly for that, so spill EVERYTHING
        rather than just the overflow above SPILL_CAP, which at shutdown
        protects nothing: there is no next flush for the rows it leaves in
        memory. (This once justified itself with "a sub-hour stall never
        reaches SPILL_CAP" — false, and refuted by the measurement recorded
        beside STALL_LOG above: two of the three ~30 min episodes DID reach
        the cap. The cap was ~27 min of firehose then (~8 min since 2026-10-23), not the ~1 h it was
        documented as; see `StreamStore.BUFFER_ROWS_PER_S`. The spill-all is
        right for the reason stated here, which does not depend on it.) (c) The
        drain is a race against the unit's `TimeoutStopSec` at a measured
        `StreamStore.FLUSH_ROWS_PER_S`, and LOSING that race is worse than not starting: a
        SIGKILL lands after `flush()` has moved the buffers into locals,
        skipping the `except BaseException` that would put them back and
        the `spill_all` that would have saved them. Refused above the row
        budget; see `DRAIN_BUDGET_S`. (b) The open stall episode
        would never be written: `ok()` is unreachable when the drain that
        would have ended the stall is the thing that failed, so the episode
        the restart interrupted is recorded as `open` — a lower bound.
        (d) The drain does not own this store alone: the periodic flush it
        races runs on an `asyncio.to_thread` worker that OUTLIVES the task
        cancellation in `run()`'s `finally` (measured). Two `flush()` calls
        archive the same sidecar twice, and a `spill_all` beside one lands
        in the file that flush unlinks after commit. Everything below is
        therefore under `store.exclusive`, and a wait it loses is declined
        rather than raced; see `DRAIN_LOCK_WAIT_S`.

        Nothing here propagates: this runs inside `run()`'s `finally`, where
        a raise would mask the exception that ended the daemon and skip the
        shutdown line.
        """
        now = datetime.now(UTC)
        t0 = _time.monotonic()
        if elapsed is None:

            def elapsed() -> float:
                return _time.monotonic() - t0

        with self.store.exclusive(DRAIN_LOCK_WAIT_S) as got:
            if not got:
                # (d) A periodic flush is still writing on its worker
                # thread, so touching the SIDECAR here is a hole: a flush
                # would archive it twice, a spill would be unlinked by the
                # commit in flight. What is at risk is only what arrived
                # since that flush swapped the buffers -- and until
                # 2026-10-14 this branch left exactly that to die with the
                # process (measured: a poweroff inside an outside hold,
                # 143,520 rows "left in memory"). The snapshot goes to a
                # FRESH file the in-flight flush cannot see, untrimmed, so
                # the next boot adopts it: duplicates possible, holes not.
                held = self.store.pending
                try:
                    moved, path = self.store.spill_snapshot(now)
                except Exception as snap_exc:
                    _log(
                        f"CRITICAL final drain SKIPPED: a flush still holds the store "
                        f"after {DRAIN_LOCK_WAIT_S:.0f}s and the snapshot refused "
                        f"({_short_err(snap_exc)}) -- {held} row(s) lost"
                    )
                    moved = 0
                else:
                    _log(
                        f"final drain SKIPPED: a flush still holds the store after "
                        f"{DRAIN_LOCK_WAIT_S:.0f}s; {moved} row(s) snapshotted to "
                        f"{path.name if path else 'nothing'} for the next boot to adopt "
                        f"(the in-flight flush keeps its own batch)"
                    )
                self.stalls.interrupted(now, handoff=moved, spilled=self.store.spilled)
                return
            self._drain_locked(now, elapsed)

    def _drain_locked(self, now: datetime, elapsed: Callable[[], float]) -> None:
        """The drain proper, under `store.exclusive`. Split out so the
        decision and the write it leads to cannot be reached without the
        lock they both depend on."""
        # What is left of the budget after the wait above -- SIGKILL lands
        # `TimeoutStopSec` after SIGTERM, not after the lock came free. The
        # charge is truncated to WHOLE seconds so an uncontended drain (the
        # normal case: microseconds to take a free lock) is charged nothing
        # and the boundary stays exactly where DRAIN_BUDGET_S puts it.
        # Sub-second resolution here would be false precision anyway --
        # FLUSH_ROWS_PER_S is a rounded-down measurement, and one second of
        # it is 100,000 rows.
        left = max(0.0, DRAIN_BUDGET_S - float(int(elapsed())))
        budget = int(left * self.store.FLUSH_ROWS_PER_S)
        est = self.store.drain_rows_estimate()
        if est > budget:
            # (c) The flush cannot finish before systemd's SIGKILL, so
            # starting it is how the rows are lost — see DRAIN_BUDGET_S.
            # Hand the tape to the sidecar instead and let the next boot
            # drain it. Note the estimate counts sidecar rows that are
            # ALREADY on disk, so `moved` here is only what left memory.
            moved = 0
            try:
                moved = self.store.spill_all()
            except Exception as spill_exc:
                _log(
                    f"CRITICAL drain of ~{est} row(s) exceeds the {left:.0f}s "
                    f"budget left and the sidecar refused the handoff "
                    f"({_short_err(spill_exc)}) — {self.store.pending} row(s) lost"
                )
            else:
                _log(
                    f"final drain DECLINED: ~{est} row(s) is "
                    f"~{est / self.store.FLUSH_ROWS_PER_S:.0f}s of flush against the "
                    f"{left:.0f}s left of a {DRAIN_BUDGET_S:.0f}s budget; "
                    f"{moved} row(s) moved to the sidecar "
                    f"for the next boot to drain"
                )
            # No flush failed here, so the episode is not closed and no
            # exception is accounted against it; `interrupted` is a no-op
            # unless a stall was already open, which is the only way a
            # sidecar this large gets built in the first place.
            self.stalls.interrupted(now, handoff=moved, spilled=self.store.spilled)
            return
        try:
            out = self.store.flush_outcome()
        except Exception as exc:
            held = self.store.pending
            try:
                moved = self.store.spill_all()
            except Exception as spill_exc:
                moved = 0
                _log(
                    f"CRITICAL final drain failed ({_short_err(exc)}) and the sidecar "
                    f"refused it ({_short_err(spill_exc)}) — {held} row(s) lost"
                )
            else:
                _log(
                    f"final drain FAILED ({_short_err(exc)}); {moved} row(s) moved to the "
                    f"sidecar for the next boot to drain"
                )
            self.stalls.observe(held, self.store.spilled, exc, now)
            self.stalls.interrupted(now, handoff=moved)
        else:
            # THE SAME RULE AS THE FLUSHER, ONE LEVEL DOWN (mistakes #103,
            # #97). `flush_outcome` returns without opening the archive when
            # there is nothing to write, and this branch used the count-only
            # `flush()` and reported `ok` unconditionally -- closing an open
            # episode on the strength of a call that never touched the file,
            # in the one path that runs while the daemon is dying and so is
            # the last thing the ledger ever says about that episode.
            #
            # Latent today, and only by an invariant nothing enforced: an
            # episode opens from a flush that RAISED, which cannot happen
            # unless there were rows or a sidecar, so `drain_rows_estimate`
            # is non-zero and the drain's own flush opens the file. That
            # invariant is asserted in `tests/test_stream_stalls.py` rather
            # than trusted -- `drain_rows_estimate` already reads an
            # unstattable sidecar as zero, so the arithmetic that protects
            # this line is one suppressed OSError wide.
            #
            # An untouched drain is not a recovery and is not a loss: the
            # episode simply outlives this process, which is what
            # `interrupted` says (and is a no-op when none is open).
            if out.touched:
                self.stalls.ok(now)
            else:
                self.stalls.interrupted(now, handoff=0)

    async def run(self, duration: float | None = None) -> None:
        # Before any flush: a shutdown snapshot (see `_final_drain`, the
        # refused branch) is a sidecar the flusher does not know about until
        # it is folded in, and this is the one moment no flush can race it.
        adopted = self.store.adopt_snapshots()
        if adopted:
            _log(f"adopted {adopted} row(s) from shutdown snapshot(s) into the sidecar")
        self.store.mark_startup_gap()
        self.stalls.arm(datetime.now(UTC))
        tasks = [
            asyncio.create_task(self.kalshi_trades(), name="kalshi-trades"),
            asyncio.create_task(self.kalshi_books(), name="kalshi-books"),
            asyncio.create_task(self.kalshi_lifecycle(), name="kalshi-lifecycle"),
            asyncio.create_task(self.kalshi_ticker(), name="kalshi-ticker"),
            asyncio.create_task(self.poly_books(), name="poly-books"),
            asyncio.create_task(self.flusher(), name="flusher"),
        ]
        stop = self._install_stop()
        running = asyncio.gather(*tasks)
        try:
            if duration:
                await asyncio.sleep(duration)
            elif stop is None:
                await running
            else:
                done, _ = await asyncio.wait({running, stop}, return_when=asyncio.FIRST_COMPLETED)
                # A task that died still has to surface with its traceback;
                # only the signal arm is a normal end.
                if running in done:
                    running.result()
        finally:
            self._release_stop()
            if stop is not None:
                stop.cancel()
            running.cancel()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # `running` gathers the same tasks, so its outcome is already
            # accounted for -- but an unretrieved cancelled gather prints a
            # bare traceback over the shutdown line at interpreter exit, and
            # the shutdown line is the one an operator reads. Consumed, never
            # awaited: this is a shutdown path and must not be able to block.
            if running.done():
                with contextlib.suppress(asyncio.CancelledError):
                    running.exception()
            self._final_drain()
            _log(f"shutdown; stats {self.stats}")


def main() -> None:
    ap = argparse.ArgumentParser(description="hyxlab live stream daemon")
    ap.add_argument("--db", default="data/hyxstream.duckdb")
    ap.add_argument("--watchlist", default=None)
    ap.add_argument("--smoke", type=float, default=None, help="run N seconds, then exit")
    ap.add_argument(
        "--raw-frames",
        default="data/raw_frames",
        help="root for the raw Kalshi frame files; '' keeps none",
    )
    ap.add_argument(
        "--raw-retain-days",
        type=int,
        default=RAW_RETAIN_DAYS,
        help="raw-frame day directories kept (pruned hourly by the writer)",
    )
    ap.add_argument("--env", default=".env", help="KEY=VALUE file for the Kalshi credentials")
    args = ap.parse_args()

    # Own the stream archive before opening it. DuckDB's file lock is held
    # only for the duration of each flush, so between flushes it excludes
    # nothing: two daemons on one file interleave duplicate book_events and
    # stream_trades into tables that have no key and never dedupe, and a
    # duplicated DELTA corrupts every book replay downstream of it. The
    # data is unrecoverable — neither venue serves historical books — so
    # the second copy must never start.
    lock, why = db_owner_lock_or_reason(args.db)
    if lock is None:
        print(f"[streamd] {why}; not starting", flush=True)
        raise SystemExit(75)

    load_env(args.env)
    from hyxlab.watchlist import DEFAULT_WATCHLIST, load_watchlist

    watchlist = load_watchlist(args.watchlist or str(DEFAULT_WATCHLIST))
    store = StreamStore(args.db)
    raw = (
        RawFrameWriter(args.raw_frames, retain_days=args.raw_retain_days)
        if args.raw_frames
        else None
    )
    if raw is not None:
        raw.start()
    daemon = Daemon(store, watchlist, raw)
    _log(f"starting (db={args.db}, raw_frames={args.raw_frames or 'off'}, smoke={args.smoke})")
    try:
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(daemon.run(duration=args.smoke))
    finally:
        if raw is not None and not raw.close(RAW_CLOSE_S):
            print(f"[streamd] raw frames: close timed out after {RAW_CLOSE_S:.0f}s", flush=True)
    print(f"[streamd] final counts: {store.counts()}", flush=True)


if __name__ == "__main__":
    main()
