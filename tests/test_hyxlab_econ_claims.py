"""EconClaimsRW + run_econ_claims: the pre-registered spec
(prereg_econ_claims_backtest.md), mechanically. Synthetic data only --
the registration forbids touching the archive's outcomes before the
registered count, and so does this file."""

from datetime import date, datetime, timedelta

import pytest

from hyxlab.models import EconVintage, MarketInfo, Snapshot
from hyxlab.store import Store
from simulator.features import FeatureView
from simulator.run_econ_claims import (
    MIN_EVENTS,
    SECOND_LOOK_EVENTS,
    gate,
    scorable_events,
    score,
    sign_test_k,
)
from simulator.sim import Simulator
from strategies.econ_claims import CCSA, HIST_N, ICSA, EconClaimsRW, Model, fit_model

CLOSE = datetime(2026, 10, 15, 12, 25)  # a Thursday; ICSA obs = 10-10, anchor = 10-03
ANCHOR_OBS = date(2026, 10, 3)
PULL = datetime(2026, 10, 10, 3, 59)  # every vintage stamped knowable before the window


def _vintages(series="ICSA", anchor_obs=ANCHOR_OBS, anchor_value=200_000.0, n=HIST_N + 5):
    """`n` consecutive weekly obs ending at `anchor_obs`, alternating +/-
    5k around the anchor so sigma is non-zero and well defined."""
    out = []
    for i in range(n):
        obs = anchor_obs - timedelta(days=7 * i)
        val = anchor_value + (5_000.0 if i % 2 else 0.0)
        out.append(EconVintage(series, obs, val, PULL))
    return out


def _store(tmp_path, vintages):
    store = Store(tmp_path / "t.duckdb")
    store.insert_vintages(vintages)
    return store


def _ladder(strikes, close=CLOSE, result_print=None, series="KXJOBLESSCLAIMS"):
    out = {}
    for k in strikes:
        res = "" if result_print is None else ("yes" if result_print >= k else "no")
        mid = f"{series}-{close:%y%b%d}-{int(k)}".upper()
        out[("kalshi", mid)] = MarketInfo(
            venue="kalshi",
            market_id=mid,
            series=series,
            close_time=close,
            strike_type="greater_or_equal",
            floor_strike=float(k),
            result=res,
        )
    return out


def _snap(market_id, bid, ask, ts):
    return Snapshot(
        venue="kalshi",
        market_id=market_id,
        ts=ts,
        yes_bid=bid,
        yes_ask=ask,
        no_bid=1 - ask,
        no_ask=1 - bid,
        yes_bid_size=float("inf"),
        yes_ask_size=float("inf"),
        no_bid_size=float("inf"),
        no_ask_size=float("inf"),
    )


def _run(tmp_path, markets, snaps, vintages=None):
    fv = FeatureView.from_store(_store(tmp_path, vintages or _vintages()))
    strat = EconClaimsRW()
    res = Simulator(markets, [strat], features=fv).run(snaps)
    return res, strat


# -- model ----------------------------------------------------------------


def test_fit_model_is_the_random_walk_on_the_prior_week():
    fv_obs = sorted(_vintages(), key=lambda v: v.obs_date, reverse=True)
    model, why = fit_model(fv_obs, CLOSE.date(), ICSA)
    assert why == "" and model.anchor == 200_000.0
    # alternating +/-5k changes: every diff is +-5000, sample std of 52 of them
    assert model.sigma == pytest.approx(5_000.0, rel=0.02)


def test_fit_model_refuses_a_stale_or_missing_anchor():
    # anchor one week older than the prior week -> the prior print is missing
    stale = sorted(_vintages(anchor_obs=ANCHOR_OBS - timedelta(days=7)), key=lambda v: v.obs_date)
    model, why = fit_model(stale[::-1], CLOSE.date(), ICSA)
    assert model is None and "anchor obs" in why
    short = sorted(_vintages(n=HIST_N - 1), key=lambda v: v.obs_date, reverse=True)
    assert fit_model(short, CLOSE.date(), ICSA)[0] is None


def test_p_yes_is_monotone_and_continuity_corrected_toward_yes():
    m = Model(anchor=200_000.0, sigma=5_000.0)
    ps = [m.p_yes(k) for k in (190_000, 195_000, 200_000, 205_000, 210_000)]
    assert ps == sorted(ps, reverse=True)
    # at the anchor strike, P(print >= anchor) > 1/2: equality settles YES
    assert m.p_yes(200_000) > 0.5


# -- strategy -------------------------------------------------------------


def test_trades_both_sides_only_when_the_model_clears_the_edge(tmp_path):
    markets = _ladder([190_000, 200_000, 210_000])
    ts = CLOSE - timedelta(minutes=58)
    # 190k: model ~0.98 yes; ask 0.80 -> YES at 0.80. 210k: model ~0.03 yes,
    # no_ask = 1 - bid = 0.25 -> NO at 0.25. 200k: model ~0.54, ask 0.55 -> no edge.
    snaps = [
        _snap("KXJOBLESSCLAIMS-26OCT15-190000", 0.75, 0.80, ts),
        _snap("KXJOBLESSCLAIMS-26OCT15-200000", 0.45, 0.55, ts),
        _snap("KXJOBLESSCLAIMS-26OCT15-210000", 0.75, 0.80, ts),
    ]
    res, strat = _run(tmp_path, markets, snaps)
    by_mid = {f.market_id: f for f in res.fills}
    assert set(by_mid) == {"KXJOBLESSCLAIMS-26OCT15-190000", "KXJOBLESSCLAIMS-26OCT15-210000"}
    y = by_mid["KXJOBLESSCLAIMS-26OCT15-190000"]
    n = by_mid["KXJOBLESSCLAIMS-26OCT15-210000"]
    assert (y.side, y.qty, y.price) == ("yes", 10, 0.80)
    assert (n.side, n.qty, n.price) == ("no", 10, 0.25)
    assert "no edge" in strat.skipped[("kalshi", "KXJOBLESSCLAIMS-26OCT15-200000")]


def test_one_look_inside_the_window_only(tmp_path):
    markets = _ladder([190_000])
    mid = "KXJOBLESSCLAIMS-26OCT15-190000"
    snaps = [
        _snap(mid, 0.75, 0.80, CLOSE - timedelta(minutes=61)),  # before the window
        _snap(mid, 0.98, 0.99, CLOSE - timedelta(minutes=59)),  # the look: no edge at 0.99
        _snap(mid, 0.75, 0.80, CLOSE - timedelta(minutes=55)),  # edge, but already looked
        _snap(mid, 0.75, 0.80, CLOSE - timedelta(minutes=49)),  # after the window
    ]
    res, strat = _run(tmp_path, markets, snaps)
    assert res.fills == []
    assert "no edge" in strat.skipped[("kalshi", mid)]


def test_missing_prior_print_means_no_trade_not_a_guess(tmp_path):
    markets = _ladder([190_000])
    mid = "KXJOBLESSCLAIMS-26OCT15-190000"
    snaps = [_snap(mid, 0.75, 0.80, CLOSE - timedelta(minutes=58))]
    # vintages whose newest obs is TWO weeks before the event's obs
    res, strat = _run(
        tmp_path, markets, snaps, vintages=_vintages(anchor_obs=ANCHOR_OBS - timedelta(days=7))
    )
    assert res.fills == [] and "anchor obs" in strat.skipped[("kalshi", mid)]


def test_vintage_stamped_after_decision_time_is_not_knowable(tmp_path):
    markets = _ladder([190_000])
    mid = "KXJOBLESSCLAIMS-26OCT15-190000"
    snaps = [_snap(mid, 0.75, 0.80, CLOSE - timedelta(minutes=58))]
    late = [
        EconVintage(v.series_id, v.obs_date, v.value, CLOSE + timedelta(hours=15))
        for v in _vintages()
    ]
    res, strat = _run(tmp_path, markets, snaps, vintages=late)
    assert res.fills == [] and "history 0" in strat.skipped[("kalshi", mid)]


def test_ccsa_unit_guard_excludes_a_strike_quoted_in_the_wrong_unit(tmp_path):
    close = CLOSE
    # CCSA anchor obs = close - 12 - 7 days
    vint = _vintages(
        series="CCSA", anchor_obs=close.date() - timedelta(days=19), anchor_value=1_710_000.0
    )
    markets = _ladder([1700, 1.75], close=close, series="KXCONTCLAIMS")
    ts = close - timedelta(minutes=58)
    snaps = [
        _snap("KXCONTCLAIMS-26OCT15-1700", 0.60, 0.65, ts),
        _snap("KXCONTCLAIMS-26OCT15-1", 0.60, 0.65, ts),  # the 1.75 (millions) rung
    ]
    fv = FeatureView.from_store(_store(tmp_path, vint))
    strat = EconClaimsRW(pair=CCSA)
    Simulator(markets, [strat], features=fv).run(snaps)
    assert strat.skipped[("kalshi", "KXCONTCLAIMS-26OCT15-1")].startswith("UNIT")
    assert ("kalshi", "KXCONTCLAIMS-26OCT15-1700") in strat.looked


# -- scorer ---------------------------------------------------------------


def test_sign_test_k_matches_the_registered_table():
    assert {n: sign_test_k(n) for n in (14, 20, 26, 30, 40)} == {
        14: 11,
        20: 15,
        26: 18,
        30: 20,
        40: 26,
    }


def test_count_gate_refuses_under_the_registered_read():
    assert gate(MIN_EVENTS - 1, False).startswith("GATED")
    assert gate(MIN_EVENTS, False) is None
    assert gate(SECOND_LOOK_EVENTS - 1, True).startswith("GATED")
    assert gate(SECOND_LOOK_EVENTS, True) is None


def test_scorable_events_counts_from_quotes_and_vintages_not_outcomes(tmp_path):
    fv = FeatureView.from_store(_store(tmp_path, _vintages()))
    strat = EconClaimsRW()
    settled = _ladder([190_000, 200_000], result_print=201_000)
    unsettled = _ladder([190_000], close=CLOSE + timedelta(days=7))
    markets = {**settled, **unsettled}
    snaps = [
        _snap("KXJOBLESSCLAIMS-26OCT15-190000", 0.75, 0.80, CLOSE - timedelta(minutes=58)),
        _snap("KXJOBLESSCLAIMS-26OCT22-190000", 0.75, 0.80, CLOSE + timedelta(days=7, minutes=-58)),
    ]
    ev = scorable_events(markets, snaps, fv, ICSA, strat)
    assert ev[CLOSE.date()]["scorable"] is True and ev[CLOSE.date()]["strikes"] == 1
    assert ev[(CLOSE + timedelta(days=7)).date()] == {"scorable": False, "why": "not settled"}
    # same settled event with its only snapshot outside the window -> not scorable
    ev2 = scorable_events(
        settled,
        [_snap("KXJOBLESSCLAIMS-26OCT15-190000", 0.75, 0.80, CLOSE - timedelta(minutes=30))],
        fv,
        ICSA,
        strat,
    )
    assert ev2[CLOSE.date()]["why"] == "no snapshot in decision window"


def test_score_judges_at_the_event_level():
    from hyxlab.models import Fill

    def fill(mid, close, side, price, fee=0.1):
        return Fill("econ_claims", "kalshi", mid, side, 10.0, price, fee, close, False)

    rows = []
    for i in range(22):
        close = CLOSE + timedelta(days=7 * i)
        info = MarketInfo(
            "kalshi", f"M{i}", series="KXJOBLESSCLAIMS", close_time=close, result="yes"
        )
        # 19 winning events, 3 losing; two fills in event 0 so fills != events
        win = i < 19
        rows.append((fill(f"M{i}", close, "yes", 0.60), info, 10.0 if win else 0.0))
        if i == 0:
            rows.append((fill("M0b", close, "yes", 0.60), info, 10.0))
    block = score(rows, n_scorable=26)
    assert block["traded_events"] == 22 and block["fills"] == 23
    assert block["event_wins"] == 19 and block["sign_test_k"] == sign_test_k(22)
    assert block["thresholds"]["wins_ge_k"] is True
    assert block["verdict"].startswith("SURVIVE")
    under = score(rows[:10], n_scorable=26)
    assert under["verdict"].startswith("UNDERPOWERED")


# --- --watch: the standing read of the SAMPLE (2026-11-24) -----------------


def _ev(**kw):
    return {"scorable": False, **kw} if "why" in kw else {"scorable": True, **kw}


def test_watch_summary_separates_pending_from_lost_and_dates_the_read():
    from simulator.run_econ_claims import watch_summary

    today = date(2026, 11, 24)
    events = {
        date(2026, 7, 30): _ev(why="no snapshot in decision window"),
        date(2026, 11, 12): _ev(strikes=11),
        date(2026, 11, 19): _ev(strikes=11),
        date(2026, 11, 25): _ev(why="not settled"),
    }
    w = watch_summary(events, today, need=5)
    assert w["scorable"] == 2
    assert w["lost"] == [(date(2026, 7, 30), "no snapshot in decision window")]
    assert w["recent_lost"] == []
    assert w["pending"] == [date(2026, 11, 25)]
    # 3 more needed, one of them the pending event: two weeks past it.
    assert w["opens"] == date(2026, 12, 9)


def test_watch_summary_a_window_missed_last_week_is_a_recent_loss():
    from simulator.run_econ_claims import RECENT_LOSS_DAYS, watch_summary

    today = date(2026, 11, 24)
    events = {
        date(2026, 11, 12): _ev(strikes=11),
        date(2026, 11, 19): _ev(why="no snapshot in decision window"),
    }
    w = watch_summary(events, today)
    assert w["recent_lost"] == [(date(2026, 11, 19), "no snapshot in decision window")]
    # ... and stops failing the unit once the next event has had its chance.
    later = date(2026, 11, 19) + timedelta(days=RECENT_LOSS_DAYS + 1)
    assert watch_summary(events, later)["recent_lost"] == []
    assert watch_summary(events, later)["lost"] == w["lost"]


def test_watch_summary_an_overdue_settlement_is_lost_not_pending():
    from simulator.run_econ_claims import SETTLE_GRACE_DAYS, watch_summary

    ev = date(2026, 11, 19)
    events = {ev: _ev(why="not settled")}
    inside = watch_summary(events, ev + timedelta(days=SETTLE_GRACE_DAYS))
    assert inside["pending"] == [ev] and inside["lost"] == []
    past = watch_summary(events, ev + timedelta(days=SETTLE_GRACE_DAYS + 1))
    assert past["pending"] == [] and past["recent_lost"] == [(ev, "not settled")]


def test_watch_line_carries_every_field_of_the_summary():
    """By perturbation (#95): bump each field, the line must change."""
    from simulator.run_econ_claims import watch_line

    base = {
        "scorable": 16,
        "need": 26,
        "lost": [(date(2026, 7, 30), "no snapshot in decision window")],
        "recent_lost": [],
        "pending": [date(2026, 11, 25)],
        "opens": date(2027, 1, 27),
    }
    line = watch_line("ICSA", base)
    assert "16 of 26 scorable" in line and "2027-01-27" in line and "2026-11-25" in line
    bumps = {
        "scorable": 17,
        "need": 40,
        "lost": [(date(2026, 8, 6), "history 0 < 53")],
        "pending": [],
        "opens": date(2027, 2, 3),
    }
    for k, v in bumps.items():
        assert watch_line("ICSA", {**base, k: v}) != line, k
    # `recent_lost` is rendered by watch() as its own LOST line, not here.
    assert "OPEN" in watch_line("ICSA", {**base, "scorable": 26, "opens": None})


def test_watch_exits_1_on_a_recent_loss_and_never_replays(monkeypatch, capsys):
    import simulator.run_econ_claims as rec

    def boom(*a, **k):
        raise AssertionError("--watch must not replay")

    monkeypatch.setattr(rec, "Simulator", boom)
    monkeypatch.setattr(rec, "score", boom)
    monkeypatch.delenv("HYXLAB_BACKUP_DIR", raising=False)
    monkeypatch.setattr(rec, "_load", lambda db, pair: ([], {}, [], None))
    table = {
        "ICSA": {
            date(2026, 11, 12): _ev(strikes=11),
            date(2026, 11, 19): _ev(why="no snapshot in decision window"),
        },
        "CCSA": {date(2026, 11, 19): _ev(strikes=11)},
    }
    monkeypatch.setattr(rec, "scorable_events", lambda m, s, fv, pair, strat: table[pair.series_id])
    assert rec.watch("nope.duckdb", today=date(2026, 11, 24)) == 1
    out = capsys.readouterr().out.strip().splitlines()
    assert out[-1].startswith("[econ-claims] LOST ICSA 2026-11-19: no snapshot")
    assert any(line.startswith("[econ-claims] watch CCSA: 1 of 26") for line in out)
    assert "live nope.duckdb" in out[0]
    # The same sample a fortnight on: the loss stands in the count, the unit is green.
    assert rec.watch("nope.duckdb", today=date(2026, 12, 8)) == 0
    assert "1 lost for good" in capsys.readouterr().out


def test_loss_advice_sends_only_a_missing_snapshot_to_the_collector():
    # 2026-11-26 03:52Z: both 11-25 events (a Wednesday close for Thanksgiving)
    # were lost to the registered anchor date and the line blamed the collector,
    # which had captured the window.
    from simulator.run_econ_claims import NO_SNAPSHOT, loss_advice

    wed, thu = date(2026, 11, 25), date(2026, 11, 19)
    assert "5-min collector out of" in loss_advice(thu, NO_SNAPSHOT)
    shifted = loss_advice(wed, "anchor obs 2026-11-14 != 2026-11-13")
    assert "NOT the collector" in shifted and "closed on a Wednesday" in shifted
    stale = loss_advice(thu, "anchor obs 2026-10-31 != 2026-11-07")
    assert "NOT the collector" in stale and "Wednesday" not in stale and "holiday" not in stale
    assert "NOT the collector" in loss_advice(thu, "history 12 < 53")
