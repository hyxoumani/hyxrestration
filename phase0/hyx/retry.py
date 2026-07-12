"""Shared 3× exponential-backoff retry helper.

Matches architecture.md §3.4: 1s / 2s / 4s on transient failures, immediate
raise on auth / bad-request errors (they don't fix themselves).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")

RETRY_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0)

# String fragments in exception messages that mean "don't retry — the call is wrong,
# not unlucky." Case-insensitive match.
_NO_RETRY_MARKERS: tuple[str, ...] = (
    "unauthorized",
    "forbidden",
    "invalid api key",
    "400 bad request",
    "404",
)


def with_retry(fn: Callable[[], T], *, what: str) -> T:
    """Run fn with 3× exponential backoff. Raises the final exception on exhaustion."""
    last_exc: Exception | None = None
    for attempt, delay in enumerate((0.0,) + RETRY_DELAYS):
        if delay > 0:
            time.sleep(delay)
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            msg = str(exc).lower()
            if any(marker in msg for marker in _NO_RETRY_MARKERS):
                raise
            if attempt == len(RETRY_DELAYS):
                break
    assert last_exc is not None
    raise RuntimeError(f"{what} failed after {len(RETRY_DELAYS) + 1} attempts") from last_exc
