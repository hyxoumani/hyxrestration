"""Kalshi public market-data client (no auth required for reads).

The /markets endpoint already carries top-of-book (yes/no bid/ask in
dollars plus displayed sizes), so one paginated call per series yields
snapshots for every strike bracket — no per-market orderbook calls needed
at this fidelity. Public rate limit is ~30 req/s; the collector polls at
minutes-scale, far below it.

Weather series (KXHIGHNY, KXHIGHCHI, ...) settle on the NWS Climatological
Report (Daily) — objective, no oracle risk. Event tickers encode the
measured local date as e.g. "KXHIGHNY-26JUL07".
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import requests

BASE = "https://api.elections.kalshi.com/trade-api/v2"
VENUE = "kalshi"

_MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}

from hyxlab.models import MarketInfo, Snapshot  # noqa: E402

# EXP-1333 (hylshi). Sink for the headers of ORGANIC 429 responses. Kalshi's
# 2xx responses carry no rate-limit headers (measured, hylshi EXP-1326) and a
# load test is forbidden, so the only way to learn the Retry-After/limit
# semantics is to capture ALL headers when a 429 naturally occurs — routine
# here during sweeps (~6 in 2h measured 2026-08-18). Relative path, same
# convention as collector/collect.py's SKIP_LOG (cwd = repo root under the
# systemd units). Additive telemetry only; retry behavior is unchanged.
RATE_LIMIT_HEADERS_LOG = "data/rate_limit_headers.jsonl"


def _log_429_headers(resp: Any, url: str) -> None:
    """Append ALL headers of one 429 response to the JSONL sink. One line per
    429 response observed; called on the 429 branch only. Never raises — a
    logging failure must not turn a retryable 429 into a lost series."""
    import json
    import os

    try:
        headers = dict(getattr(resp, "headers", None) or {})
        row = {
            "ts": datetime.now(UTC).isoformat(),
            "source": "collector/venues/kalshi.py",
            "url": url,
            "status": 429,
            "headers": headers,
        }
        log_path = RATE_LIMIT_HEADERS_LOG
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        with open(log_path, "a") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except Exception as e:  # noqa: BLE001 — telemetry only
        print(f"[kalshi] WARNING: failed to log 429 headers for {url}: {e}", flush=True)


# A transport error is not a 429. `requests` raises these BEFORE any response
# object exists, so the status-code branch in _get_with_429_retry — which can
# only run once a response has come back — is structurally incapable of seeing
# them. Measured 2026-09-08: two `ReadTimeout`s (12:47Z, 18:47Z) each killed a
# whole `collector.breadth` cycle, discarding the 8 pages that had already
# succeeded, and each recovered unaided on the next 5-minute firing. Retrying
# is safe because every request here is a GET with an explicit cursor: re-
# issuing the same page returns the same page.
#
# Timeout covers ReadTimeout/ConnectTimeout; ConnectionError covers DNS and
# refused/reset sockets. Deliberately NOT RequestException — that would also
# swallow HTTPError and TooManyRedirects, which are answers, not lost packets.
_TRANSPORT_ERRORS = (requests.exceptions.Timeout, requests.exceptions.ConnectionError)

# THE 2026-09-21 CORRECTION. The comment above drew its line in the wrong
# place: at whether a RESPONSE OBJECT EXISTS, which is a fact about Python's
# exception plumbing, not about whether a retry can succeed. A 504 Gateway
# Time-out says an intermediary gave up waiting for the origin — the origin
# never answered — which is the SAME physical event as a ReadTimeout, and
# differs only in whether Kalshi's edge ran out of patience before our 30s
# `timeout` did. So the clause "HTTPError ... are answers, not lost packets"
# is true of 4xx and false of the gateway class, and the 09-08 fix shipped
# retrying one twin while leaving the other fatal.
#
# Measured, 15 days of journal: FOUR 504s, every one of them on
# `collector.breadth`, every one killing a whole cycle mid-walk with a live
# cursor, and every one recovering unaided on the next 5-minute firing —
# 4/4, the exact signature the 09-08 fix was written for.
#
# THE THREE ON 09-13 WERE SEEN AND DISMISSED, and that is why this class
# survived a fix aimed at it. The 09-14 08:30Z status entry triaged them as
# "the same flood: 504 Gateway Time-out while paging deep into the 250k-row
# walk ... not a separate fault". 09-21 19:37Z falsifies that: it fired on a
# 9,229-market, ~10-page, 8.5-second walk with `truncated: False`, i.e. the
# healthy post-`mve_filter` configuration. Walk depth was a coincidence of
# timing, not a cause; a gateway does not care how many pages preceded it.
#
# 500 IS DELIBERATELY EXCLUDED, on this repo's own evidence: a 500 means the
# origin DID answer, and the one 500 we have a record of is persistent, not
# transient — Polymarket's Gamma tail fault (probed 2026-08-22) answers the
# last page of a long walk with a 500 on demand, reproduced in four daylight
# probes. Retrying that burns the budget on a request that cannot succeed.
# 502/503/504 are the "no answer from the origin" statuses; they are what a
# lost packet looks like once a CDN is in the path.
_GATEWAY_STATUSES = (502, 503, 504)

# Retries are budgeted per WALK, not per request, and this is why: breadth is a
# oneshot on a 5-minute timer walking ~9 pages, so a per-request budget makes
# the worst case 9x the retry cost and a network-wide outage would push a cycle
# past the next firing. Two retries per walk bounds the MARGINAL cost at
# 2x30s reads + 2s + 4s backoff = 66s over what the same walk costs today.
# systemd will not run two copies of a oneshot concurrently, so overrunning
# delays the next cycle rather than duplicating it — but a delayed cycle stamps
# a late quote, which is the thing this collector exists not to do.
TRANSPORT_TRIES = 2
_TRANSPORT_BACKOFF_S = 2.0

# Counts of retries actually spent, PER CLASS. A retried fault no longer fails
# the unit, so it vanishes from the health digest's failure history — the rate
# has to surface SOMEWHERE or this fix buys the lost cycle back by making the
# underlying network fault invisible, which is the 2026-09-06 truncation
# mistake wearing new clothes. Callers reset them and report them per cycle.
# Safe as module state only because every reader here is a single-threaded
# oneshot.
#
# SPLIT BY CLASS, not pooled, because the three mean different things to an
# operator: `transport` is our own socket, `gateway` is Kalshi's edge failing
# to reach Kalshi, `rate_limit` is us being told to slow down. A pooled
# counter rising says only "the network is worse"; these say who to ask.
#
# `rate_limit` is new here and closes a #71-class hole of its own: the 429
# ladder has existed since 2026-08-02 and counted NOTHING, so the field
# `collector.breadth` published as `http_retries` was blind to the one retry
# class that predated it. 3,796 of 3,799 archived cycles read 0 and three
# read 1; none of those zeros could ever have been a 429.
#
# A COUNT IS NOT A CONSUMPTION, AND FOR THIS CLASS THE TWO COME APART. The
# transport/gateway allowance is per WALK, so its count and its budget
# fraction carry the same information and `_TransportBudget.budget_frac`
# reports it. The 429 ladder is per REQUEST -- `_get_with_429_retry` builds a
# fresh `tries`-attempt loop for every page, and `get_markets` walks up to
# `max_pages` of them -- so the SUM below cannot say how close any one page
# came to failing. Measured on the first 48h of production readings
# (2026-09-21..23, 421 breadth cycles): 19 cycles spent rate-limit retries,
# 12 at 1 and 7 at 2, against 1 transport and 1 gateway all window. A `2` is
# either one page at 2 of its 3 allowed -- one 429 from failing the cycle --
# or two pages at 1 of 3, which is not close to anything, and the published
# number does not distinguish them. mistakes #71 at the site #73 created: the
# class that actually fires in production is the one whose ladder publishes
# only what it counted, never what it spent.
#
# THE WORST REQUEST, NOT THE MEAN, because the failure is per request: one
# page exhausting its ladder raises and kills the walk however comfortable
# every other page was. Averaging over pages would be a statistic whose value
# FALLS as the walk gets longer, reporting the deepest walks as the safest.
_TRANSPORT_RETRIES = 0
_GATEWAY_RETRIES = 0
_RATE_LIMIT_RETRIES = 0
_RATE_LIMIT_WORST_FRAC = 0.0

#: Attempts per request in the 429 ladder, so the ALLOWANCE is one fewer --
#: the last attempt does not retry, it `raise_for_status()`es. The fraction
#: below is over the allowance, matching `_TransportBudget.budget_frac`: 1.0
#: means the next 429 on that request fails the cycle.
RATE_LIMIT_TRIES = 4


def transport_retries() -> int:
    """Transport retries spent since the last `reset_retry_counts()`."""
    return _TRANSPORT_RETRIES


def gateway_retries() -> int:
    """Gateway-status (502/503/504) retries spent since the last reset."""
    return _GATEWAY_RETRIES


def rate_limit_retries() -> int:
    """429 retries spent since the last reset."""
    return _RATE_LIMIT_RETRIES


def rate_limit_budget_frac() -> float:
    """Share of ONE request's 429 allowance spent by the WORST request since
    the last reset, in [0, 1]. See the block above `_RATE_LIMIT_RETRIES` for
    why the sum of retries cannot answer this and why the worst request is
    the right reduction."""
    return _RATE_LIMIT_WORST_FRAC


def retry_counts() -> dict[str, int]:
    """Every retry class spent since the last `reset_retry_counts()`.

    `total` is the sum and is what a caller should publish under a name like
    `http_retries`; the per-class members are what makes a rising total
    actionable.
    """
    return {
        "transport": _TRANSPORT_RETRIES,
        "gateway": _GATEWAY_RETRIES,
        "rate_limit": _RATE_LIMIT_RETRIES,
        "total": _TRANSPORT_RETRIES + _GATEWAY_RETRIES + _RATE_LIMIT_RETRIES,
    }


def reset_retry_counts() -> None:
    global _TRANSPORT_RETRIES, _GATEWAY_RETRIES, _RATE_LIMIT_RETRIES
    global _RATE_LIMIT_WORST_FRAC
    _TRANSPORT_RETRIES = 0
    _GATEWAY_RETRIES = 0
    _RATE_LIMIT_RETRIES = 0
    _RATE_LIMIT_WORST_FRAC = 0.0


class _TransportBudget:
    """A retry allowance shared by every request in one walk."""

    def __init__(self, tries: int = TRANSPORT_TRIES) -> None:
        self.tries = tries
        self.remaining = tries
        self.delay = _TRANSPORT_BACKOFF_S
        # mistakes #71: a budget whose only observable is succeeded/raised
        # reports full health at 99% consumption. This ladder now serves two
        # fault classes off one allowance, so how much of it a SUCCESSFUL
        # walk consumed is the thing that goes from 0 to fatal without ever
        # being reported in between.
        self.spent = 0
        self.waited_s = 0.0

    def take(self) -> float | None:
        """Consume one retry, returning how long to wait; None when spent."""
        if self.remaining <= 0:
            return None
        self.remaining -= 1
        wait = self.delay
        self.delay *= 2
        self.spent += 1
        self.waited_s += wait
        return wait

    def budget_frac(self) -> float:
        """Share of the walk's retry allowance this walk actually spent."""
        return self.spent / self.tries if self.tries else 0.0


def _get_transport_retrying(
    sess: requests.Session,
    url: str,
    params: dict[str, Any],
    timeout: int,
    budget: _TransportBudget,
) -> requests.Response:
    """One GET, retrying "the origin did not answer" until `budget` is spent.

    Two fault classes, one allowance, because they are one event: a lost
    packet raised by `requests` before any response exists, and a 502/503/504
    handed back by an intermediary that itself gave up on the origin. See
    `_GATEWAY_STATUSES` for why the 09-08 version caught only the first.

    Bounded and then re-raised or returned-as-is, never swallowed: a
    persistent outage must still fail the unit, so an exhausted budget
    re-raises the transport error and returns the gateway response for
    `raise_for_status()` to turn into the HTTPError it always was. What this
    removes is the case where one blip in a multi-page walk throws away
    every page that already succeeded.
    """
    import time as _time

    global _TRANSPORT_RETRIES, _GATEWAY_RETRIES
    while True:
        try:
            resp = sess.get(url, params=params, timeout=timeout)
        except _TRANSPORT_ERRORS as e:
            wait = budget.take()
            if wait is None:
                raise
            _TRANSPORT_RETRIES += 1
            print(
                f"[kalshi] WARNING: transport retry in {wait:.0f}s"
                f" ({budget.remaining} left this walk) for {url}:"
                f" {type(e).__name__}: {e}",
                flush=True,
            )
            _time.sleep(wait)
            continue

        if resp.status_code not in _GATEWAY_STATUSES:
            return resp
        wait = budget.take()
        if wait is None:
            # Budget spent. Hand the gateway response back unchanged so the
            # caller's `raise_for_status()` fails the unit exactly as it did
            # before this retry existed.
            return resp
        _GATEWAY_RETRIES += 1
        print(
            f"[kalshi] WARNING: gateway retry in {wait:.0f}s"
            f" ({budget.remaining} left this walk) for {url}:"
            f" HTTP {resp.status_code}",
            flush=True,
        )
        _time.sleep(wait)


def _get_with_429_retry(
    sess: requests.Session,
    url: str,
    params: dict[str, Any],
    timeout: int = 30,
    tries: int = RATE_LIMIT_TRIES,
    transport_budget: _TransportBudget | None = None,
) -> requests.Response:
    """GET honoring 429 Retry-After with capped exponential fallback, and
    retrying transport errors against a separate, per-walk budget.

    Measured live defect (sweep audit 2026-08-02): a 429 inside the
    get_markets page loop escaped to run_sweep's except, so the sweep of
    KXNASDAQ100U failed 5 consecutive days without advancing its watermark —
    4,947 closed markets unarchived while inside Kalshi's ~60-90d purge
    window. The candles path had per-request 429 handling; the markets page
    loop did not.

    The two budgets are separate on purpose: sharing one counter would let
    three 429s — the case it was already handling correctly — leave a
    subsequent timeout with no retry at all. The GATEWAY class shares the
    transport allowance rather than getting a third, because a 504 and a
    ReadTimeout are one event (see `_GATEWAY_STATUSES`) and the per-walk
    bound exists to cap total added wall-clock, which a second allowance
    would double.
    """
    import time as _time

    global _RATE_LIMIT_RETRIES, _RATE_LIMIT_WORST_FRAC

    budget = transport_budget if transport_budget is not None else _TransportBudget()
    # THIS request's consumption. Local, because the allowance is per request
    # and the module-level worst is a reduction over these -- see the block
    # above `_RATE_LIMIT_RETRIES`.
    # NOT a separate counter: every iteration that does not return reaches
    # the retry below, so the retries spent by THIS request are exactly
    # `attempt + 1` and a second variable could only drift from it.
    allowance = max(tries - 1, 0)
    delay = 5.0
    for attempt in range(tries):
        resp = _get_transport_retrying(sess, url, params, timeout, budget)
        if resp.status_code == 429:
            _log_429_headers(resp, url)  # EXP-1333: capture only, no behavior change
        if resp.status_code != 429 or attempt == tries - 1:
            resp.raise_for_status()
            return resp
        retry_after = resp.headers.get("Retry-After")
        try:
            wait = float(retry_after) if retry_after else delay
        except ValueError:
            wait = delay
        _RATE_LIMIT_RETRIES += 1
        spent = attempt + 1
        if allowance:
            _RATE_LIMIT_WORST_FRAC = max(_RATE_LIMIT_WORST_FRAC, spent / allowance)
        # Printed for the same reason the transport and gateway ladders print,
        # and it is the reason this hole stayed open: those two put a line in
        # the journal naming what was left, and the 429 branch -- the class
        # that fires most -- put nothing anywhere. 19 retries over the first
        # 48h of production readings left exactly zero journal lines.
        print(
            f"[kalshi] WARNING: rate-limit retry in {min(wait, 60.0):.0f}s"
            f" ({allowance - spent} left this request) for {url}: HTTP 429",
            flush=True,
        )
        _time.sleep(min(wait, 60.0))
        delay *= 2
    raise AssertionError("unreachable")  # pragma: no cover


def get_markets(
    series_ticker: str | None = None,
    status: str = "open",
    limit: int = 200,
    max_pages: int = 10,
    session: requests.Session | None = None,
    pause_s: float = 0.0,
    with_truncated: bool = False,
    **extra_params: Any,
) -> list[dict[str, Any]] | tuple[list[dict[str, Any]], bool]:
    """Paginated /markets. `pause_s` paces BETWEEN pages.

    Pacing lives here rather than in the caller because a caller can only
    sleep around the whole loop: a wide enumeration (collector.breadth
    walks the entire open universe) would otherwise burst every page
    back-to-back at whatever the network allows, and this client shares
    Kalshi's rate budget with a live trading loop. Default 0.0 keeps the
    existing narrow per-series callers byte-identical.

    `with_truncated=True` returns `(markets, truncated)` instead of a bare
    list, matching `get_markets_ascending`'s existing shape. It exists
    because the TRUNCATED print below is not a signal any check can read:
    on 2026-09-06T23:32Z Kalshi's 24h-close universe crossed 60k markets,
    this function truncated on EVERY cycle for the next 15 hours, and
    `collector.breadth` — whose entire value rests on ranking an exhaustive
    enumeration — went on reporting success while it silently ranked an
    arbitrary head slice and dropped ~97% of its tape. Nothing consumed the
    print, so nothing could fail. A caller whose correctness depends on the
    walk being COMPLETE must be able to ask, in-band, whether it was.
    Default False keeps the four per-series callers byte-identical.
    """
    import time as _time

    sess = session or requests.Session()
    out: list[dict[str, Any]] = []
    cursor = ""
    budget = _TransportBudget()  # shared by every page: the WALK is the unit
    for page in range(max_pages):
        if page and pause_s:
            _time.sleep(pause_s)
        params: dict[str, Any] = {"limit": limit, "status": status, **extra_params}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if cursor:
            params["cursor"] = cursor
        resp = _get_with_429_retry(sess, f"{BASE}/markets", params, transport_budget=budget)
        body = resp.json()
        out.extend(body.get("markets", []))
        cursor = body.get("cursor") or ""
        if not cursor or not body.get("markets"):
            break
    truncated = bool(cursor)  # pages exhausted with more upstream — Gamma-offset class
    if truncated:
        print(
            f"[kalshi] get_markets TRUNCATED at {len(out)} rows"
            f" (max_pages={max_pages} exhausted, cursor live)",
            flush=True,
        )
    return (out, truncated) if with_truncated else out


# Starting (and maximum) close-time window for get_markets_ascending. The
# window HALVES on demand, so this is an upper bound, not a step: 231 of the
# 281 Crypto/Exotics series have zero settled markets in 60 days and clear a
# whole week in one request, while the ~1,700-markets/day ones narrow
# themselves down in a handful of probes. A fixed small step is what this
# replaces — measured, a fixed 6h step cost 240 requests per DORMANT series,
# ~67k requests to walk the crypto category once, which is not a rate this
# client may spend while a live trading loop shares the budget.
MARKETS_WINDOW_S = 7 * 86400
_MIN_WINDOW_S = 60
# Pages per window attempt before narrowing. Small on purpose: this is a
# DENSITY PROBE, not a budget, and every page spent on an over-wide window
# is thrown away when it narrows.
_WINDOW_MAX_PAGES = 5


def get_markets_ascending(
    series_ticker: str,
    status: str = "settled",
    *,
    min_close_ts: int,
    max_close_ts: int,
    max_markets: int | None = None,
    window_s: int = MARKETS_WINDOW_S,
    limit: int = 1000,
    session: requests.Session | None = None,
    pause_s: float = 0.0,
) -> tuple[list[dict[str, Any]], bool]:
    """Walk a close-time range in ASCENDING windows. Returns (markets, truncated).

    MEASURED DEFECT this exists to fix (EXP-931, 2026-08-02): `/markets`
    returns settled markets in DESCENDING close_time order, so a plain
    `get_markets(max_pages=N)` that exhausts its page budget keeps the
    NEWEST N*limit rows and silently drops every OLDER one. `sweep_series`
    then set the watermark to `max(close)` — the newest row it kept — so
    the dropped older range was never revisited and was permanently lost
    inside Kalshi's 60-90d purge window. Verified live: KXBNB was capped at
    exactly 10,000 rows (50 pages x 200) spanning only 07-27..08-02 of a
    60-day request, and its watermark was advanced to 08-02 anyway.

    Windowing inverts that. Each request is bounded to [lo, lo+window),
    windows are consumed oldest-first, and a window is only ACCEPTED once
    its cursor is exhausted — so whatever this returns is a CONTIGUOUS
    PREFIX from `min_close_ts`, and `max(close)` over it is an honest
    watermark no matter where we stop. Stopping early is therefore
    resumable rather than lossy, which is what makes `max_markets` safe to
    impose: it bounds how long one series may hold the sweep without
    costing any coverage.

    The window is ADAPTIVE. It starts at `window_s` and halves whenever a
    window is too dense to drain in `_WINDOW_MAX_PAGES`; rows from a
    rejected attempt are discarded so no window is ever half-accepted.
    Series density on this exchange spans four orders of magnitude
    (0/day dormant to ~6,900/day for a 15-minute series), and a fixed step
    that suits one end is either request-ruinous or truncation-prone at
    the other.

    Both bounds are inclusive on Kalshi (probed 2026-08-02), so windows use
    `max_close_ts = hi - 1` and the next `lo = hi`: exact, no overlap, no
    double-fetched candles.
    """
    import time as _time

    sess = session or requests.Session()
    out: list[dict[str, Any]] = []
    lo = min_close_ts
    window = max(_MIN_WINDOW_S, min(window_s, max_close_ts - min_close_ts + 1))
    while lo <= max_close_ts:
        hi = min(lo + window, max_close_ts + 1)
        accepted = len(out)  # rollback point for a rejected attempt
        cursor = ""
        for page in range(_WINDOW_MAX_PAGES):
            if page and pause_s:
                _time.sleep(pause_s)
            params: dict[str, Any] = {
                "limit": limit,
                "status": status,
                "series_ticker": series_ticker,
                "min_close_ts": lo,
                "max_close_ts": hi - 1,
            }
            if cursor:
                params["cursor"] = cursor
            body = _get_with_429_retry(sess, f"{BASE}/markets", params).json()
            page_markets = body.get("markets", [])
            out.extend(page_markets)
            cursor = body.get("cursor") or ""
            if not cursor or not page_markets:
                break
        if pause_s:
            _time.sleep(pause_s)
        if cursor:  # too dense to drain: narrow and retry, keeping nothing
            del out[accepted:]
            if hi - lo <= _MIN_WINDOW_S:
                # A single minute overflows the page budget. Stopping keeps
                # the prefix contiguous; continuing would reintroduce the
                # exact silent hole this function exists to remove.
                return out, True
            window = max(_MIN_WINDOW_S, (hi - lo) // 2)
            continue
        lo = hi
        if max_markets is not None and len(out) >= max_markets and lo <= max_close_ts:
            return out, True
    return out, False


#: Max characters of the joined `tickers=` value in one /markets request.
#: MEASURED against the live endpoint 2026-08-03: 250 tickers (5,869 chars)
#: returns all 250 in ONE request; 500 tickers (12,247 chars) returns HTTP
#: 414 "Request-URI Too Long". The bound is on URL LENGTH, not ticker COUNT
#: — Exotics tickers average 55 chars against ~23 for a weather bracket, so
#: batching by count would 414 on exactly the families with the most to
#: repair. 6,000 is the largest measured-good length, rounded down.
TICKERS_URL_CHARS = 6000


def batch_tickers(tickers: list[str], max_chars: int = TICKERS_URL_CHARS) -> list[list[str]]:
    """Chunk tickers into URL-length-bounded batches, PRESERVING ORDER.

    Order is preserved because the caller's order is a priority order (most
    endangered first, collector/reconcile.py); re-grouping by convenience
    would spend a truncated run's budget on whatever happened to sort well.
    A single ticker longer than `max_chars` still gets its own batch — one
    doomed request beats silently dropping it from the work order.
    """
    out: list[list[str]] = []
    cur: list[str] = []
    n = 0
    for t in tickers:
        add = len(t) + (1 if cur else 0)
        if cur and n + add > max_chars:
            out.append(cur)
            cur, n = [], 0
            add = len(t)
        cur.append(t)
        n += add
    if cur:
        out.append(cur)
    return out


def get_markets_by_tickers(
    tickers: list[str],
    session: requests.Session | None = None,
    pause_s: float = 0.0,
    max_chars: int = TICKERS_URL_CHARS,
    max_pages: int = 5,
) -> tuple[dict[str, dict[str, Any]], list[str], list[str]]:
    """Resolve explicit tickers. Returns (found, absent, undetermined).

    `/markets?tickers=a,b,c` returns the subset that still exists and
    silently OMITS the rest (probed 2026-08-03: a purged ticker also
    returns HTTP 404 on `/markets/{ticker}`). That omission is the only
    purge signal Kalshi gives, so it is returned as a first-class list
    rather than inferred by the caller.

    `undetermined` is the third state and the reason this returns a triple:
    if a batch's cursor is still live when `max_pages` runs out, the
    tickers we have not seen are of UNKNOWN status — they are neither
    repaired nor proven gone. Folding them into `absent` would write a
    permanent "this data is lost" record for data that may be sitting one
    page away, which is EXP-931's mistake with the sign flipped.
    """
    sess = session or requests.Session()
    import time as _time

    found: dict[str, dict[str, Any]] = {}
    absent: list[str] = []
    undetermined: list[str] = []
    for i, batch in enumerate(batch_tickers(tickers, max_chars)):
        if i and pause_s:
            _time.sleep(pause_s)
        cursor = ""
        seen: dict[str, dict[str, Any]] = {}
        for page in range(max_pages):
            if page and pause_s:
                _time.sleep(pause_s)
            params: dict[str, Any] = {"tickers": ",".join(batch), "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            body = _get_with_429_retry(sess, f"{BASE}/markets", params).json()
            for m in body.get("markets", []):
                seen[m["ticker"]] = m
            cursor = body.get("cursor") or ""
            if not cursor or not body.get("markets"):
                break
        found.update(seen)
        missing = [t for t in batch if t not in seen]
        if cursor:
            undetermined.extend(missing)
        else:
            absent.extend(missing)
    return found, absent, undetermined


def get_trades(
    ticker: str,
    limit: int = 1000,
    max_pages: int = 100,
    session: requests.Session | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """All public trade prints for one market (cursor-paginated).

    Returns (trades, truncated): truncated=True when max_pages ran out
    with the cursor still live — callers must record it (a truncated
    tape marked 'ok' is a permanent silent hole; the retention clock
    gives no second chance).

    Probed 2026-07-07: same string-dollar shape as the WS trade channel
    (trade_id, created_time ISO, yes_price_dollars, count_fp, taker_side,
    is_block_trade). Retention purges prints ~64 days after close —
    markets closed ≤2026-05-01 already return empty.
    """
    sess = session or requests.Session()
    out: list[dict[str, Any]] = []
    cursor = ""
    for _ in range(max_pages):
        params: dict[str, Any] = {"ticker": ticker, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        resp = sess.get(f"{BASE}/markets/trades", params=params, timeout=30)
        # getattr: adds NO new happy-path requirement on the response object
        # (a pre-existing test double here has no status_code at all).
        if getattr(resp, "status_code", None) == 429:
            # EXP-1333: the trade tape has no retry wrapper (a 429 escapes to
            # sweep_series' except and is printed there) — capture the headers
            # before raise_for_status throws them away.
            _log_429_headers(resp, f"{BASE}/markets/trades")
        resp.raise_for_status()
        body = resp.json()
        out.extend(body.get("trades", []))
        cursor = body.get("cursor") or ""
        if not cursor or not body.get("trades"):
            break
    return out, bool(cursor)


def trade_row(t: dict[str, Any]) -> tuple:
    """Flatten one API trade into the store's trades-table column order."""
    return (
        VENUE,
        t["ticker"],
        t["trade_id"],
        _parse_ts(t.get("created_time")),
        float(t["yes_price_dollars"]),
        float(t.get("count_fp") or 0.0),
        t.get("taker_side") or None,
        bool(t.get("is_block_trade", False)),
    )


def get_series_list(session: requests.Session | None = None) -> list[dict[str, Any]]:
    """All series with category/fee metadata. Verified 2026-07-06: the
    endpoint returns the full set (~11k) in one unpaginated response."""
    sess = session or requests.Session()
    resp = sess.get(f"{BASE}/series", timeout=60)
    resp.raise_for_status()
    return resp.json().get("series", [])


# Measured 2026-08-12 against the live API (EXP-1274): the candlesticks
# endpoint rejects any request whose span exceeds this many periods —
# 5000 periods returned 200, 5001 returned 400 with body
# 'requested time range with candlesticks: 5001.000000, max candlesticks:
# 5000'. Long-lived monthly markets (KXPAYROLLS-26JUL-T90000's ~298-day
# open→close span = 7150 hourly periods) hit it, and post-EXP-1271 the
# sweep retried that deterministic 400 daily.
MAX_CANDLES_PER_REQUEST = 5000


def get_candlesticks(
    series_ticker: str,
    market_ticker: str,
    start_ts: int,
    end_ts: int,
    period_interval: int = 60,
    session: requests.Session | None = None,
    pause_s: float = 0.0,
) -> list[dict[str, Any]]:
    """Historical candles (price + yes_bid/yes_ask OHLC) for one market.

    period_interval is in minutes (1, 60, or 1440). Available for settled
    markets — this is what makes Tier-1 historical backtesting possible.

    Spans over MAX_CANDLES_PER_REQUEST periods are chunked into
    limit-sized windows and stitched. Chunk boundaries are INCLUSIVE at
    both ends (measured 2026-08-12: a candle whose end_period_ts equals
    the shared boundary is returned by BOTH adjacent requests), so
    stitching dedups on end_period_ts; within a request the API returns
    ascending end_period_ts, and chunks are walked ascending, so order is
    preserved. `pause_s` paces BETWEEN chunk requests, same convention as
    get_markets' between-pages pacing.

    Goes through `_get_with_429_retry` like every other endpoint here.
    `sweep_series` has its own inline single-retry around this call, but a
    single retry is not enough: measured 2026-08-03, the KXSHIBA capture
    aborted the whole series on a candlesticks 429 after that retry, which
    `run_sweep` recorded as status='error' with zero markets kept. Capped
    exponential backoff turns that into a pause instead of a lost series.
    """
    import time as _time

    sess = session or requests.Session()
    url = f"{BASE}/series/{series_ticker}/markets/{market_ticker}/candlesticks"
    max_span_s = MAX_CANDLES_PER_REQUEST * period_interval * 60
    out: list[dict[str, Any]] = []
    seen: set[Any] = set()
    chunk_start = start_ts
    while True:
        chunk_end = min(chunk_start + max_span_s, end_ts)
        if chunk_start != start_ts and pause_s:
            _time.sleep(pause_s)
        resp = _get_with_429_retry(
            sess,
            url,
            {"start_ts": chunk_start, "end_ts": chunk_end, "period_interval": period_interval},
        )
        for c in resp.json().get("candlesticks", []):
            ts = c.get("end_period_ts")
            if ts in seen:
                continue  # boundary candle already stitched from the previous chunk
            seen.add(ts)
            out.append(c)
        if chunk_end >= end_ts:
            return out
        chunk_start = chunk_end


def candle_row(series: str, m: dict[str, Any], c: dict[str, Any], period_s: int) -> tuple:
    """Flatten one API candle into the store's candles-table column order."""

    def d(block: dict[str, Any] | None, key: str) -> float | None:
        if not block:
            return None
        v = block.get(key)
        return None if v in (None, "") else float(v)

    price, bid, ask = c.get("price"), c.get("yes_bid"), c.get("yes_ask")
    return (
        VENUE,
        m["ticker"],
        datetime.fromtimestamp(c["end_period_ts"], tz=UTC),
        period_s,
        d(price, "open_dollars"),
        d(price, "high_dollars"),
        d(price, "low_dollars"),
        d(price, "close_dollars"),
        d(bid, "close_dollars"),
        d(ask, "close_dollars"),
        d(bid, "high_dollars"),
        d(ask, "low_dollars"),
        float(c.get("volume_fp") or 0.0),
        float(c.get("open_interest_fp") or 0.0),
    )


def parse_event_date(event_ticker: str) -> date | None:
    """'KXHIGHNY-26JUL07' → date(2026, 7, 7); None if no date suffix."""
    parts = event_ticker.split("-")
    if len(parts) < 2:
        return None
    tail = parts[1]
    if len(tail) < 7:
        return None
    month = _MONTHS.get(tail[2:5].upper())
    if month is None:
        return None
    try:
        return date(2000 + int(tail[:2]), month, int(tail[5:7]))
    except ValueError:
        return None


def _dollars(m: dict[str, Any], key: str) -> float | None:
    v = m.get(key)
    if v in (None, ""):
        return None
    return float(v)


def _fp(m: dict[str, Any], key: str) -> float:
    v = m.get(key)
    if v in (None, ""):
        return 0.0
    return float(v)


def _parse_ts(v: str | None) -> datetime | None:
    if not v:
        return None
    return datetime.fromisoformat(v.replace("Z", "+00:00"))


def to_market_info(m: dict[str, Any]) -> MarketInfo:
    event = m.get("event_ticker", "")
    return MarketInfo(
        venue=VENUE,
        market_id=m["ticker"],
        title=m.get("title", ""),
        series=event.split("-")[0] if event else "",
        close_time=_parse_ts(m.get("close_time")),
        strike_type=m.get("strike_type", "") or "",
        floor_strike=m.get("floor_strike"),
        cap_strike=m.get("cap_strike"),
        result=m.get("result", "") or "",
        target_date=parse_event_date(event) if event else None,
        open_time=_parse_ts(m.get("open_time")),
    )


def to_snapshot(m: dict[str, Any], ts: datetime | None = None) -> Snapshot:
    # Kalshi's NO book is the mirror of the YES book and the API only
    # reports YES-side sizes: buying NO at the no_ask consumes the yes_bid,
    # so the NO ask size IS the yes_bid size (and vice versa).
    yes_bid_size = _fp(m, "yes_bid_size_fp")
    yes_ask_size = _fp(m, "yes_ask_size_fp")
    return Snapshot(
        venue=VENUE,
        market_id=m["ticker"],
        ts=ts or datetime.now(UTC),
        yes_bid=_dollars(m, "yes_bid_dollars"),
        yes_ask=_dollars(m, "yes_ask_dollars"),
        no_bid=_dollars(m, "no_bid_dollars"),
        no_ask=_dollars(m, "no_ask_dollars"),
        yes_bid_size=yes_bid_size,
        yes_ask_size=yes_ask_size,
        no_bid_size=_fp(m, "no_bid_size_fp") or yes_ask_size,
        no_ask_size=_fp(m, "no_ask_size_fp") or yes_bid_size,
        last_price=_dollars(m, "last_price_dollars"),
        volume=_fp(m, "volume_fp"),
        open_interest=_fp(m, "open_interest_fp"),
    )
