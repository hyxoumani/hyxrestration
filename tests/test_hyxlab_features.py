"""FeatureView as-of correctness: boundary inclusivity, two-dimensional
vintage semantics, news windows, and the P1 property — no accessor ever
answers from data with knowable_at > ts."""

import random
from datetime import UTC, date, datetime, timedelta

from hyxlab.models import EconVintage, Forecast, NewsItem
from hyxlab.store import Store
from simulator.features import FeatureView

T0 = datetime(2026, 7, 1, 12, 0)
JUN, JUL = date(2026, 6, 1), date(2026, 7, 1)


def _store(tmp_path):
    store = Store(tmp_path / "t.duckdb")
    store.insert_vintages(
        [
            # June CPI printed Jul 10, revised Jul 25; July CPI printed Aug 12
            EconVintage("CPIAUCSL", JUN, 320.0, datetime(2026, 7, 10, 12, 30)),
            EconVintage("CPIAUCSL", JUN, 320.4, datetime(2026, 7, 25, 12, 30)),
            EconVintage("CPIAUCSL", JUL, 321.0, datetime(2026, 8, 12, 12, 30)),
            # late revision of JUNE lands AFTER July's print
            EconVintage("CPIAUCSL", JUN, 320.6, datetime(2026, 8, 20, 12, 30)),
        ]
    )
    store.insert_news(
        [
            NewsItem("gdelt", f"h{i}", None, T0 + timedelta(hours=i), tone=t, topics="inflation")
            for i, t in enumerate([-2.0, None, 4.0])
        ]
    )
    store.insert_forecasts(
        [
            Forecast("NYC", datetime(2026, 7, 1, 6, 0, tzinfo=UTC), JUL, 88),
            Forecast("NYC", datetime(2026, 7, 1, 11, 0, tzinfo=UTC), JUL, 91),
        ]
    )
    return store


def test_asof_boundary_is_inclusive(tmp_path):
    fv = FeatureView.from_store(_store(tmp_path))
    release = datetime(2026, 7, 10, 12, 30)
    assert fv.econ_latest("CPIAUCSL", release).value == 320.0  # at the instant
    assert fv.econ_latest("CPIAUCSL", release - timedelta(microseconds=1)) is None


def test_vintage_revision_visibility(tmp_path):
    fv = FeatureView.from_store(_store(tmp_path))
    assert fv.econ_latest("CPIAUCSL", datetime(2026, 7, 20)).value == 320.0
    assert fv.econ_latest("CPIAUCSL", datetime(2026, 7, 26)).value == 320.4  # revised


def test_late_revision_of_old_period_never_displaces_newer_print(tmp_path):
    fv = FeatureView.from_store(_store(tmp_path))
    at = datetime(2026, 8, 21)
    latest = fv.econ_latest("CPIAUCSL", at)
    assert latest.obs_date == JUL and latest.value == 321.0
    # ...but the revision IS visible in the as-of series view
    series = fv.econ_series_asof("CPIAUCSL", at, 2)
    assert [(o.obs_date, o.value) for o in series] == [(JUL, 321.0), (JUN, 320.6)]


def test_econ_series_asof_pre_revision(tmp_path):
    fv = FeatureView.from_store(_store(tmp_path))
    series = fv.econ_series_asof("CPIAUCSL", datetime(2026, 7, 12), 5)
    assert [(o.obs_date, o.value) for o in series] == [(JUN, 320.0)]


def test_news_window_edges_and_none_tones(tmp_path):
    fv = FeatureView.from_store(_store(tmp_path))
    # items at T0, T0+1h (tone None), T0+2h
    agg = fv.news_window("inflation", T0 + timedelta(hours=2), timedelta(hours=2))
    # window (T0, T0+2h]: excludes T0 exactly at the open edge
    assert agg.count == 2
    assert agg.mean_tone == 4.0  # the None-tone item counts but doesn't average
    assert fv.news_window("inflation", T0, timedelta(hours=1)).count == 1
    assert fv.news_window("nosuch", T0, timedelta(hours=1)) == (0, None)


def test_forecast_high_asof(tmp_path):
    fv = FeatureView.from_store(_store(tmp_path))
    assert fv.forecast_high("NYC", JUL, datetime(2026, 7, 1, 10, 0)) == 88
    assert fv.forecast_high("NYC", JUL, datetime(2026, 7, 1, 11, 0)) == 91
    assert fv.forecast_high("NYC", JUL, datetime(2026, 7, 1, 5, 0)) is None


def test_property_never_references_future_knowable_at(tmp_path):
    """P1 unit-level proof: for random ts, every answer's knowable_at ≤ ts."""
    store = Store(tmp_path / "p.duckdb")
    rng = random.Random(7)
    vs = [
        EconVintage(
            "ICSA",
            date(2026, 1, 1) + timedelta(days=7 * (i // 3)),
            200000 + i,
            datetime(2026, 1, 4) + timedelta(days=7 * (i // 3), hours=rng.randint(0, 96)),
        )
        for i in range(60)
    ]
    store.insert_vintages(vs)
    fv = FeatureView.from_store(store)
    lo, hi = datetime(2026, 1, 1), datetime(2026, 8, 1)
    for _ in range(500):
        ts = lo + timedelta(seconds=rng.randint(0, int((hi - lo).total_seconds())))
        latest = fv.econ_latest("ICSA", ts)
        if latest is not None:
            assert latest.knowable_at <= ts
        for obs in fv.econ_series_asof("ICSA", ts, 4):
            assert obs.knowable_at <= ts


def test_context_delegates_signals_asof_now(tmp_path):
    from simulator.strategy import Context

    fv = FeatureView.from_store(_store(tmp_path))
    ctx = Context({}, features=fv)
    ctx.now = datetime(2026, 7, 26)
    assert ctx.econ_latest("CPIAUCSL").value == 320.4
    assert ctx.econ_series("CPIAUCSL", 1)[0].value == 320.4
    assert ctx.news_window("inflation", timedelta(days=30)).count == 3
    ctx.now = None  # before the first snapshot: no signals
    assert ctx.econ_latest("CPIAUCSL") is None
    assert Context({}).econ_latest("CPIAUCSL") is None  # no feed wired
