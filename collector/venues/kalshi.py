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


def _get_with_429_retry(
    sess: requests.Session,
    url: str,
    params: dict[str, Any],
    timeout: int = 30,
    tries: int = 4,
) -> requests.Response:
    """GET honoring 429 Retry-After with capped exponential fallback.

    Measured live defect (sweep audit 2026-08-02): a 429 inside the
    get_markets page loop escaped to run_sweep's except, so the sweep of
    KXNASDAQ100U failed 5 consecutive days without advancing its watermark —
    4,947 closed markets unarchived while inside Kalshi's ~60-90d purge
    window. The candles path had per-request 429 handling; the markets page
    loop did not.
    """
    import time as _time

    delay = 5.0
    for attempt in range(tries):
        resp = sess.get(url, params=params, timeout=timeout)
        if resp.status_code != 429 or attempt == tries - 1:
            resp.raise_for_status()
            return resp
        retry_after = resp.headers.get("Retry-After")
        try:
            wait = float(retry_after) if retry_after else delay
        except ValueError:
            wait = delay
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
    **extra_params: Any,
) -> list[dict[str, Any]]:
    """Paginated /markets. `pause_s` paces BETWEEN pages.

    Pacing lives here rather than in the caller because a caller can only
    sleep around the whole loop: a wide enumeration (collector.breadth
    walks the entire open universe) would otherwise burst every page
    back-to-back at whatever the network allows, and this client shares
    Kalshi's rate budget with a live trading loop. Default 0.0 keeps the
    existing narrow per-series callers byte-identical.
    """
    import time as _time

    sess = session or requests.Session()
    out: list[dict[str, Any]] = []
    cursor = ""
    for page in range(max_pages):
        if page and pause_s:
            _time.sleep(pause_s)
        params: dict[str, Any] = {"limit": limit, "status": status, **extra_params}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if cursor:
            params["cursor"] = cursor
        resp = _get_with_429_retry(sess, f"{BASE}/markets", params)
        body = resp.json()
        out.extend(body.get("markets", []))
        cursor = body.get("cursor") or ""
        if not cursor or not body.get("markets"):
            break
    if cursor:  # pages exhausted with more upstream — the Gamma-offset class
        print(
            f"[kalshi] get_markets TRUNCATED at {len(out)} rows"
            f" (max_pages={max_pages} exhausted, cursor live)",
            flush=True,
        )
    return out


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


def get_candlesticks(
    series_ticker: str,
    market_ticker: str,
    start_ts: int,
    end_ts: int,
    period_interval: int = 60,
    session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    """Historical candles (price + yes_bid/yes_ask OHLC) for one market.

    period_interval is in minutes (1, 60, or 1440). Available for settled
    markets — this is what makes Tier-1 historical backtesting possible.

    Goes through `_get_with_429_retry` like every other endpoint here.
    `sweep_series` has its own inline single-retry around this call, but a
    single retry is not enough: measured 2026-08-03, the KXSHIBA capture
    aborted the whole series on a candlesticks 429 after that retry, which
    `run_sweep` recorded as status='error' with zero markets kept. Capped
    exponential backoff turns that into a pause instead of a lost series.
    """
    sess = session or requests.Session()
    resp = _get_with_429_retry(
        sess,
        f"{BASE}/series/{series_ticker}/markets/{market_ticker}/candlesticks",
        {"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_interval},
    )
    return resp.json().get("candlesticks", [])


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
