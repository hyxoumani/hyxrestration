"""DuckDB persistence for collected market data and forecasts.

One file (default `data/hyxlab.duckdb` — gitignored via the rooted /data/
rule) holds three tables:

- markets:       latest known metadata + settlement result per market
- snapshots:     append-only top-of-book observations
- nws_forecasts: append-only forecast pulls (fetched_at kept so the sim
                 can enforce no-lookahead when serving forecasts)
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import duckdb

from hyxlab.memcap import duck_memory_limit
from hyxlab.models import EconVintage, Forecast, MarketInfo, NewsItem, Snapshot
from hyxlab.scratch import duck_scratch_dir
from hyxlab.spillcap import duck_spill_limit, parse_size

_SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    venue        VARCHAR NOT NULL,
    market_id    VARCHAR NOT NULL,
    title        VARCHAR,
    series       VARCHAR,
    close_time   TIMESTAMP,
    strike_type  VARCHAR,
    floor_strike DOUBLE,
    cap_strike   DOUBLE,
    result       VARCHAR,
    target_date  DATE,
    updated_at   TIMESTAMP,
    -- open_time is LAST deliberately: pre-existing DBs gain it via the
    -- idempotent ALTER below, and ALTER appends at the end — keeping the
    -- positional column order identical for fresh and migrated DBs.
    open_time    TIMESTAMP,
    PRIMARY KEY (venue, market_id)
);
ALTER TABLE markets ADD COLUMN IF NOT EXISTS open_time TIMESTAMP;
CREATE TABLE IF NOT EXISTS snapshots (
    venue         VARCHAR NOT NULL,
    market_id     VARCHAR NOT NULL,
    ts            TIMESTAMP NOT NULL,
    yes_bid       DOUBLE, yes_ask DOUBLE, no_bid DOUBLE, no_ask DOUBLE,
    yes_bid_size  DOUBLE, yes_ask_size DOUBLE,
    no_bid_size   DOUBLE, no_ask_size DOUBLE,
    last_price    DOUBLE,
    volume        DOUBLE,
    open_interest DOUBLE
);
-- Exchange-wide top-N breadth tape (collector/breadth.py, EXP-928).
-- Separate table from `snapshots` ON PURPOSE: `snapshots` is the
-- watchlist FOCUS tape that the simulator, QA and every coverage
-- instrument read as "the series we study", and folding ~288k rows/day
-- of unstudied families into it would silently redefine every one of
-- those readings. Same columns plus the ranking basis, so a breadth row
-- can be projected onto a Snapshot by dropping the last two.
CREATE TABLE IF NOT EXISTS breadth_snapshots (
    venue         VARCHAR NOT NULL,
    market_id     VARCHAR NOT NULL,
    ts            TIMESTAMP NOT NULL,
    yes_bid       DOUBLE, yes_ask DOUBLE, no_bid DOUBLE, no_ask DOUBLE,
    yes_bid_size  DOUBLE, yes_ask_size DOUBLE,
    no_bid_size   DOUBLE, no_ask_size DOUBLE,
    last_price    DOUBLE,
    volume        DOUBLE,
    open_interest DOUBLE,
    volume_24h    DOUBLE,
    rank          INTEGER
);
-- One row per breadth cycle: what the cycle SAW, next to what it wrote.
-- `breadth_snapshots` alone cannot distinguish "few rows because the
-- exchange is quiet" from "few rows because the enumeration truncated and
-- we ranked an arbitrary head slice" — and on 2026-09-06T23:32Z that
-- distinction was the whole story: coverage fell 990 -> 3 rows/cycle for
-- 15 hours while freshness and 24h-continuity both read green, because
-- cycles kept landing ON TIME with almost nothing in them. The
-- discriminator existed only as a journald print. It lives here now.
CREATE TABLE IF NOT EXISTS breadth_cycles (
    ts         TIMESTAMP NOT NULL,
    universe   INTEGER NOT NULL,  -- markets the enumeration returned
    picked     INTEGER NOT NULL,  -- survivors of the top-N volume floor
    inserted   INTEGER NOT NULL,
    truncated  BOOLEAN NOT NULL,  -- page budget exhausted, cursor still live
    cutoff_volume_24h DOUBLE
);
CREATE TABLE IF NOT EXISTS nws_forecasts (
    station     VARCHAR NOT NULL,
    fetched_at  TIMESTAMP NOT NULL,
    target_date DATE NOT NULL,
    high_f      INTEGER NOT NULL,
    short       VARCHAR
);
CREATE TABLE IF NOT EXISTS candles (
    venue         VARCHAR NOT NULL,
    market_id     VARCHAR NOT NULL,
    end_ts        TIMESTAMP NOT NULL,
    period_s      INTEGER NOT NULL,
    price_open    DOUBLE, price_high DOUBLE, price_low DOUBLE, price_close DOUBLE,
    yes_bid_close DOUBLE, yes_ask_close DOUBLE,
    yes_bid_high  DOUBLE, yes_ask_low DOUBLE,
    volume        DOUBLE,
    open_interest DOUBLE
);
CREATE TABLE IF NOT EXISTS observations (
    station     VARCHAR NOT NULL,
    obs_date    DATE NOT NULL,
    high_f      INTEGER,
    PRIMARY KEY (station, obs_date)
);
CREATE TABLE IF NOT EXISTS trades (
    venue      VARCHAR NOT NULL,
    market_id  VARCHAR NOT NULL,
    trade_id   VARCHAR NOT NULL,
    ts         TIMESTAMP NOT NULL,
    yes_price  DOUBLE NOT NULL,
    qty        DOUBLE NOT NULL,
    taker_side VARCHAR,
    is_block   BOOLEAN
);
CREATE TABLE IF NOT EXISTS trades_swept (
    market_id VARCHAR PRIMARY KEY,
    swept_at  TIMESTAMP,
    n_trades  INTEGER,
    status    VARCHAR
);
CREATE TABLE IF NOT EXISTS poly_prices (
    token_id  VARCHAR NOT NULL,
    market_id VARCHAR NOT NULL,
    outcome   VARCHAR,
    ts        TIMESTAMP NOT NULL,
    price     DOUBLE NOT NULL
);
CREATE TABLE IF NOT EXISTS poly_tail_stops (
    market_id  VARCHAR NOT NULL,
    stopped_at TIMESTAMP NOT NULL,
    prints     INTEGER NOT NULL,   -- prints the tail reached before the stop
    status     INTEGER,            -- HTTP status that ended it (429 / 408)
    PRIMARY KEY (market_id, stopped_at)
);
CREATE TABLE IF NOT EXISTS poly_market_stats (
    market_id VARCHAR NOT NULL,
    ts        TIMESTAMP NOT NULL,
    volume    DOUBLE,
    liquidity DOUBLE
);
CREATE TABLE IF NOT EXISTS series (
    venue          VARCHAR NOT NULL,
    ticker         VARCHAR NOT NULL,
    title          VARCHAR,
    category       VARCHAR,
    fee_type       VARCHAR,
    fee_multiplier DOUBLE,
    frequency      VARCHAR,
    updated_at     TIMESTAMP,
    PRIMARY KEY (venue, ticker)
);
CREATE TABLE IF NOT EXISTS sweep_log (
    series     VARCHAR NOT NULL,
    swept_at   TIMESTAMP NOT NULL,
    min_close  TIMESTAMP,
    max_close  TIMESTAMP,
    n_markets  INTEGER,
    n_candles  INTEGER,
    status     VARCHAR,
    note       VARCHAR
);
CREATE TABLE IF NOT EXISTS watermarks (
    series        VARCHAR NOT NULL PRIMARY KEY,
    last_close_ts TIMESTAMP
);
CREATE TABLE IF NOT EXISTS econ_vintages (
    series_id    VARCHAR NOT NULL,     -- e.g. 'CPIAUCSL'
    obs_date     DATE    NOT NULL,     -- period the value describes
    value        DOUBLE,
    knowable_at  TIMESTAMP NOT NULL,   -- release moment of this vintage
    PRIMARY KEY (series_id, obs_date, knowable_at)
);
CREATE TABLE IF NOT EXISTS news_items (
    source       VARCHAR NOT NULL,     -- 'gdelt' | 'alpaca'
    url_hash     VARCHAR NOT NULL,
    published_at TIMESTAMP,
    knowable_at  TIMESTAMP NOT NULL,   -- monitored/wire time
    title        VARCHAR,
    tone         DOUBLE,               -- NULL for alpaca
    topics       VARCHAR,              -- comma list, from query template
    symbols      VARCHAR,              -- comma list of tickers (alpaca)
    PRIMARY KEY (source, url_hash)
);
CREATE TABLE IF NOT EXISTS schema_meta (
    version INTEGER NOT NULL
);
"""

# Bump when adding a migration in hyxlab/migrate.py.
SCHEMA_VERSION = 2


def _naive_utc(dt: datetime | None) -> datetime | None:
    """Normalize to naive-UTC before insert.

    DuckDB's TIMESTAMP is naive and converts tz-aware values to the BOX's
    local time on insert — a silent, machine-dependent convention. All
    hyxlab timestamps are therefore stored as naive UTC explicitly; reads
    return naive UTC and all in-DB comparisons stay consistent.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(UTC).replace(tzinfo=None)


def private_spill(conn, path: str | Path) -> None:
    """Point `conn`'s spill at this process's own directory.

    Every connection, not just the ones known to spill: DuckDB's default
    `temp_directory` is `<db>.tmp`, shared by every process that opens
    the file, and its temp files carry no pid. Two spilling processes
    there crash or misread each other's blocks (measured — see
    `hyxlab.scratch`). Applied at the connect chokepoint because "this
    query is small" is a claim about a plan, and the plan is the thing
    that changes.

    Never fatal, and the catch is deliberately total: this is hardening
    bolted onto every connect in the repo, so any way it can fail is a
    way it can take the archive down for a spill that may never happen.
    Same rule as `lockid.note_holder` — an instrument that can abort the
    work it observes trades a small loss for a bigger one.
    """
    try:
        d = duck_scratch_dir(path)
        if d is not None:
            conn.execute("SET temp_directory = ?", [d])
    except Exception:  # noqa: BLE001 — see above
        pass


def cgroup_memory_limit(conn) -> None:
    """Size `conn`'s buffer manager from the cgroup, not from host RAM.

    DuckDB's default `memory_limit` is 80% of PHYSICAL memory, which a
    capped systemd service does not have: `hyxlab-stream` runs under
    `MemoryMax=2G` on a 60 GiB box and opened the stream archive
    believing it had 48.2 GiB. Its own startup query then peaked at
    2899 MB and the cgroup answered with SIGKILL (EXP-1374, measured —
    see `hyxlab.memcap`). Under a pinned limit the same query is bounded,
    faster, and spills nothing.

    Applied at the connect chokepoint for the same reason as
    `private_spill`: "this query is small" is a claim about a plan, and
    the plan is the thing that changes. A no-op where no cgroup cap
    binds, so an uncapped box keeps DuckDB's own default.

    Never fatal, and the catch is deliberately total — same rule as
    `private_spill`: hardening bolted onto every connect in the repo must
    not be a new way to lose the archive.
    """
    try:
        n = duck_memory_limit()
        if n is not None:
            conn.execute("SET memory_limit = ?", [f"{n}B"])
    except Exception:  # noqa: BLE001 — see above
        pass


def spill_cap(conn, path: str | Path) -> None:
    """Bound `conn`'s spill by the limit in force, not by the disk.

    DuckDB's `max_temp_directory_size` defaults to "90% of available disk
    space" — 1.26 TB of the volume the archive, the collector and the
    stream daemon share. Spill is the buffer manager's overflow, so the
    bound is derived from `memory_limit` as it stands AFTER
    `cgroup_memory_limit` has had its say, and clamped by a share of free
    space for the uncapped case (EXP-1375, measured — see
    `hyxlab.spillcap`). Nothing measured comes near it: the largest spill
    by a query that SUCCEEDED was 266 MiB, and the big ones die in memory
    without spilling at all.

    Third at the chokepoint, and the order against `cgroup_memory_limit`
    is load-bearing: this is a multiple of the limit that one sets. Free
    space is measured on the DATABASE's filesystem, not by asking the
    connection where it spills — the spill root is `<db>.tmp` by
    construction (`hyxlab.scratch.scratch_root`), so it is the same
    filesystem either way, and naming a spill directory anywhere but
    `private_spill` is what `test_sidecar_discipline.py` forbids.

    Never fatal, and the catch is deliberately total — same rule as the
    two rungs below: hardening bolted onto every connect in the repo must
    not be a new way to lose the archive.
    """
    try:
        raw = conn.execute("SELECT current_setting('memory_limit')").fetchone()[0]
        mem = parse_size(str(raw))
        if mem is None:
            return
        n = duck_spill_limit(mem, path)
        conn.execute("SET max_temp_directory_size = ?", [f"{n}B"])
    except Exception:  # noqa: BLE001 — see above
        pass


#: DuckDB's message when another process holds the file lock. The PID is
#: the whole point: it is the difference between "come back later" and
#: "the archive is broken", and those two want opposite reactions.
_LOCK_HOLDER_RE = re.compile(r"Conflicting lock is held in (\S+) \(PID (\d+)\)")
#: ...and the file it could not lock, which is what turns the PID from a
#: number into a verifiable claim (see `_holder_name`).
_LOCK_FILE_RE = re.compile(r'Could not set lock on file "([^"]+)"')
#: Indirected for the tests ONLY. A holder's unit, command line and open
#: descriptors all come from one kernel filesystem, and a test that cannot
#: build a holder cannot check any of the three -- the alternative is
#: asserting against whatever cgroup the suite happens to run in, which is a
#: `.scope` under a shell and `hyxlab-autoloop.service` under the loop.
_PROC = Path("/proc")


def _holder_unit(pid: str) -> str | None:
    """The systemd unit `pid` runs under, from its cgroup — or None.

    The cgroup line is `0::/user.slice/.../app.slice/hyxlab-stream.service`;
    the last component that looks like a unit IS the attribution. Readable
    only while the process lives, which is why every caller resolves at the
    moment of the collision and never afterwards.
    """
    try:
        line = (_PROC / pid / "cgroup").read_text().strip().rsplit("/", 1)[-1]
    except OSError:
        return None
    return line if line.endswith((".service", ".scope", ".slice")) else None


def _holder_cmd(pid: str) -> str | None:
    """`python -m simulator.run_l2`-shaped summary of `pid`'s command line.

    The fallback for a holder with no unit — an interactive `python -m ...`,
    which on this box is a real and recurring class of long reader (three of
    the 2026-09-25 stall episodes were an autonomous pass's own probes).
    """
    try:
        argv = (_PROC / pid / "cmdline").read_bytes().split(b"\0")
    except OSError:
        return None
    parts = [a.decode("utf-8", "replace") for a in argv if a]
    if not parts:
        return None
    parts[0] = Path(parts[0]).name
    return " ".join(parts)[:80]


def _holds_file(pid: str, path: str) -> bool | None:
    """Does `pid` have `path` open? True/False, or None if unanswerable.

    A held DuckDB file lock is an flock on an OPEN descriptor, so the
    holder's `/proc/<pid>/fd` is a direct answer and not a heuristic — it
    is the one check that separates the real holder from a PID that was
    recycled between DuckDB's refusal and this lookup. None (not False)
    when the fd table is unreadable (another user's process, or the holder
    exited mid-scan): "I could not look" must not read as "it was not it".
    """
    try:
        fds = list((_PROC / pid / "fd").iterdir())
    except OSError:
        return None
    seen = False
    for fd in fds:
        try:
            if os.path.realpath(fd) == path:
                return True
            seen = True
        except OSError:
            continue
    return False if seen else None


def lock_holder(exc: BaseException) -> str | None:
    """Who holds this file's lock, from a failed attach — or None.

    A reader that could not attach has learned ONE of two opposite facts,
    and `duckdb.IOException` says both with the same words. Either a live
    writer holds the file, which on this box is routine and expected (the
    poly sweep holds the archive for ~7 hours, collect and tradepass in
    bursts) and the answer is to come back later; or nothing holds it and
    the file is unreachable, which is an incident. Reporting the first as
    the second is alarm fatigue; reporting the second as the first loses
    the archive quietly. The discriminator is already in the error text.

    None when the message names no holder AND when it names a PID that is
    no longer alive — a dead PID is a lock left behind, which is the
    unreachable case wearing the routine one's message. That check is why
    this is a function and not a regex at each call site: `collector.qa`
    got it right, `simulator.atlas` re-derived neither half and printed
    the traceback.

    THE NAME DuckDB SUPPLIES CANNOT ATTRIBUTE ANYTHING HERE, and that is
    what this function adds. Its first capture is the EXECUTABLE, which on
    this box is `/usr/bin/python3.14` for `hyxlab-stream`, `hyxlab-shadow`,
    `hyxlab-divergence`, every sweep, and every ad-hoc probe alike — so the
    string it produced was the same string for every holder there has ever
    been. On 2026-09-25 a 3,057 s stall drove 387,856 rows out of streamd's
    buffer into the torn-append sidecar, and the ledger recorded that
    interpreter path against it; the culprit was identified a pass later by
    ELIMINATION over which timers had fired in the window, on an inference
    the ledger could neither support nor refute. The cgroup names the unit
    outright, and it is legible only while the holder is alive — minutes
    later `/proc/<pid>` is gone and the question is permanently unanswerable.

    So the name is resolved here, at the collision: unit if there is one,
    else the command line, else the interpreter path as before. Where the
    message also names the FILE, the claim is VERIFIED against the holder's
    open descriptors rather than asserted, because a PID is only evidence
    if it still refers to the process DuckDB meant.

    LIMIT, WRITTEN DOWN RATHER THAN IMPLIED: a read-only DuckDB lock is
    SHARED, so several processes may hold the file while the message names
    exactly one. This names *a* holder, never the whole set — and the
    verdict (None vs not-None) is deliberately unchanged by any of the
    above, so every existing caller's live-writer-vs-broken-archive
    discriminator reads exactly as it did before.
    """
    m = _LOCK_HOLDER_RE.search(str(exc))
    if not m or not (_PROC / m.group(2)).exists():
        return None
    exe, pid = m.group(1), m.group(2)
    name = _holder_unit(pid) or _holder_cmd(pid) or exe
    mf = _LOCK_FILE_RE.search(str(exc))
    if mf and _holds_file(pid, mf.group(1)) is False:
        # Alive, but not on this file: the PID was recycled, or the holder
        # let go between DuckDB's attempt and this lookup. Naming it without
        # the caveat is how an instrument indicts an innocent.
        return f"{name} pid {pid} (no longer holds {Path(mf.group(1)).name})"
    return f"{name} pid {pid}"


def duck_connect(path: str | Path, *, read_only: bool = False, **kw):
    """`duckdb.connect` plus private spill — the ONLY attach in the repo.

    A bare `duckdb.connect` gets DuckDB's default `temp_directory`,
    `<db>.tmp`, which is shared by every process that opens the file and
    whose temp files carry no pid; two spilling processes there crash or
    misread each other (EXP-1373, measured). The sites that cannot use
    `connect_retry`/`open_retry` — they hand-roll a retry budget, degrade
    on error, or own the file outright — still need that fix, and
    "this query is too small to spill" is a claim about a query plan.

    So the rule is mechanical instead: nothing outside this module calls
    `duckdb.connect`, enforced by `tests/test_sidecar_discipline.py`.
    Read-only discipline is unchanged and still enumerated by
    `tests/test_connect_discipline.py`, which counts this as an attach.
    """
    conn = duckdb.connect(str(path), read_only=read_only, **kw)
    private_spill(conn, path)
    cgroup_memory_limit(conn)
    spill_cap(conn, path)
    return conn


# ---------------------------------------------------------------------------
# Attach wait: the measured half of every retry budget in this repo.
#
# Every attach budget here (this module's defaults, `atlas.ARCHIVE_ATTACH`,
# `divergence.STREAM_ATTACH`) is justified by a comment citing a wait-time
# distribution measured OUTSIDE the code -- "four of five attempts died after
# 30s", "the lock samples free 87% of the time", "p50 0.0s, p90 7.6s, max
# 22.6s". None of those numbers is emitted by any artifact, and the retry
# ladder's own outcome is BINARY: it returns a connection or it raises. An
# attach that returned on its first try and one that returned on its
# nineteenth of twenty are indistinguishable downstream, so a budget eroding
# toward its cliff is invisible until the day it goes over (mistakes #70).
#
# So the wait is recorded here, on EVERY attach, gated on nothing -- the
# common case is ~10ms (measured 2026-09-21 against all three live DBs while
# `hyxlab-poly-sweep` was 10h into a run: p50 11ms, max 14ms) and the common
# case is not the point. The tail is.
#
# ELAPSED IS NOT WAIT, AND ON A BIG RUN IT IS MOSTLY NOT WAIT (mistakes #78).
# The argument above this line used to end "a refused attach itself costs
# 0.03ms, so the elapsed time IS the sleep ladder" -- true, and true only of
# the REFUSED case it was measured on. A SUCCEEDING attach pays the ~10ms of
# actually opening the file, and the ledger was charging that to the retry
# budget. At three attaches (atlas, divergence) 30ms is invisible. At the
# sweep's 7,483 it is the majority of the published total: the first
# production reading, 2026-09-22 11:52Z, printed "96.3s spent waiting in
# total" for a run whose opens alone account for ~65s of it at the measured
# 8.7ms, and whose whole contended span may be as little as 15 sleeps.
#
# So an attach now records both halves. `slept_s` is time spent INSIDE
# `time.sleep`, measured rather than summed from the intended delays (a
# stubbed sleep must read as zero, and a real one overshoots); `waited_s`
# stays the elapsed total, so `open_s = waited_s - slept_s` is the cost of
# the opens themselves. `budget_frac` is `slept_s / budget_s` -- a sleep
# against a sleep budget, which is what "the share of the budget this attach
# spent" always claimed to be. `contended_n` counts the attaches that slept
# at all, because a max and a total cannot tell one 96s block from 48 x 2s,
# and that is the difference between one long reader and constant beating.
#
# Deliberately NOT a re-sizing of any budget. Re-sizing a threshold off a
# freshly observed max is how the wrong number got into `LIVE_GRACE_S`;
# `budget_frac` is the tripwire instead.


@dataclass(frozen=True)
class AttachWait:
    """One attach's cost, split into the two things it is made of.

    `waited_s` is the elapsed time in the retry helper; `slept_s` is the part
    of it spent in `time.sleep`, i.e. the part the retry budget actually
    pays for. `budget_s` is the ladder's nominal sleep total, so `budget_frac`
    is `slept_s / budget_s` and not a share of a number that includes work
    the budget does not govern (mistakes #78).
    """

    db: str
    attempts: int
    waited_s: float
    budget_s: float
    ok: bool
    slept_s: float = 0.0

    @property
    def open_s(self) -> float:
        """Elapsed minus slept: the cost of the open attempts themselves.

        Clamped at 0 -- the two are read from the same clock but not in one
        atomic step, so a tiny negative is measurement noise, not a finding.
        """
        return max(self.waited_s - self.slept_s, 0.0)

    @property
    def budget_frac(self) -> float | None:
        # None, not 0.0: a one-attempt ladder has no budget to spend a share
        # of, and a zeroed field reads as "measured, and it was fine".
        # 0.0 against a REAL budget is a different statement and a true one:
        # the ladder existed and was never entered.
        return self.slept_s / self.budget_s if self.budget_s > 0 else None

    def as_dict(self) -> dict:
        frac = self.budget_frac
        return {
            "db": self.db,
            "attempts": self.attempts,
            "waited_s": round(self.waited_s, 3),
            "slept_s": round(self.slept_s, 3),
            "open_s": round(self.open_s, 3),
            "budget_s": round(self.budget_s, 1),
            "budget_frac": None if frac is None else round(frac, 4),
            "ok": self.ok,
        }


_ATTACH_WAITS: list[AttachWait] = []
# A daemon attaches for as long as it lives; the ledger is for one-shot
# reports, so it is bounded and keeps the MOST RECENT observations.
_ATTACH_WAITS_MAX = 256


@dataclass
class _AttachTotals:
    """Running aggregates over EVERY recorded attach, kept separately from
    the bounded row list.

    The rows are a SAMPLE and always were; the statistics must not be
    (mistakes #72). `_ATTACH_WAITS_MAX` was written for callers that attach
    two or three times, where retained == observed and the distinction is
    invisible. `collector.sweep` attaches twice per series over ~3,709
    series -- so a block computed from the retained rows would describe the
    last ~3% of the run while carrying the run's name, and the statistic a
    drop destroys first is `budget_frac_max`: a max is exactly the number
    that lives in the observations you threw away. The 2026-09-10 06:10Z
    sweep died at series ~600 of 3,656 on an exhausted open ladder; a block
    keeping the last 256 would have held not one attach from the incident.
    """

    observed_n: int = 0
    waited_s_total: float = 0.0
    waited_s_max: float = 0.0
    slept_s_total: float = 0.0
    slept_s_max: float = 0.0
    open_s_total: float = 0.0
    contended_n: int = 0
    budget_frac_max: float | None = None
    exhausted_n: int = 0

    def add(self, w: AttachWait) -> None:
        self.observed_n += 1
        self.waited_s_total += w.waited_s
        self.waited_s_max = max(self.waited_s_max, w.waited_s)
        self.slept_s_total += w.slept_s
        self.slept_s_max = max(self.slept_s_max, w.slept_s)
        self.open_s_total += w.open_s
        # "slept at all" is the contention test: the ladder is only entered
        # when an open is REFUSED, so one sleep means one collision.
        self.contended_n += int(w.slept_s > 0.0)
        frac = w.budget_frac
        if frac is not None:
            self.budget_frac_max = (
                frac if self.budget_frac_max is None else max(self.budget_frac_max, frac)
            )
        self.exhausted_n += int(not w.ok)


_ATTACH_TOTALS = _AttachTotals()


def attach_budget_s(
    retries: int, delay: float = 2.0, backoff: float = 1.0, max_delay: float | None = None
) -> float:
    """Nominal sleep budget of a retry ladder -- `retries` attempts means
    `retries - 1` sleeps, which is the arithmetic every call site's comment
    was doing by hand (and `atlas.ATTACH_BUDGET_S` was carrying as a
    literal)."""
    wait = delay
    total = 0.0
    for _ in range(max(retries - 1, 0)):
        total += wait
        wait *= backoff
        if max_delay is not None:
            wait = min(wait, max_delay)
    return total


def _record_attach(
    path: str | Path,
    attempts: int,
    waited_s: float,
    budget_s: float,
    ok: bool,
    slept_s: float = 0.0,
):
    w = AttachWait(Path(path).name, attempts, waited_s, budget_s, ok, slept_s)
    _ATTACH_TOTALS.add(w)  # BEFORE the trim: the totals outlive the rows
    _ATTACH_WAITS.append(w)
    del _ATTACH_WAITS[:-_ATTACH_WAITS_MAX]


def attach_waits() -> list[AttachWait]:
    return list(_ATTACH_WAITS)


def reset_attach_waits() -> None:
    """Called at the top of a report's `main` so the block describes THIS
    run's attaches and not whatever a test or an import did first."""
    global _ATTACH_TOTALS
    _ATTACH_WAITS.clear()
    _ATTACH_TOTALS = _AttachTotals()


def attach_wait_block(waits: list[AttachWait] | None = None, *, rows: bool = True) -> dict | None:
    """The report block. `None` when nothing attached through the retry
    helpers -- an empty block would read as "attached instantly".

    `n` is the number of attaches OBSERVED, not the number of rows kept:
    the row list is capped at `_ATTACH_WAITS_MAX` and the statistics come
    from `_ATTACH_TOTALS`, which is never trimmed. `retained_n` and
    `dropped_n` say so out loud, because a block that silently described
    its own tail window would be the defect it exists to catch. Pass an
    explicit `waits` list and it IS the population -- nothing was dropped
    by definition.

    `rows=False` omits the per-attach sample for callers that attach
    thousands of times, where the rows are journal noise and the margin is
    the whole message.
    """
    if waits is not None:
        obs = list(waits)
        if not obs:
            return None
        totals = _AttachTotals()
        for w in obs:
            totals.add(w)
    else:
        obs = attach_waits()
        totals = _ATTACH_TOTALS
        if not totals.observed_n:
            return None
    block = {
        "n": totals.observed_n,
        **({"attaches": [w.as_dict() for w in obs]} if rows else {}),
        "retained_n": len(obs),
        "dropped_n": totals.observed_n - len(obs),
        "waited_s_max": round(totals.waited_s_max, 3),
        "waited_s_total": round(totals.waited_s_total, 3),
        # The half the budget actually pays for, and the half it does not
        # (mistakes #78). `waited_s_total` alone reads as contention and on a
        # thousands-of-attaches run it is mostly the cost of the opens.
        "slept_s_max": round(totals.slept_s_max, 3),
        "slept_s_total": round(totals.slept_s_total, 3),
        "open_s_total": round(totals.open_s_total, 3),
        # How many attaches met a holder at all -- a max plus a total cannot
        # separate one long block from many short ones.
        "contended_n": totals.contended_n,
        # The number the call-site comments assert is generous, now readable
        # from the artifact on a run that SUCCEEDED.
        "budget_frac_max": (
            None if totals.budget_frac_max is None else round(totals.budget_frac_max, 4)
        ),
        "exhausted_n": totals.exhausted_n,
    }
    return block


def connect_retry(
    path: str | Path,
    *,
    read_only: bool = True,
    retries: int = 15,
    delay: float = 2.0,
    backoff: float = 1.0,
    max_delay: float | None = None,
):
    """Raw duckdb.connect with lock-retry — for non-Store databases
    (stream archive, shadow ledger). Same rationale as open_retry:
    writers flush in bursts and readers attach briefly; colliding is
    normal, dying on it is not.

    The defaults (15 x 2.0s = 30s, flat) were calibrated for that brief
    reader. They are NOT adequate against a 24/7 writer: `collector.
    streamd` holds `hyxstream.duckdb` continuously, and a flat 2s period
    can beat against its flush period rather than sample it. Callers
    attaching a daemon-held database should pass a larger budget and
    `backoff > 1.0`, which both lengthens the budget and detunes it from
    any fixed flush period. `max_delay` caps the per-attempt sleep so a
    long budget stays responsive once the writer lets go.
    """
    import time

    budget = attach_budget_s(retries, delay, backoff, max_delay)
    started = time.monotonic()
    # MEASURED, not summed from `wait`: a stubbed sleep must read as zero
    # slept (the atlas busy-line test turns on exactly that) and a real one
    # overshoots its argument. See mistakes #78.
    slept = 0.0
    wait = delay
    for attempt in range(retries):
        try:
            conn = duckdb.connect(str(path), read_only=read_only)
            private_spill(conn, path)
            cgroup_memory_limit(conn)
            spill_cap(conn, path)
            _record_attach(path, attempt + 1, time.monotonic() - started, budget, True, slept)
            return conn
        except duckdb.Error:
            if attempt == retries - 1:
                # Recorded BEFORE the raise: an exhausted budget is the one
                # observation the ledger most needs, and the caller re-raises
                # out of every frame that could have recorded it.
                _record_attach(path, attempt + 1, time.monotonic() - started, budget, False, slept)
                raise
            before = time.monotonic()
            time.sleep(wait)
            slept += time.monotonic() - before
            wait *= backoff
            if max_delay is not None:
                wait = min(wait, max_delay)
    raise AssertionError("unreachable")


def open_retry(
    path: str | Path = "data/hyxlab.duckdb",
    *,
    read_only: bool = False,
    retries: int = 30,
    delay: float = 2.0,
) -> Store:
    """Open the store, waiting out whoever briefly holds the file lock.

    DuckDB excludes a writer while ANY reader is attached (and vice
    versa), so a QA run, doctor, backtest, or simui session can make a
    bare Store() raise mid-run — which can kill a multi-hour sweep at
    an unguarded flush. Readers hold the file for seconds; waiting is
    almost always right for a writer that must not die."""
    import time

    budget = attach_budget_s(retries, delay)
    started = time.monotonic()
    slept = 0.0
    for attempt in range(retries):
        try:
            store = Store(path, read_only=read_only)
            _record_attach(path, attempt + 1, time.monotonic() - started, budget, True, slept)
            return store
        except duckdb.Error:
            if attempt == retries - 1:
                _record_attach(path, attempt + 1, time.monotonic() - started, budget, False, slept)
                raise
            before = time.monotonic()
            time.sleep(delay)
            slept += time.monotonic() - before
    raise AssertionError("unreachable")


class Store:
    def __init__(self, path: str | Path = "data/hyxlab.duckdb", read_only: bool = False) -> None:
        p = Path(path)
        fresh = not p.exists()
        if p.parent != Path(".") and not read_only:
            p.parent.mkdir(parents=True, exist_ok=True)
        self.conn = duckdb.connect(str(p), read_only=read_only)
        private_spill(self.conn, p)
        cgroup_memory_limit(self.conn)
        spill_cap(self.conn, p)
        if not read_only:
            self.conn.execute(_SCHEMA)
            if fresh:
                # Fresh DBs are born current; only pre-existing data migrates.
                self.conn.execute("INSERT INTO schema_meta VALUES (?)", [SCHEMA_VERSION])

    def schema_version(self) -> int:
        row = self.conn.execute("SELECT max(version) FROM schema_meta").fetchone()
        return row[0] if row and row[0] is not None else 0

    def set_schema_version(self, v: int) -> None:
        self.conn.execute("DELETE FROM schema_meta")
        self.conn.execute("INSERT INTO schema_meta VALUES (?)", [v])

    def insert_new(self, table: str, rows: list[tuple], key_cols: list[str]) -> int:
        """Anti-join insert: only rows whose natural key is absent. Idempotent
        re-runs of any backfill/sweep are safe (P5)."""
        if not rows:
            return 0
        self.conn.execute(f"CREATE OR REPLACE TEMP TABLE _staging AS SELECT * FROM {table} LIMIT 0")
        placeholders = ",".join("?" * len(rows[0]))
        self.conn.executemany(f"INSERT INTO _staging VALUES ({placeholders})", rows)
        on = " AND ".join(f"t.{k} IS NOT DISTINCT FROM s.{k}" for k in key_cols)
        before = self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        self.conn.execute(
            f"INSERT INTO {table} SELECT s.* FROM _staging s"
            f" WHERE NOT EXISTS (SELECT 1 FROM {table} t WHERE {on})"
        )
        after = self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        self.conn.execute("DROP TABLE _staging")
        return after - before

    def close(self) -> None:
        self.conn.close()

    # -- writes ---------------------------------------------------------

    def upsert_markets(self, infos: list[MarketInfo]) -> None:
        """Set-based staged upsert, not executemany (EXP-963).

        `executemany` of INSERT OR REPLACE runs one statement per row
        against the PK index — measured 11.0s for a production-scale
        cycle (5,913 rows into 324k markets), which was most of the
        collect cycle's lock hold. Staging + one set-based OR REPLACE
        does the same work in ~1.0s. `seq` preserves executemany's
        last-wins semantics on duplicate keys within a batch, which
        OR REPLACE over a SELECT does not guarantee (DuckDB keeps an
        arbitrary source row). The empty-batch guard is also load-bearing:
        executemany raises on an empty parameter list, so a cycle whose
        every fetch failed would have rolled back its whole write.
        """
        if not infos:
            return
        now = datetime.now(UTC).replace(tzinfo=None)
        rows = [
            (
                i.venue,
                i.market_id,
                i.title,
                i.series,
                _naive_utc(i.close_time),
                i.strike_type,
                i.floor_strike,
                i.cap_strike,
                i.result,
                i.target_date,
                now,
                _naive_utc(i.open_time),
                seq,
            )
            for seq, i in enumerate(infos)
        ]
        self.conn.execute(
            "CREATE OR REPLACE TEMP TABLE _mkstage AS"
            " SELECT *, 0::BIGINT AS seq FROM markets LIMIT 0"
        )
        self.conn.executemany("INSERT INTO _mkstage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        self.conn.execute(
            "INSERT OR REPLACE INTO markets"
            " SELECT * EXCLUDE (seq) FROM _mkstage"
            " QUALIFY row_number() OVER (PARTITION BY venue, market_id ORDER BY seq DESC) = 1"
        )
        self.conn.execute("DROP TABLE _mkstage")

    def insert_snapshots(self, snaps: list[Snapshot]) -> None:
        rows = [
            (
                s.venue,
                s.market_id,
                _naive_utc(s.ts),
                s.yes_bid,
                s.yes_ask,
                s.no_bid,
                s.no_ask,
                s.yes_bid_size,
                s.yes_ask_size,
                s.no_bid_size,
                s.no_ask_size,
                s.last_price,
                s.volume,
                s.open_interest,
            )
            for s in snaps
        ]
        self.insert_new("snapshots", rows, ["venue", "market_id", "ts"])

    def insert_breadth_snapshots(self, rows: list[tuple]) -> int:
        """Rows in breadth_snapshots column order (see _SCHEMA).

        `ts` is normalised here rather than by the caller: every new
        writer that passed tz-aware datetimes straight to DuckDB has
        landed box-local rows (mistakes log #1, recurring as #10), so the
        conversion belongs inside the writer, not next to it.
        """
        rows = [(r[0], r[1], _naive_utc(r[2]), *r[3:]) for r in rows]
        return self.insert_new("breadth_snapshots", rows, ["venue", "market_id", "ts"])

    def insert_breadth_cycle(
        self,
        ts: datetime,
        universe: int,
        picked: int,
        inserted: int,
        truncated: bool,
        cutoff_volume_24h: float,
    ) -> None:
        """One breadth cycle's own account of itself (see _SCHEMA).

        Written even when the cycle picked NOTHING — that is the case the
        table exists for. `ts` is normalised here for the same reason
        `insert_breadth_snapshots` does it (mistakes #1/#10).
        """
        self.conn.execute(
            "INSERT INTO breadth_cycles VALUES (?,?,?,?,?,?)",
            [_naive_utc(ts), universe, picked, inserted, truncated, cutoff_volume_24h],
        )

    def upsert_series(self, rows: list[tuple]) -> None:
        """(venue, ticker, title, category, fee_type, fee_multiplier, frequency)."""
        now = datetime.now(UTC).replace(tzinfo=None)
        self.conn.executemany(
            "INSERT OR REPLACE INTO series VALUES (?,?,?,?,?,?,?,?)",
            [(*r, now) for r in rows],
        )

    def series_meta(self, venue: str = "kalshi") -> dict[str, dict]:
        rows = self.conn.execute(
            "SELECT ticker, title, category, fee_type, fee_multiplier, frequency"
            " FROM series WHERE venue = ?",
            [venue],
        ).fetchall()
        return {
            r[0]: {
                "title": r[1],
                "category": r[2],
                "fee_type": r[3],
                "fee_multiplier": r[4],
                "frequency": r[5],
            }
            for r in rows
        }

    def watermark(self, series: str) -> datetime | None:
        row = self.conn.execute(
            "SELECT last_close_ts FROM watermarks WHERE series = ?", [series]
        ).fetchone()
        return row[0] if row else None

    def set_watermark(self, series: str, last_close_ts: datetime) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO watermarks VALUES (?, ?)",
            [series, _naive_utc(last_close_ts)],
        )

    def log_sweep(
        self,
        series: str,
        min_close,
        max_close,
        n_markets: int,
        n_candles: int,
        status: str,
        note: str = "",
    ) -> None:
        self.conn.execute(
            "INSERT INTO sweep_log VALUES (?,?,?,?,?,?,?,?)",
            [
                series,
                datetime.now(UTC).replace(tzinfo=None),
                _naive_utc(min_close),
                _naive_utc(max_close),
                n_markets,
                n_candles,
                status,
                note,
            ],
        )

    def insert_forecasts(self, fcs: list[Forecast]) -> None:
        rows = [
            (f.station, _naive_utc(f.fetched_at), f.target_date, f.high_f, f.short) for f in fcs
        ]
        self.insert_new("nws_forecasts", rows, ["station", "fetched_at", "target_date"])

    def insert_candles(self, rows: list[tuple]) -> int:
        """Rows in candles-table column order (see _SCHEMA)."""
        rows = [(r[0], r[1], _naive_utc(r[2]), *r[3:]) for r in rows]
        return self.insert_new("candles", rows, ["venue", "market_id", "end_ts", "period_s"])

    def insert_trades(self, rows: list[tuple]) -> int:
        """(venue, market_id, trade_id, ts, yes_price, qty, taker_side,
        is_block); dedup on trade_id so retro-pass re-runs are safe."""
        rows = [(*r[:3], _naive_utc(r[3]), *r[4:]) for r in rows]
        return self.insert_new("trades", rows, ["trade_id"])

    def mark_trades_swept(self, market_id: str, n_trades: int, status: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO trades_swept VALUES (?,?,?,?)",
            [market_id, datetime.now(UTC).replace(tzinfo=None), n_trades, status],
        )

    def trades_swept_ids(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT market_id FROM trades_swept").fetchall()}

    def insert_vintages(self, vintages: list[EconVintage]) -> int:
        """Idempotent: a vintage re-fetched on a later day dedups away;
        only genuinely new (series, period, release) rows land."""
        rows = [(v.series_id, v.obs_date, v.value, _naive_utc(v.knowable_at)) for v in vintages]
        return self.insert_new("econ_vintages", rows, ["series_id", "obs_date", "knowable_at"])

    def insert_news(self, items: list[NewsItem]) -> int:
        rows = [
            (
                n.source,
                n.url_hash,
                _naive_utc(n.published_at),
                _naive_utc(n.knowable_at),
                n.title,
                n.tone,
                n.topics,
                n.symbols,
            )
            for n in items
        ]
        return self.insert_new("news_items", rows, ["source", "url_hash"])

    def insert_poly_prices(self, rows: list[tuple]) -> int:
        """(token_id, market_id, outcome, ts, price); dedup (token, ts)."""
        rows = [(*r[:3], _naive_utc(r[3]), r[4]) for r in rows]
        return self.insert_new("poly_prices", rows, ["token_id", "ts"])

    def insert_poly_stats(self, rows: list[tuple]) -> None:
        """(market_id, ts, volume, liquidity) — one row per market per sweep
        RUN, sharing that run's start instant, so `ts` doubles as the run id
        and `count(DISTINCT market_id) GROUP BY ts` is the enumerated
        universe. Normalized like every other write: the caller passes an
        aware UTC `now`, and without _naive_utc DuckDB would silently store
        the box's local time (this table shipped that way until 2026-08-23;
        see migration_2)."""
        rows = [(r[0], _naive_utc(r[1]), *r[2:]) for r in rows]
        self.conn.executemany("INSERT INTO poly_market_stats VALUES (?,?,?,?)", rows)

    def insert_poly_tail_stops(self, rows: list[tuple]) -> int:
        """(market_id, stopped_at, prints, status) — one row per `trades_tail`
        call that ended on an error status instead of the end of the tape.

        The COUNT of these was already published (`http_retries
        .tail_truncated`) and the count is the one thing that cannot answer
        the question the counter exists for. "The next sweep re-fetches the
        tail" is a claim about DEPTH per market: it is true exactly when the
        archived tail later exceeds the prints that truncated pass returned,
        and the count discards both the market and the depth. Verifying it on
        2026-09-24 therefore needed a journal scrape for 1,358 log lines plus
        two archive queries — the scrape the counter was added to retire.
        With these rows the same check is SQL (`qa_poly_tail_absorbed`).

        Keyed (market_id, stopped_at) so a re-flushed batch dedups; a market
        that stops early on several days keeps one row per day."""
        rows = [(r[0], _naive_utc(r[1]), *r[2:]) for r in rows]
        return self.insert_new("poly_tail_stops", rows, ["market_id", "stopped_at"])

    def poly_price_watermarks(self) -> dict[str, datetime]:
        """token_id -> latest captured price ts (for incremental backfill)."""
        rows = self.conn.execute(
            "SELECT token_id, max(ts) FROM poly_prices GROUP BY token_id"
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def upsert_observations(self, rows: list[tuple[str, date, int | None]]) -> None:
        self.conn.executemany("INSERT OR REPLACE INTO observations VALUES (?,?,?)", rows)

    # -- reads ----------------------------------------------------------

    def markets(
        self,
        venue: str | None = None,
        alive_days: float | None = None,
        include: Iterable[tuple[str, str]] = (),
        market_ids: Iterable[str] | None = None,
    ) -> dict[tuple[str, str], MarketInfo]:
        """Market metadata keyed (venue, market_id). Unfiltered by default.

        The full table is 1.87M rows / **1.32 GiB resident, 1.56 GiB
        peak** as MarketInfo objects (measured 2026-08-27, EXP-1378; it
        was ~486k rows / ~430MB on 08-07, so this grows with the ARCHIVE
        and nothing a caller chooses bounds it) — holders must filter or
        they eat their cgroup cap. `alive_days` keeps a market while it
        is unsettled OR closed within that many days; `market_ids`
        restricts to a known id set (a REPLAY knows exactly which
        markets its window can touch — 714 of 1.87M for a 3 h window,
        and the empty set means the empty load, not the whole table);
        `include` pins specific (venue, market_id) keys past any filter
        — a held position whose settlement lands after the recency
        window would otherwise vanish from a reload and never credit its
        payout.
        """
        clauses = []
        params: list = []
        if market_ids is not None:
            clauses.append("market_id = ANY(?::VARCHAR[])")
            params.append(list(market_ids))
        if venue is not None:
            clauses.append("venue = ?")
            params.append(venue)
        if alive_days is not None:
            cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=alive_days)
            clauses.append("(coalesce(result, '') = '' OR close_time >= ?)")
            params.append(cutoff)
        where = " AND ".join(clauses)
        include = list(include)
        if include:
            pins = " OR ".join(["(venue = ? AND market_id = ?)"] * len(include))
            where = f"({where}) OR {pins}" if where else pins
            for v, m in include:
                params += [v, m]
        sql = (
            "SELECT venue, market_id, title, series, close_time, strike_type,"
            " floor_strike, cap_strike, result, target_date FROM markets"
        )
        if where:
            sql += f" WHERE {where}"
        rows = self.conn.execute(sql, params).fetchall()
        out: dict[tuple[str, str], MarketInfo] = {}
        for r in rows:
            info = MarketInfo(
                venue=r[0],
                market_id=r[1],
                title=r[2] or "",
                series=r[3] or "",
                close_time=r[4],
                strike_type=r[5] or "",
                floor_strike=r[6],
                cap_strike=r[7],
                result=r[8] or "",
                target_date=r[9],
            )
            out[(info.venue, info.market_id)] = info
        return out

    def iter_snapshots(self) -> list[Snapshot]:
        """All snapshots in replay order (ts, then venue/market for stability)."""
        rows = self.conn.execute(
            "SELECT venue, market_id, ts, yes_bid, yes_ask, no_bid, no_ask,"
            " yes_bid_size, yes_ask_size, no_bid_size, no_ask_size,"
            " last_price, volume, open_interest"
            " FROM snapshots ORDER BY ts, venue, market_id"
        ).fetchall()
        return [
            Snapshot(
                venue=r[0],
                market_id=r[1],
                ts=r[2],
                yes_bid=r[3],
                yes_ask=r[4],
                no_bid=r[5],
                no_ask=r[6],
                yes_bid_size=r[7] or 0.0,
                yes_ask_size=r[8] or 0.0,
                no_bid_size=r[9] or 0.0,
                no_ask_size=r[10] or 0.0,
                last_price=r[11],
                volume=r[12] or 0.0,
                open_interest=r[13] or 0.0,
            )
            for r in rows
        ]

    def forecasts(self) -> list[Forecast]:
        rows = self.conn.execute(
            "SELECT station, fetched_at, target_date, high_f, short"
            " FROM nws_forecasts ORDER BY fetched_at"
        ).fetchall()
        return [
            Forecast(station=r[0], fetched_at=r[1], target_date=r[2], high_f=r[3], short=r[4] or "")
            for r in rows
        ]

    def candles_as_snapshots(self) -> list[Snapshot]:
        """Synthesize Tier-1 snapshots from historical candle closes.

        NO-side quotes are the binary complement of the YES book
        (no_ask = 1 − yes_bid). Sizes are unknown at candle granularity, so
        they're set to +inf: the sim then caps fills at order qty and
        strategies must self-limit via max_qty — an optimistic fill model,
        which is why Tier 1 can kill strategies but not green-light them.
        """
        # Correctness gate (2026-07-07): candle bid/ask closes are sampled
        # at different sub-hour moments and can be crossed (bid > ask) or
        # carry empty-book sentinels (ask=1 with bid=0) — verified sums
        # ranged 0.11–2.00. Filling at such phantom quotes is fiction, so
        # they are excluded from replay.
        rows = self.conn.execute(
            "SELECT venue, market_id, end_ts, yes_bid_close, yes_ask_close,"
            " price_close, volume, open_interest FROM candles"
            " WHERE yes_bid_close IS NULL OR yes_ask_close IS NULL"
            "    OR (yes_bid_close <= yes_ask_close"
            "        AND NOT (yes_ask_close >= 0.999 AND yes_bid_close <= 0.001))"
            " ORDER BY end_ts, venue, market_id"
        ).fetchall()
        inf = float("inf")
        out: list[Snapshot] = []
        for r in rows:
            yes_bid, yes_ask = r[3], r[4]
            out.append(
                Snapshot(
                    venue=r[0],
                    market_id=r[1],
                    ts=r[2],
                    yes_bid=yes_bid,
                    yes_ask=yes_ask,
                    no_bid=None if yes_ask is None else 1.0 - yes_ask,
                    no_ask=None if yes_bid is None else 1.0 - yes_bid,
                    yes_bid_size=inf,
                    yes_ask_size=inf,
                    no_bid_size=inf,
                    no_ask_size=inf,
                    last_price=r[5],
                    volume=r[6] or 0.0,
                    open_interest=r[7] or 0.0,
                )
            )
        return out

    def observations(self) -> dict[tuple[str, date], int]:
        rows = self.conn.execute(
            "SELECT station, obs_date, high_f FROM observations WHERE high_f IS NOT NULL"
        ).fetchall()
        return {(r[0], r[1]): r[2] for r in rows}

    def mirror_violations(self, tol: float = 0.005) -> int:
        """Kalshi mirror-invariant tripwire.

        Kalshi runs ONE mirrored book: no_ask ≡ 1 − yes_bid and
        no_bid ≡ 1 − yes_ask by venue construction (0 violations observed
        live). A violation therefore never signals opportunity — it means
        the pipeline corrupted quote fields (swapped sides, stale merge,
        unit slip). Checked by `sweep --doctor`; nonzero = investigate.
        """
        return self.conn.execute(
            "SELECT count(*) FROM snapshots WHERE venue = 'kalshi' AND ("
            " (no_ask IS NOT NULL AND yes_bid IS NOT NULL"
            "  AND abs(no_ask - (1 - yes_bid)) > ?)"
            " OR (no_bid IS NOT NULL AND yes_ask IS NOT NULL"
            "  AND abs(no_bid - (1 - yes_ask)) > ?))",
            [tol, tol],
        ).fetchone()[0]

    def counts(self) -> dict[str, int]:
        return {
            t: self.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("markets", "snapshots", "nws_forecasts", "candles", "trades", "observations")
        }


def target_date_key(d: date) -> str:
    return d.isoformat()
