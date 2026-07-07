"""Alpaca (Benzinga) historical financial news → NewsItem records.

Verified 2026-07-06 with the project's existing credentials: coverage
back to 2016-01-01, fields incl. created_at/updated_at/headline/summary/
content/symbols/source/url. knowable_at = created_at (wire timestamp).

Auth: ALPACA_KEY / ALPACA_SECRET from .env (same creds Phase 0 used).
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from typing import Any

import requests

from hyxlab.models import NewsItem

BASE = "https://data.alpaca.markets/v1beta1/news"


def _parse_ts(v: str | None) -> datetime | None:
    if not v:
        return None
    return datetime.fromisoformat(v.replace("Z", "+00:00"))


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def parse_news_payload(payload: dict[str, Any]) -> list[NewsItem]:
    out: list[NewsItem] = []
    for n in payload.get("news", []):
        created = _parse_ts(n.get("created_at"))
        if created is None:
            continue  # no honest knowable_at -> not ingested (P1)
        out.append(
            NewsItem(
                source="alpaca",
                url_hash=url_hash(n.get("url") or str(n.get("id"))),
                published_at=created,
                knowable_at=created,
                title=n.get("headline", ""),
                tone=None,
                symbols=",".join(n.get("symbols", [])),
            )
        )
    return out


def get_news(
    start: datetime,
    end: datetime,
    symbols: list[str] | None = None,
    limit: int = 50,
    session: requests.Session | None = None,
) -> tuple[list[NewsItem], str | None]:
    """One page of news; returns (items, next_page_token)."""
    sess = session or requests.Session()
    headers = {
        "APCA-API-KEY-ID": os.environ["ALPACA_KEY"],
        "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET"],
    }
    params: dict[str, Any] = {
        "start": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit": limit,
        "sort": "asc",
    }
    if symbols:
        params["symbols"] = ",".join(symbols)
    resp = sess.get(BASE, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    return parse_news_payload(payload), payload.get("next_page_token")
