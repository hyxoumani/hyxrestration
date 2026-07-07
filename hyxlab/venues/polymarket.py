"""Polymarket public-data client: Gamma (metadata) + CLOB (order books).

No auth needed for reads. A Polymarket binary market has two CLOB tokens
(YES and NO); Gamma's `clobTokenIds` is a JSON-encoded ["yes","no"] pair.
Rate limits: Gamma ~60 req/min unauthenticated; CLOB books are batched
via POST /books so one call covers the whole watchlist.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
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
        ts=ts or datetime.now(timezone.utc),
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        no_ask=no_ask,
        yes_bid_size=yes_bid_size,
        yes_ask_size=yes_ask_size,
        no_bid_size=no_bid_size,
        no_ask_size=no_ask_size,
    )
