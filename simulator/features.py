"""FeatureView — the single as-of read gate for time-sensitive signals.

Research (C3) and strategies (via Context) read signals ONLY through
this layer, so they can never disagree about what was knowable at a
timestamp (P1). Every accessor takes `ts` and answers from data whose
knowable_at ≤ ts — inclusive at equality: at the release instant a
live trader has the number.

Vintage semantics (the subtle one): `econ_latest('CPIAUCSL', ts)`
returns the value *as most recently revised before ts* for the most
recent period whose release ≤ ts. Both dimensions (period, revision)
are as-of; a revision of an old period released later than a newer
period's print must not displace the newer period. Resolved via a
prefix array built once at load — O(log n) per query.

Built once per run from a read-only Store; lookups are bisects over
sorted arrays (generalizes the Context forecast index).
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import date, datetime, timedelta
from typing import NamedTuple

from hyxlab.store import Store


class EconObs(NamedTuple):
    obs_date: date
    value: float
    knowable_at: datetime


class NewsAgg(NamedTuple):
    count: int
    mean_tone: float | None


class SignalIndex:
    """Per-key time-sorted values with O(log n) as-of lookup."""

    def __init__(self) -> None:
        self._ts: dict[tuple, list[datetime]] = {}
        self._vals: dict[tuple, list] = {}

    def add(self, key: tuple, ts: datetime, value) -> None:
        self._ts.setdefault(key, []).append(ts)
        self._vals.setdefault(key, []).append(value)

    def freeze(self) -> None:
        for key, tss in self._ts.items():
            order = sorted(range(len(tss)), key=lambda i: tss[i])
            self._ts[key] = [tss[i] for i in order]
            self._vals[key] = [self._vals[key][i] for i in order]

    def asof(self, key: tuple, ts: datetime):
        """Latest value with timestamp ≤ ts, or None."""
        tss = self._ts.get(key)
        if not tss:
            return None
        pos = bisect_right(tss, ts)
        return self._vals[key][pos - 1] if pos else None


class FeatureView:
    def __init__(self) -> None:
        self._forecasts = SignalIndex()
        # per series: rows sorted by knowable_at, plus a prefix array of
        # the resolved (latest period, latest revision) at each position
        self._econ_ts: dict[str, list[datetime]] = {}
        self._econ_rows: dict[str, list[EconObs]] = {}
        self._econ_best: dict[str, list[EconObs]] = {}
        # per topic: knowable_at-sorted with prefix sums for windows
        self._news_ts: dict[str, list[datetime]] = {}
        self._news_tone_sum: dict[str, list[float]] = {}
        self._news_tone_n: dict[str, list[int]] = {}

    @classmethod
    def from_store(cls, store: Store) -> FeatureView:
        fv = cls()
        for station, fetched_at, target_date, high_f in store.conn.execute(
            "SELECT station, fetched_at, target_date, high_f FROM nws_forecasts"
        ).fetchall():
            fv._forecasts.add((station, target_date), fetched_at, high_f)
        fv._forecasts.freeze()

        for series_id, obs_date, value, knowable_at in store.conn.execute(
            "SELECT series_id, obs_date, value, knowable_at FROM econ_vintages"
            " ORDER BY series_id, knowable_at, obs_date"
        ).fetchall():
            obs = EconObs(obs_date, value, knowable_at)
            fv._econ_ts.setdefault(series_id, []).append(knowable_at)
            fv._econ_rows.setdefault(series_id, []).append(obs)
            best = fv._econ_best.setdefault(series_id, [])
            cur = best[-1] if best else None
            # newer period wins; same period: newer revision wins;
            # older period's late revision never displaces a newer print
            best.append(obs if cur is None or obs.obs_date >= cur.obs_date else cur)

        for knowable_at, tone, topics in store.conn.execute(
            "SELECT knowable_at, tone, topics FROM news_items ORDER BY knowable_at"
        ).fetchall():
            for topic in (topics or "").split(","):
                if not topic:
                    continue
                fv._news_ts.setdefault(topic, []).append(knowable_at)
                s = fv._news_tone_sum.setdefault(topic, [0.0])
                n = fv._news_tone_n.setdefault(topic, [0])
                s.append(s[-1] + (tone if tone is not None else 0.0))
                n.append(n[-1] + (1 if tone is not None else 0))
        return fv

    # -- accessors (all as-of ts, inclusive) -----------------------------

    def forecast_high(self, station: str, target_date: date, ts: datetime) -> int | None:
        return self._forecasts.asof((station, target_date), ts)

    def econ_latest(self, series_id: str, ts: datetime) -> EconObs | None:
        """Most recent period released ≤ ts, at its most recent revision ≤ ts."""
        tss = self._econ_ts.get(series_id)
        if not tss:
            return None
        pos = bisect_right(tss, ts)
        return self._econ_best[series_id][pos - 1] if pos else None

    def econ_series_asof(self, series_id: str, ts: datetime, n: int) -> list[EconObs]:
        """Last n periods as known at ts, newest period first."""
        tss = self._econ_ts.get(series_id)
        if not tss:
            return []
        pos = bisect_right(tss, ts)
        latest: dict[date, EconObs] = {}
        for i in range(pos - 1, -1, -1):  # backward: first hit = latest revision
            obs = self._econ_rows[series_id][i]
            if obs.obs_date not in latest:
                latest[obs.obs_date] = obs
        return sorted(latest.values(), key=lambda o: o.obs_date, reverse=True)[:n]

    def news_window(self, topic: str, ts: datetime, window: timedelta) -> NewsAgg:
        """Count and mean tone of items with knowable_at in (ts-window, ts]."""
        tss = self._news_ts.get(topic)
        if not tss:
            return NewsAgg(0, None)
        lo = bisect_right(tss, ts - window)
        hi = bisect_right(tss, ts)
        count = hi - lo
        tone_n = self._news_tone_n[topic][hi] - self._news_tone_n[topic][lo]
        if not tone_n:
            return NewsAgg(count, None)
        tone_sum = self._news_tone_sum[topic][hi] - self._news_tone_sum[topic][lo]
        return NewsAgg(count, tone_sum / tone_n)
