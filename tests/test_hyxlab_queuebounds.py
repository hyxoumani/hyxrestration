"""Queue-position bounds: FIFO consumption, cancel ambiguity bracket,
level clamp narrowing, tape-vs-delta double-count protection."""

from datetime import datetime, timedelta

from simulator.queuebounds import QueueTracker, consuming_print

T0 = datetime(2026, 7, 11, 12, 0)


def ts(s):
    return T0 + timedelta(seconds=s)


def test_consuming_print_side_mapping():
    # probed 2026-07-11: no-taker lifts yes-book at p; yes-taker lifts
    # no-book at 1-p; same-side prints never consume our level
    assert consuming_print("yes", 0.40, "no", 0.40)
    assert consuming_print("no", 0.60, "yes", 0.40)
    assert not consuming_print("yes", 0.40, "yes", 0.40)
    assert not consuming_print("no", 0.60, "no", 0.60)
    assert not consuming_print("yes", 0.40, "no", 0.41)


def test_prints_consume_ahead_then_fill_both_bounds():
    q = QueueTracker(side="yes", price=0.40, qty=10, level_size=30)
    q.on_print(ts(1), 20)  # 10 of 30 ahead remain
    assert (q.filled_pess, q.filled_opt) == (0.0, 0.0)
    q.on_print(ts(2), 15)  # consumes last 10 ahead, fills 5 of ours
    assert (q.filled_pess, q.filled_opt) == (5.0, 5.0)
    q.on_print(ts(3), 50)  # fill capped at order qty
    assert (q.filled_pess, q.filled_opt) == (10.0, 10.0)
    assert q.done
    assert [(t, f) for t, f in q.fill_events] == [(ts(2), 5.0), (ts(3), 5.0)]


def test_anonymous_cancel_diverges_bounds_and_only_opt_fills():
    q = QueueTracker(side="yes", price=0.40, qty=10, level_size=30)
    q.on_delta(ts(1), -20)  # no print in window: anonymous cancel
    assert q.ahead_opt == 10.0  # optimistic: cancels were ahead
    assert q.ahead_pess == 10.0  # clamped to displayed level (30-20)
    q.on_delta(ts(2), -5)  # another cancel: opt 5, level 5 clamps pess
    assert (q.ahead_pess, q.ahead_opt) == (5.0, 5.0)


def test_level_clamp_narrows_pessimistic_bound():
    q = QueueTracker(side="yes", price=0.40, qty=10, level_size=100)
    q.on_delta(ts(1), -97)  # thin book: only 3 displayed remain
    assert q.ahead_pess == 3.0  # can't have more ahead than displayed
    q.on_print(ts(2), 5)  # 3 ahead consumed, 2 fill us — even pessimistically
    assert q.filled_pess == 2.0


def test_trade_explained_decrement_is_not_a_cancel():
    q = QueueTracker(side="yes", price=0.40, qty=10, level_size=30)
    q.on_print(ts(1), 12)  # tape consumes 12 ahead (both bounds -> 18)
    q.on_delta(ts(1), -12)  # venue's own decrement for that print
    # opt must NOT double-shrink: 18, not 6
    assert (q.ahead_pess, q.ahead_opt) == (18.0, 18.0)


def test_stale_prints_age_out_of_absorption_window():
    q = QueueTracker(side="yes", price=0.40, qty=10, level_size=30)
    q.on_print(ts(0), 12)
    q.on_delta(ts(10), -12)  # far outside the 2s window: real cancel
    assert q.ahead_opt == 6.0  # 30 - 12 (print) - 12 (cancel, all ahead)
    assert q.ahead_pess == 18.0  # 30 - 12 (print); level 18 doesn't clamp


def test_level_increase_joins_behind_us():
    q = QueueTracker(side="yes", price=0.40, qty=10, level_size=10)
    q.on_delta(ts(1), 50)  # new liquidity queues behind
    assert (q.ahead_pess, q.ahead_opt) == (10.0, 10.0)
    q.on_print(ts(2), 15)  # consumes our 10 ahead, fills 5
    assert q.filled_pess == 5.0
