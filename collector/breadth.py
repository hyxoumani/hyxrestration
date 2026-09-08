"""Exchange-wide top-N top-of-book breadth collector (EXP-928).

The 5-min `collect` cycle only snapshots the 23-series watchlist, so the
archive holds a bid/ask history for ~630 markets out of the ~3,033 series
the exchange traded in the same week. Settlement and hourly candles cover
everything in the sweep's allowlist, but those are the OUTCOME dimension:
without quotes, **no new series family can ever be evaluated
retrospectively**, because there is no price a strategy could have traded
at. This module captures that missing price dimension exchange-wide.

Deliberately NOT gated by the sweep's category allowlist: the whole point
is covering families we have never studied.

Mechanism (one HTTP pass per cycle, no per-market calls):
`/markets?status=open` already carries top-of-book (yes/no bid/ask +
displayed sizes) AND `volume_24h_fp` for every market it lists, so a
single paginated enumeration both RANKS the universe and IS the snapshot.
Ranking is exact rather than sampled ONLY WHILE THE WALK IS EXHAUSTIVE,
and top-N then costs the same requests as top-1.

THAT PREMISE IS CONDITIONAL, and the exchange falsified it on
2026-09-06T23:32Z. The 24h-close universe crossed `MAX_PAGES * PAGE_LIMIT`
(60,000) — a mass listing of KXMVECROSSCATEGORY parlay legs. Every cycle
for the next 27 hours truncated, and a truncated walk returns the API's own
enumeration order, which is NOT volume order: the volume-bearing markets
sat beyond the cap, `top_n` correctly rejected the dead legs in front of
them, and coverage fell from ~990 to as low as 1 row/cycle. The collector
reported success throughout. So truncation is now returned in-band
(`fetch_universe`), recorded per cycle (`breadth_cycles`) and failed by QA.

WHY THE FILTER IS `min_close_ts`, AND WHY THE TWO OBVIOUS FIXES ARE BOTH
DEAD (one exhaustive walk, 2026-09-08T02:16Z, 400 pages / 284 s):
- **Widening `MAX_PAGES` is dead.** The 24h universe is no longer 60k, it
  is **>400,000 and the cursor was still live** when the probe stopped.
  There is no cap that is both affordable every 5 minutes and above a
  number the exchange can raise at will.
- **Excluding the parlay family client-side is dead**, and this is the
  trap: filtering by ticker cannot fix TRUNCATION, because the legs must
  still be PAGED THROUGH to be discarded. It removes rows from the result,
  not requests from the walk.
- **Narrowing `CLOSE_WINDOW_H` is dead.** A 1-hour window still holds
  282,295 markets — the legs close on the same clock the real markets do.
- What actually collapses the universe is that **260,662 of the 391,414
  parlay legs have a close_time in the PAST and are still `status=open`**:
  expired legs the exchange has not cleared. `min_close_ts = now` drops
  65% of the walk and **loses exactly zero volume-bearing markets, because
  zero markets with any 24h volume close in the past** (measured over all
  400,000: every one of the 1,598 with volume closes in the future, and
  all 8,586 non-parlay markets do too). A market past its close cannot be
  traded, so its book was never "a price a strategy could have traded at"
  — this filter is a DEFECT FIX, not a scope cut. It takes the walk to
  139,338 markets in 140 pages, which TERMINATES.
Residual, stated: the floor assumes the exchange keeps clearing real
markets promptly, so that lingering past-close markets stay parlay legs.
If that changed, the floor would drop a real market — still correctly, as
it is no longer tradeable, but its late quotes would stop being recorded.
The useful universe is UNCHANGED by the flood: 8,586 non-parlay markets on
2026-09-08 against 8,718 measured on 2026-08-03.

MEASURED 2026-08-03, and the reason CLOSE_WINDOW_H exists: the
UNFILTERED open universe is **>200,000 markets** (the walk truncated at
200 pages with the cursor still live, 140 s, 2x HTTP 429) and is almost
entirely KXMVE* parlay legs with zero volume — fewer than 1,000 of the
first 200k had ANY 24h volume, so an unwindowed "top 1,000" was ranking
noise (measured cutoff volume_24h = 0.0). Adding `max_close_ts = now +
24 h` collapses it to **8,718 markets in 9 requests / 5.8 s**, of which
1,636 have nonzero volume and the rank-1,000 cutoff is 99 contracts. A
48 h window costs 36 requests for 35,676 markets and only 2,162 nonzero —
4x the requests for +32% of the useful set. Quotes only matter for
markets approaching resolution (the fade edge lives 4-6 h out), and a
market re-enters the window as it approaches, so 24 h loses nothing.

Discipline (see docs/wiki/data-pipeline.md and mistakes log):
- writer_burst — HTTP happens OUTSIDE `data/writer.lock`; the DB is
  opened, written and closed in one short burst. A long-held lock
  previously dropped 11.4% of 5-min collect cycles, which is
  unrecoverable data loss.
- pacing — `sweep.MARKETS_PAUSE_S` between pages, the repo's empirical
  safe constant; 429s go through `kalshi._get_with_429_retry`, which
  honours Retry-After.
- DEFAULT DISABLED. This collector shares Kalshi's rate budget with a
  live real-money trading loop, so it must be turned on deliberately:
  set HYXLAB_BREADTH_ENABLED=1 (the systemd unit does) or pass --enable.

Run:
    HYXLAB_BREADTH_ENABLED=1 python -u -m collector.breadth --once
    python -u -m collector.breadth --once --enable --top-n 500
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import UTC, datetime, timedelta

import requests

from collector.sweep import MARKETS_PAUSE_S, writer_burst
from collector.venues import kalshi
from hyxlab.models import Snapshot

__all__ = [
    "CLOSE_WINDOW_H",
    "DEFAULT_INTERVAL_S",
    "DEFAULT_TOP_N",
    "MAX_PAGES",
    "PAGE_LIMIT",
    "breadth_row",
    "collect_breadth_once",
    "enabled",
    "fetch_universe",
    "main",
    "top_n",
]

# Kalshi's /markets page cap. 1,000/page keeps a windowed enumeration to
# ~9 requests; see the measured arithmetic in the module docstring.
PAGE_LIMIT = 1000
# max_pages is a TRUNCATION GUARD, not a budget: a normal cycle must never
# reach it, and reaching it is reported in-band (see fetch_universe). The
# guard is sized against the MEASURED post-`min_close_ts` walk — 139,338
# markets / 140 pages on 2026-09-08 — at ~1.8x, so a parlay flood that
# doubles again is caught rather than silently truncated, while the steady
# cost stays 140 requests over ~100 s (1.4 req/s against a ~30 req/s public
# limit). It is NOT sized against the unfiltered universe: that is >400,000
# markets and unbounded, which is exactly why the floor exists.
MAX_PAGES = 250
# Hours ahead of now to include by close_time. See the docstring: this
# is what makes the collector affordable at all.
CLOSE_WINDOW_H = 24

DEFAULT_TOP_N = 1000
DEFAULT_INTERVAL_S = 300

ENABLE_ENV = "HYXLAB_BREADTH_ENABLED"
TOP_N_ENV = "HYXLAB_BREADTH_TOP_N"
INTERVAL_ENV = "HYXLAB_BREADTH_INTERVAL_S"
WINDOW_ENV = "HYXLAB_BREADTH_CLOSE_WINDOW_H"


def enabled(env: dict[str, str] | None = None) -> bool:
    """Off unless explicitly switched on. See module docstring for why."""
    env = os.environ if env is None else env
    return str(env.get(ENABLE_ENV, "")).strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int, env: dict[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    raw = str(env.get(name, "")).strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        print(f"[breadth] ignoring non-integer {name}={raw!r}", flush=True)
        return default
    return v if v > 0 else default


def fetch_universe(
    session: requests.Session | None = None,
    page_limit: int = PAGE_LIMIT,
    max_pages: int = MAX_PAGES,
    pause_s: float = MARKETS_PAUSE_S,
    close_window_h: int = CLOSE_WINDOW_H,
    now: datetime | None = None,
) -> tuple[list[dict], bool]:
    """Every OPEN market closing within `close_window_h`, with its book,
    as `(markets, truncated)`.

    One paginated pass, paced between pages by `kalshi.get_markets`
    itself (429s handled there via `_get_with_429_retry`). No
    series_ticker filter and NO CATEGORY ALLOWLIST — that is the point;
    the only filter is the close-time horizon, which is about cost and
    usefulness rather than about which families we are willing to study.

    The horizon is BOUNDED AT BOTH ENDS. `min_close_ts = now` is not a
    cost knob like the ceiling — it is a correctness one. `status=open`
    includes markets whose close_time has already passed and which the
    exchange has not cleared yet, and on 2026-09-08 that was 260,662 of
    the 400,000+ markets in the 24h window, every one of them a
    KXMVECROSSCATEGORY parlay leg with no volume. Such a market cannot be
    traded again, so its top-of-book is not a price any strategy could
    have traded at, which is the only reason this tape is captured. See
    the module docstring for the measurement, including the discrimination
    that matters: zero markets with any 24h volume close in the past.

    `truncated` is returned rather than discarded because it invalidates
    this module's central claim. Ranking is exact ONLY over an exhaustive
    enumeration; a truncated walk returns the first `max_pages` pages in
    the API's own order, which is not volume order, so `top_n` over it is
    a head slice wearing a ranking's name.
    """
    sess = session or requests.Session()
    now = now or datetime.now(UTC)
    max_close_ts = int((now + timedelta(hours=close_window_h)).timestamp())
    return kalshi.get_markets(
        status="open",
        limit=page_limit,
        max_pages=max_pages,
        session=sess,
        pause_s=pause_s,
        min_close_ts=int(now.timestamp()),
        max_close_ts=max_close_ts,
        with_truncated=True,
    )


def _vol24(m: dict) -> float:
    v = m.get("volume_24h_fp", m.get("volume_24h"))
    if v in (None, ""):
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def top_n(
    markets: list[dict], n: int, min_volume_24h: float = 0.0
) -> list[tuple[dict, float, int]]:
    """The n highest-`volume_24h` markets as (market, volume_24h, rank).

    Ranks are 1-based and DENSE over the returned slice. Ties break on
    ticker so the selection is deterministic: two cycles seeing the same
    universe must pick the same markets, otherwise the tape's membership
    churns for no reason and coverage analysis reads as flapping.

    The `min_volume_24h` floor is STRICT and defaults to 0.0, i.e. a
    market with no 24h volume is never captured. Measured: even inside
    the 24h close window only 1,636 of 8,718 open markets have traded at
    all, so without the floor a quiet hour would pad the tape with
    hundreds of dead parlay legs whose relative rank is arbitrary — rows
    that record nothing and make coverage look better than it is.
    """
    if n <= 0:
        return []
    scored = sorted(
        (m for m in markets if _vol24(m) > min_volume_24h),
        key=lambda m: (-_vol24(m), str(m.get("ticker", ""))),
    )
    return [(m, _vol24(m), i + 1) for i, m in enumerate(scored[:n])]


def breadth_row(snap: Snapshot, volume_24h: float, rank: int) -> tuple:
    """Flatten into the store's breadth_snapshots column order."""
    return (
        snap.venue,
        snap.market_id,
        snap.ts,
        snap.yes_bid,
        snap.yes_ask,
        snap.no_bid,
        snap.no_ask,
        snap.yes_bid_size,
        snap.yes_ask_size,
        snap.no_bid_size,
        snap.no_ask_size,
        snap.last_price,
        snap.volume,
        snap.open_interest,
        volume_24h,
        rank,
    )


def collect_breadth_once(
    db: str,
    n: int = DEFAULT_TOP_N,
    session: requests.Session | None = None,
    lock_file: str | None = None,
    pause_s: float = MARKETS_PAUSE_S,
    close_window_h: int = CLOSE_WINDOW_H,
) -> dict:
    """One cycle: enumerate (no lock held), then write in one burst."""
    t0 = time.monotonic()
    # Zeroed here and read below so `http_retries` describes THIS cycle. A
    # retried timeout is invisible everywhere else: it no longer fails the
    # unit, so the health digest's failure history cannot see it, and a
    # silently-degrading network would look exactly like a healthy one.
    kalshi.reset_transport_retries()
    markets, truncated = fetch_universe(
        session=session, pause_s=pause_s, close_window_h=close_window_h
    )
    http_retries = kalshi.transport_retries()
    fetch_s = time.monotonic() - t0

    ts = datetime.now(UTC)
    picked = top_n(markets, n)
    rows = [breadth_row(kalshi.to_snapshot(m, ts), v, r) for m, v, r in picked]
    infos = [kalshi.to_market_info(m) for m, _, _ in picked]
    cutoff = picked[-1][1] if picked else 0.0

    # A truncated cycle still WRITES. The rows it did find are real quotes,
    # and quotes are unrecoverable after the fact — refusing to write would
    # convert a degraded tape into an absent one, which is strictly worse
    # (the 2026-09-06 event still captured 3-700 genuine markets/cycle).
    # What truncation destroys is the RANKING CLAIM, not the rows, so it is
    # recorded next to them and escalated by QA rather than by dropping data.
    # --- lock held from here; no HTTP below this line -------------------
    with writer_burst(db, lock_file=lock_file) as store:
        store.upsert_markets(infos)
        inserted = store.insert_breadth_snapshots(rows)
        store.insert_breadth_cycle(ts, len(markets), len(picked), inserted, truncated, cutoff)
    # --------------------------------------------------------------------

    return {
        "universe": len(markets),
        "picked": len(picked),
        "inserted": inserted,
        "truncated": truncated,
        "cutoff_volume_24h": cutoff,
        "http_retries": http_retries,
        "fetch_s": round(fetch_s, 1),
        "total_s": round(time.monotonic() - t0, 1),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="hyxlab exchange-wide breadth collector")
    ap.add_argument("--db", default="data/hyxlab.duckdb")
    ap.add_argument("--top-n", type=int, default=None, help=f"default {DEFAULT_TOP_N} / ${TOP_N_ENV}")
    ap.add_argument("--interval", type=int, default=None, help=f"seconds between cycles (${INTERVAL_ENV})")
    ap.add_argument("--once", action="store_true", help="one cycle, then exit")
    ap.add_argument(
        "--enable",
        action="store_true",
        help=f"run even if {ENABLE_ENV} is unset (this collector is OFF by default)",
    )
    ap.add_argument("--pause", type=float, default=MARKETS_PAUSE_S, help="seconds between pages")
    ap.add_argument(
        "--close-window-h",
        type=int,
        default=None,
        help=f"include markets closing within N hours (default {CLOSE_WINDOW_H} / ${WINDOW_ENV})",
    )
    args = ap.parse_args()

    if not (args.enable or enabled()):
        # Exit 0: a disabled collector is a normal state, not a failure —
        # a nonzero exit here would make the systemd timer look broken.
        print(f"[breadth] disabled ({ENABLE_ENV} unset); nothing to do", flush=True)
        sys.exit(0)

    n = args.top_n if args.top_n is not None else _env_int(TOP_N_ENV, DEFAULT_TOP_N)
    interval = args.interval if args.interval is not None else _env_int(INTERVAL_ENV, DEFAULT_INTERVAL_S)
    window = (
        args.close_window_h
        if args.close_window_h is not None
        else _env_int(WINDOW_ENV, CLOSE_WINDOW_H)
    )

    sess = requests.Session()
    while True:
        try:
            counts = collect_breadth_once(
                args.db, n=n, session=sess, pause_s=args.pause, close_window_h=window
            )
            print(f"[breadth] {datetime.now(UTC).isoformat()} {counts}", flush=True)
        except Exception as e:  # one bad cycle must not kill a long-running loop
            print(f"[breadth] cycle failed: {type(e).__name__}: {e}", flush=True)
            if args.once:
                raise
        if args.once:
            break
        time.sleep(interval)


if __name__ == "__main__":
    main()
