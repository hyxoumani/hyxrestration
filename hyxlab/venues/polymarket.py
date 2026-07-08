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
    **extra_params: Any,
) -> list[dict[str, Any]]:
    """Active (or closed) markets ordered volume-desc, down to min_volume.

    Gamma is ~60 req/min unauthenticated and occasionally answers a page
    with an error object — retried once, then skipped.
    """
    import time

    sess = session or requests.Session()
    out: list[dict[str, Any]] = []
    offset = 0
    for _ in range(max_pages):
        page = None
        for _attempt in range(2):
            resp = sess.get(
                f"{GAMMA}/markets",
                params={
                    "closed": str(closed).lower(),
                    "order": "volumeNum",
                    "ascending": "false",
                    "limit": 100,
                    "offset": offset,
                    **extra_params,
                },
                timeout=30,
            )
            body = resp.json()
            if isinstance(body, list):
                page = body
                break
            time.sleep(5)
        if not page:
            break
        rows = [m for m in page if isinstance(m, dict)]
        out.extend(m for m in rows if float(m.get("volumeNum") or 0) >= min_volume)
        if rows and float(rows[-1].get("volumeNum") or 0) < min_volume:
            break
        offset += 100
        time.sleep(page_pause_s)
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
    sess = session or requests.Session()
    out: list[dict[str, Any]] = []
    for offset in range(0, max_offset, 500):
        resp = sess.get(
            f"{DATA_API}/trades",
            params={"market": condition_id, "limit": 500, "offset": offset},
            timeout=30,
        )
        body = resp.json()
        if not isinstance(body, list) or not body:
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
