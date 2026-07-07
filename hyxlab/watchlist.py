"""Watchlist config loader (shared kernel).

Lives outside both the collection and sim sides so neither has to import
the other for configuration — the import-boundary test enforces that
split (collection must stay deployable without strategy code and vice
versa).
"""

from __future__ import annotations

import json
from pathlib import Path

DEFAULT_WATCHLIST = Path(__file__).parent / "watchlist.json"


def load_watchlist(path: str | Path = DEFAULT_WATCHLIST) -> dict:
    with open(path) as f:
        return json.load(f)
