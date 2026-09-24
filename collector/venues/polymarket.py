"""Polymarket public-data client: Gamma (metadata) + CLOB (order books).

No auth needed for reads. A Polymarket binary market has two CLOB tokens
(YES and NO); Gamma's `clobTokenIds` is a JSON-encoded ["yes","no"] pair.
Rate limits: Gamma ~60 req/min unauthenticated; CLOB books are batched
via POST /books so one call covers the whole watchlist.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
VENUE = "polymarket"

from hyxlab.models import MarketInfo, Snapshot  # noqa: E402

# --- Retry / fault ledger -----------------------------------------------
#
# The #80 question, asked of this client: which of its loops share a retry
# allowance, and which of its fault classes sit outside every ladder?
#
# Three loops, three different concurrency units, and only ONE of them has a
# ladder at all:
#   * `iter_markets_by_volume`'s keyset page fetch -- a 4-attempt ladder whose
#     unit is one PAGE (the allowance is 3; the last attempt does not retry,
#     it falls through to the restart path).
#   * the same function's chain restart -- unit is one WALK, budget
#     `max_restarts`. Not a retry of a request and never summed with one.
#   * `trades_tail`'s offset pagination -- NO ladder. A non-list body ends the
#     tail early and returns what it has, which the sweep archives as if it
#     were the whole tail.
#
# That last class is this venue's analogue of kalshi's unretried 429s, and it
# is far bigger than anything the sweep's own `errors` total was reporting:
# measured over 2026-09-01..23 of `hyxlab-poly-sweep` journal, 1,356 early
# stops (1,099 HTTP 429, 257 HTTP 408), ~80 per run, every run, while `done:`
# reported `errors` between 1 and 8. 687 of the 1,356 returned ZERO prints.
#
# Counting them is not a claim that they are losses. They are not: no market
# is starved -- of 1,297 distinct markets 1,239 stopped early on exactly one
# day, 57 on two and 1 on three, and the next sweep re-fetches the same tail
# from offset 0 against a 3,000-print cap that binds on 0.052% of market-days
# (74 of 140,975, archive-measured 2026-09-24). The daily re-sweep is the
# absorber, exactly as `hyxlab-tradepass` is for kalshi. The counter exists
# because establishing that took a journal scrape and two archive queries,
# and nothing on the box published the population at all.
#
# So: no ladder added and NO BUDGET RE-SIZED (#70). Retries that were never
# spent must not be reported as recoveries, so the unretried classes are
# published OUTSIDE `total`.

#: Backoff schedule for one keyset page. The trailing `None` is the attempt
#: that does NOT retry, so the ALLOWANCE is one fewer than the length.
KEYSET_BACKOFF_S: tuple[int | None, ...] = (5, 15, 45, None)

_KEYSET_RETRIES = 0
_KEYSET_WORST_FRAC = 0.0
_KEYSET_RESTARTS = 0
_TAIL_TRUNCATED = 0
_TAIL_TRUNCATED_EMPTY = 0


def keyset_retries() -> int:
    """Keyset page retries spent since the last `reset_retry_counts()`."""
    return _KEYSET_RETRIES


def keyset_budget_frac() -> float:
    """Share of ONE page's retry allowance spent by the WORST page since the
    last reset, in [0, 1]; 1.0 means some page used its last retry and the
    next failure there would have dropped the chain to a restart.

    The worst page, not the mean: a mean over pages falls as the walk
    deepens, reporting the walks with the most chances to exhaust as the
    safest. Recorded inside the page loop, because a per-page allowance is
    discarded the moment the page returns and a caller-side reduction would
    see the survivors only (the #79/#80 lesson).
    """
    return _KEYSET_WORST_FRAC


def tail_truncated() -> int:
    """`trades_tail` calls that ended early on an error body since the last
    reset -- a partial tail archived as if complete. No ladder covers this;
    see the block above."""
    return _TAIL_TRUNCATED


def retry_counts() -> dict[str, int]:
    """Every retry class spent since the last `reset_retry_counts()`.

    `total` is the sum over the classes that actually SPENT a retry
    allowance, and is what a caller should publish under a name like
    `http_retries`; the per-class members are what makes a rising total
    actionable.
    """
    return {
        "keyset_page": _KEYSET_RETRIES,
        # Outside `total`: a walk restart's unit is the WALK, not a request.
        # Summing it with page retries would be arithmetic over two
        # denominators that share nothing.
        "keyset_restart": _KEYSET_RESTARTS,
        # Outside `total`: nobody retried these, so they spent no allowance.
        # Reporting ~80 faults per run as ~80 recoveries is the error this
        # counter exists to avoid.
        "tail_truncated": _TAIL_TRUNCATED,
        # The strictly worse subset: the tail came back with no prints at
        # all, so the market contributed nothing to the run.
        "tail_truncated_empty": _TAIL_TRUNCATED_EMPTY,
        "total": _KEYSET_RETRIES,
    }


def reset_retry_counts() -> None:
    global _KEYSET_RETRIES, _KEYSET_WORST_FRAC, _KEYSET_RESTARTS
    global _TAIL_TRUNCATED, _TAIL_TRUNCATED_EMPTY
    _KEYSET_RETRIES = 0
    _KEYSET_WORST_FRAC = 0.0
    _KEYSET_RESTARTS = 0
    _TAIL_TRUNCATED = 0
    _TAIL_TRUNCATED_EMPTY = 0


def get_gamma_markets(
    session: requests.Session | None = None, **params: Any
) -> list[dict[str, Any]]:
    sess = session or requests.Session()
    resp = sess.get(f"{GAMMA}/markets", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def token_pair(gamma_market: dict[str, Any]) -> tuple[str, str] | None:
    """(yes_token, no_token) from a Gamma market row, or None."""
    raw = gamma_market.get("clobTokenIds")
    if not raw:
        return None
    ids = json.loads(raw) if isinstance(raw, str) else raw
    if len(ids) != 2:
        return None
    return ids[0], ids[1]


def get_books(
    token_ids: list[str], session: requests.Session | None = None
) -> dict[str, dict[str, Any]]:
    """Books keyed by token id. One POST covers all requested tokens."""
    if not token_ids:
        return {}
    sess = session or requests.Session()
    resp = sess.post(
        f"{CLOB}/books",
        json=[{"token_id": t} for t in token_ids],
        timeout=30,
    )
    resp.raise_for_status()
    return {b["asset_id"]: b for b in resp.json()}


def iter_markets_by_volume(
    min_volume: float,
    closed: bool = False,
    session: requests.Session | None = None,
    page_pause_s: float = 1.1,
    max_pages: int = 400,
    max_restarts: int = 20,
    want_top_n: bool = False,
    **extra_params: Any,
) -> list[dict[str, Any]]:
    """Active (or closed) markets ordered volume-desc, down to min_volume.

    Keyset pagination (probed 2026-07-08): /markets rejects offset > 2000
    — with ~4,200 markets over the $10k sweep threshold, offset paging
    would silently truncate the daily sweep to its top half. So:
    /markets/keyset, pages chained via `after_cursor` from the response's
    `next_cursor`, with the threshold also applied server-side
    (`volume_num_min`).

    Tail fault (probed 2026-08-22, supersedes the poisoned-page, daily
    fault-window and chain-depth reads that preceded it): on a long
    walk Gamma answers the LAST page — the short one that should carry
    `next_cursor: null` — with a persistent 500 instead. Six nightly
    INCOMPLETE walks all died a hair above their own volume floor
    (cursor volumes 10,050 / 10,053 / 10,116 / 10,163 …), which read as
    a clock window only because the floor is the same every night. Four
    daylight probes at 08:1x–08:3xZ, well outside the alleged window,
    reproduced it on demand and pinned the trigger to the floor rather
    than the clock or the page index: bands [10k,50k], [11k,50k] and
    [20k,50k] failed at pages 70 / 65 / 38 respectively, each one row-
    group short of its own bottom, while the shallow [40k,50k] band and
    the whole closed=false walk terminated clean.

    So a failed page is not a reason to abandon the walk. `volume_num_max`
    IS honoured, so the recovery is to drop the cursor and open a FRESH
    chain ceilinged at the last row already collected: the remainder is
    a smaller set whose own tail Gamma will serve. The 08:22Z probe that
    died at $10,161.41 returned the missing 79-row tail this way and
    terminated cleanly. Restarts must strictly lower the ceiling, so the
    walk cannot spin; duplicates at the boundary are dropped by id.

    `max_pages` is a TRUNCATION GUARD, not a budget (same contract as
    `breadth.MAX_PAGES`): exhausting it with a live cursor means the
    caller silently got less than it asked for, which is the
    Gamma-offset regression class, so it prints a loud TRUNCATED line.
    A caller that WANTS the first N by volume — streamd's top-volume
    book refresh passes `max_pages=1` on purpose — sets `want_top_n`
    to declare that, and the guard stays loud for everyone who did not.
    Silencing the line globally instead would have disarmed the alarm
    for every real caller to quiet one intentional truncation a day.
    """
    import time

    global _KEYSET_RETRIES, _KEYSET_WORST_FRAC, _KEYSET_RESTARTS

    sess = session or requests.Session()
    out: list[dict[str, Any]] = []
    seen: set[Any] = set()
    cursor: str | None = None
    ceiling: float | None = None
    restarts = 0
    resp = None
    truncated = True

    for page_idx in range(max_pages):
        params: dict[str, Any] = {
            "closed": str(closed).lower(),
            "order": "volumeNum",
            "ascending": "false",
            "limit": 100,
            "volume_num_min": str(min_volume),
            **extra_params,
        }
        if ceiling is not None:
            params["volume_num_max"] = str(ceiling)
        if cursor:
            params["after_cursor"] = cursor
        body = None
        allowance = len(KEYSET_BACKOFF_S) - 1  # the trailing None does not retry
        for attempt, backoff_s in enumerate(KEYSET_BACKOFF_S):
            resp = sess.get(f"{GAMMA}/markets/keyset", params=params, timeout=30)
            try:
                candidate = resp.json()
            except ValueError:  # non-JSON 5xx body: same retry path
                candidate = None
            if isinstance(candidate, dict) and "markets" in candidate:
                body = candidate
                break
            print(
                f"[poly] keyset page {page_idx} attempt {attempt + 1} failed"
                f" (status {getattr(resp, 'status_code', '?')})"
                + (f"; backing off {backoff_s}s" if backoff_s is not None else ""),
                flush=True,
            )
            if backoff_s is not None:
                _KEYSET_RETRIES += 1
                # Recorded here, not after the loop: this page's allowance is
                # gone the moment the page returns.
                _KEYSET_WORST_FRAC = max(_KEYSET_WORST_FRAC, (attempt + 1) / allowance)
                time.sleep(backoff_s)
        if body is None:
            last_vol = float(out[-1].get("volumeNum") or 0) if out else None
            can_restart = (
                last_vol is not None
                and restarts < max_restarts
                and (ceiling is None or last_vol < ceiling)
            )
            if can_restart:
                # The tail fault, not a dead walk: re-open below the last
                # row instead of losing everything under it.
                restarts += 1
                _KEYSET_RESTARTS += 1
                print(
                    f"[poly] keyset chain restart {restarts} below volume"
                    f" {last_vol:g} at {len(out)} markets (page {page_idx},"
                    f" status {getattr(resp, 'status_code', '?')})",
                    flush=True,
                )
                ceiling, cursor = last_vol, None
                time.sleep(page_pause_s)
                continue
            # Same failure class the QA shrink-tripwire catches a day
            # late — the same-run signal is this log line.
            print(
                f"[poly] keyset walk INCOMPLETE at {len(out)} markets"
                f" (Gamma errors persisted after retry; status"
                f" {getattr(resp, 'status_code', '?')};"
                f" page {page_idx}, cursor {cursor!r},"
                f" restarts {restarts})",
                flush=True,
            )
            truncated = False
            break
        rows = [m for m in body["markets"] or [] if isinstance(m, dict)]
        # Server-side volume_num_min is belt-and-braces; keep the local
        # filter so a silently ignored param can't widen the sweep.
        for m in rows:
            if float(m.get("volumeNum") or 0) < min_volume:
                continue
            key = m.get("id")
            if key is not None:
                if key in seen:  # boundary row re-served by a restart
                    continue
                seen.add(key)
            out.append(m)
        cursor = body.get("next_cursor") or None
        if not rows or cursor is None:
            truncated = False
            break
        if float(rows[-1].get("volumeNum") or 0) < min_volume:
            truncated = False
            break
        time.sleep(page_pause_s)

    if truncated and cursor is not None and not want_top_n:
        print(
            f"[poly] keyset walk TRUNCATED at {len(out)} markets"
            f" (max_pages={max_pages} exhausted, cursor live)",
            flush=True,
        )
    return out


def prices_history(
    token_id: str,
    start_ts: int | None = None,
    end_ts: int | None = None,
    fidelity_min: int = 60,
    session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    """[{'t': epoch_s, 'p': price}] — probed 2026-07-07: ~60-day rolling
    retention (closed markets purge ~1-2 months after close; explicit
    ranges older than ~60d return empty)."""
    sess = session or requests.Session()
    params: dict[str, Any] = {"market": token_id, "fidelity": fidelity_min}
    if start_ts is not None:
        params["startTs"] = start_ts
        params["endTs"] = end_ts or int(datetime.now(UTC).timestamp())
    else:
        params["interval"] = "max"  # rolling ~30d window
    resp = sess.get(f"{CLOB}/prices-history", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("history", [])


def prices_history_range(
    token_id: str,
    start_ts: int,
    end_ts: int | None = None,
    fidelity_min: int = 60,
    chunk_days: int = 14,
    session: requests.Session | None = None,
    pause_s: float = 0.25,
) -> list[dict[str, Any]]:
    """Stitch explicit-range chunks. The API 400s on ranges over ~30d and
    can return empty near the limit; 14-day windows are probe-verified."""
    import time

    end_ts = end_ts or int(datetime.now(UTC).timestamp())
    out: list[dict[str, Any]] = []
    chunk = chunk_days * 86400
    t = start_ts
    while t < end_ts:
        out.extend(
            prices_history(token_id, start_ts=t, end_ts=min(t + chunk, end_ts), session=session)
        )
        t += chunk
        if t < end_ts:
            time.sleep(pause_s)
    return out


def trades_tail(
    condition_id: str,
    session: requests.Session | None = None,
    max_offset: int = 3000,
) -> list[dict[str, Any]]:
    """Most recent prints for one market. HARD CAP (probed): the data-api
    serves at most the LAST 3,000 trades per market, ever — a tail
    sample, not a tape. The full forward tape is the WS stream."""
    global _TAIL_TRUNCATED, _TAIL_TRUNCATED_EMPTY

    sess = session or requests.Session()
    out: list[dict[str, Any]] = []
    for offset in range(0, max_offset, 500):
        resp = sess.get(
            f"{DATA_API}/trades",
            params={"market": condition_id, "limit": 500, "offset": offset},
            timeout=30,
        )
        body = resp.json()
        if not isinstance(body, list):  # error object with HTTP 200
            _TAIL_TRUNCATED += 1
            if not out:
                _TAIL_TRUNCATED_EMPTY += 1
            print(
                f"[poly] trades_tail {condition_id[:16]} stopped early at"
                f" {len(out)} prints (non-list body, status {getattr(resp, 'status_code', '?')})",
                flush=True,
            )
            break
        if not body:
            break
        out.extend(body)
        if len(body) < 500:
            break
    return out


def gamma_market_info(m: dict[str, Any]) -> MarketInfo:
    """Gamma market row → MarketInfo (market_id = conditionId)."""
    outcome_prices = m.get("outcomePrices")
    result = ""
    if m.get("closed") and outcome_prices:
        prices = json.loads(outcome_prices) if isinstance(outcome_prices, str) else outcome_prices
        if len(prices) == 2:
            result = "yes" if float(prices[0]) > 0.5 else "no"
    end = m.get("endDate")
    return MarketInfo(
        venue=VENUE,
        market_id=m.get("conditionId", ""),
        title=(m.get("question") or "")[:300],
        series=m.get("slug", "")[:80],
        close_time=datetime.fromisoformat(end.replace("Z", "+00:00")) if end else None,
        result=result,
    )


def poly_trade_row(t: dict[str, Any]) -> tuple:
    """data-api trade → store trades row, normalized to YES terms.

    price is for the traded token; NO fills are mirrored (yes = 1 − p).
    taker_side records the aggressor's direction in YES terms:
    BUY Yes / SELL No → 'yes'; BUY No / SELL Yes → 'no'."""
    outcome = (t.get("outcome") or "").lower()
    price = float(t["price"])
    yes_price = price if outcome != "no" else round(1.0 - price, 6)
    buys_yes = (t.get("side") == "BUY") == (outcome != "no")
    trade_id = f"{t.get('transactionHash', '')}:{t.get('asset', '')[:12]}:{price}:{t.get('size')}"
    return (
        VENUE,
        t.get("conditionId", ""),
        trade_id,
        datetime.fromtimestamp(int(t["timestamp"]), tz=UTC),
        yes_price,
        float(t.get("size") or 0.0),
        "yes" if buys_yes else "no",
        False,
    )


def price_rows(
    token_id: str, market_id: str, outcome: str, history: list[dict[str, Any]]
) -> list[tuple]:
    return [
        (token_id, market_id, outcome, datetime.fromtimestamp(int(h["t"]), tz=UTC), float(h["p"]))
        for h in history
    ]


def _best(levels: list[dict[str, Any]], *, highest: bool) -> tuple[float | None, float]:
    """(price, size) of best level; CLOB levels are unsorted {price, size} strings."""
    if not levels:
        return None, 0.0
    key = max if highest else min
    best = key(levels, key=lambda x: float(x["price"]))
    return float(best["price"]), float(best["size"])


def pair_snapshot(
    market_id: str,
    yes_book: dict[str, Any] | None,
    no_book: dict[str, Any] | None,
    ts: datetime | None = None,
) -> Snapshot:
    yes_bid, yes_bid_size = _best((yes_book or {}).get("bids", []), highest=True)
    yes_ask, yes_ask_size = _best((yes_book or {}).get("asks", []), highest=False)
    no_bid, no_bid_size = _best((no_book or {}).get("bids", []), highest=True)
    no_ask, no_ask_size = _best((no_book or {}).get("asks", []), highest=False)
    return Snapshot(
        venue=VENUE,
        market_id=market_id,
        ts=ts or datetime.now(UTC),
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        no_ask=no_ask,
        yes_bid_size=yes_bid_size,
        yes_ask_size=yes_ask_size,
        no_bid_size=no_bid_size,
        no_ask_size=no_ask_size,
    )
