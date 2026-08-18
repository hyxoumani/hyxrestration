"""Shared fixtures.

EXP-1333 (hylshi): the kalshi venue client appends the headers of any 429 it
observes to `data/rate_limit_headers.jsonl` (relative, cwd-rooted). Several
tests simulate 429s through the real code paths; without redirection they
would write fake header rows into the DEV repo's real sink — poisoning the
very telemetry the capture exists to collect. Redirect it per-test.
"""

import pytest


@pytest.fixture(autouse=True)
def _redirect_429_header_sink(tmp_path, monkeypatch):
    from collector.venues import kalshi

    monkeypatch.setattr(
        kalshi, "RATE_LIMIT_HEADERS_LOG", str(tmp_path / "rate_limit_headers.jsonl")
    )
