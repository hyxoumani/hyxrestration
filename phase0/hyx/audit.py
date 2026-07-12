"""Append-only structured audit log backed by DuckDB.

Per architecture.md §2.4 / T09: print() to stderr for humans, structured rows into
audit_log for machines. Payloads are JSON-serialized dicts; keep them small and
queryable — not a dumping ground for large blobs.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Literal

import duckdb

Level = Literal["debug", "info", "warn", "error"]


def audit(
    conn: duckdb.DuckDBPyConnection,
    *,
    slice: str,
    level: Level,
    event: str,
    payload: dict[str, Any] | None = None,
    echo: bool = True,
) -> None:
    """Write one audit row. If echo=True, also print to stderr."""
    payload_json = json.dumps(payload, default=str) if payload else None
    conn.execute(
        "INSERT INTO audit_log (slice, level, event, payload) VALUES (?, ?, ?, ?)",
        [slice, level, event, payload_json],
    )
    if echo:
        tag = f"[{slice}:{level}]"
        extra = f" {payload_json}" if payload_json else ""
        print(f"{tag} {event}{extra}", file=sys.stderr)
