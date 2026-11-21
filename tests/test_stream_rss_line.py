"""streamd publishes its OWN anonymous RSS on every stats line (#134).

The stream runs under `MemoryMax=4G`, and the cgroup's figures cannot show
a climb: `memory.current` is mostly the page cache of the daemon's own
archive writes and `MemoryPeak` sits at the cap for any healthy writer
(ops rule). The shadow learned this on 2026-10-23 when the kernel's OOM
line was the only record of how it got to `anon-rss:1039464kB`; the
stream's anon ran 734M -> 1049M over the first 18h of the expanded book
universe (2026-10-24/25) with no record but the operator's ad-hoc cgroup
reads. So the holder samples itself, through the ONE renderer both
publishers (periodic + shutdown) use (#95), and a shared helper in the
kernel so the two daemons cannot drift apart on what "anon RSS" means.
"""

import inspect

import collector.streamd as streamd
from hyxlab import memcap


def test_rss_anon_mb_is_one_helper_shared_by_both_daemons():
    import simulator.shadow as shadow

    assert shadow.rss_anon_mb is memcap.rss_anon_mb
    assert streamd.rss_anon_mb is memcap.rss_anon_mb
    rss = memcap.rss_anon_mb()
    assert rss is not None and rss > 0


def test_stats_line_carries_anon_rss_and_is_the_only_renderer(monkeypatch):
    monkeypatch.setattr(streamd, "rss_anon_mb", lambda: 123.456)
    monkeypatch.setattr(streamd, "glibc_arena_heaps", lambda: (76, 753.04))
    line = streamd.stats_line("", {"kalshi_trades": 2}, 150, "sock_throttled=441 (+0)")
    assert line == (
        "stats {'kalshi_trades': 2} (last flush 150 rows)"
        " rss_anon_mb=123.5 arena_mb=753.0/76 sock_throttled=441 (+0)"
    )
    # Perturbation: the arena split reaches the line too.
    monkeypatch.setattr(streamd, "glibc_arena_heaps", lambda: (75, 753.04))
    assert streamd.stats_line("", {"kalshi_trades": 2}, 150, "sock_throttled=441 (+0)") != line
    monkeypatch.setattr(streamd, "glibc_arena_heaps", lambda: (76, 753.04))
    # Perturbation: the reading reaches the line (not read into a local and
    # dropped, #95).
    monkeypatch.setattr(streamd, "rss_anon_mb", lambda: 7.0)
    assert streamd.stats_line("", {"kalshi_trades": 2}, 150, "sock_throttled=441 (+0)") != line
    # An unmeasured sample is None, never 0.0 (#91).
    monkeypatch.setattr(streamd, "rss_anon_mb", lambda: None)
    monkeypatch.setattr(streamd, "glibc_arena_heaps", lambda: None)
    shut = streamd.stats_line("shutdown; ", {"a": 1}, None, None)
    # The shutdown marker the e2e test and the journal grep look for stays
    # a prefix, and the shutdown publisher carries the same reading.
    assert shut.startswith("shutdown; stats {'a': 1}")
    assert shut.endswith(" rss_anon_mb=None arena_mb=None")
    assert "flushed" not in shut and "sock_throttled" not in shut
    # One renderer: no second site may format `stats {` into a line.
    src = inspect.getsource(streamd)
    assert src.count("stats {stats}") == 1
    assert src.count('f"stats {') == 0 and src.count('f"shutdown; stats {') == 0
    assert src.count("stats_line(") == 3  # the def + the two publishers


SMAPS_FIXTURE = """\
7f8984000000-7f8987cb6000 rw-p 00000000 00:00 0
Size:              62168 kB
Rss:               10240 kB
Pss:               10240 kB
7f8987cb6000-7f8988000000 ---p 00000000 00:00 0
Size:               3368 kB
Rss:                   0 kB
7f898c000000-7f898ffcf000 rw-p 00000000 00:00 0
Size:              65340 kB
Rss:                2048 kB
7f89b0200000-7f89b0400000 rw-p 00000000 00:00 0
Size:               2048 kB
Rss:                2048 kB
55d2c3a00000-55d2c3b00000 rw-p 00000000 00:00 0                          [heap]
Size:               1024 kB
Rss:                1024 kB
7f89c0000000-7f89c0100000 rw-p 00000000 00:00 0
Size:               1024 kB
Rss:                1024 kB
7f89d0000000-7f89d0005000 r--p 00000000 08:01 1234 /usr/lib/libc.so.6
Size:                 20 kB
Rss:                  20 kB
"""


def test_glibc_arena_heaps_counts_only_64mib_aligned_anonymous_heaps():
    heaps, mib = memcap.glibc_arena_heaps_from_smaps(SMAPS_FIXTURE)
    # Two heaps: the committed rw-p halves at 7f8984000000 and 7f898c000000
    # (64 MiB-aligned, anonymous). Their PROT_NONE tails are not aligned.
    # Excluded: the 2 MiB-aligned jemalloc extent (not 64 MiB-aligned), the
    # named `[heap]`, and the 1 MiB pymalloc arena at 7f89c0000000 -- that
    # one IS 64 MiB-aligned and anonymous, and is counted: the discriminator
    # is alignment plus size, and 1 MiB is the floor; the reading's own
    # docstring prices pymalloc at 23 MiB total, so a stray arena landing
    # on a boundary is noise, not a wrong class.
    assert heaps == 3
    assert mib == (10240 + 2048 + 1024) / 1024
    # Live reading exists on Linux and is a (count, MiB) pair.
    live = memcap.glibc_arena_heaps()
    assert live is not None and live[0] >= 0 and live[1] >= 0.0
    # Empty / garbage input is a zero reading, not a crash.
    assert memcap.glibc_arena_heaps_from_smaps("") == (0, 0.0)
    assert memcap.glibc_arena_heaps_from_smaps("garbage\nRss: 5 kB\n") == (0, 0.0)


# -- the owner's own holds on its archive (mistakes #211) --------------------


def test_stats_line_carries_the_flush_holds_and_omits_an_unmeasured_period(monkeypatch):
    monkeypatch.setattr(streamd, "rss_anon_mb", lambda: 1.0)
    monkeypatch.setattr(streamd, "glibc_arena_heaps", lambda: None)
    line = streamd.stats_line("", {"a": 1}, 5, None, (19, 17.34, 2.06))
    assert line.endswith(" rss_anon_mb=1.0 arena_mb=None flush_hold=19/17.3/2.1")
    # Perturbation, one field at a time: each reaches the line (#95).
    for holds in ((20, 17.34, 2.06), (19, 18.34, 2.06), (19, 17.34, 3.06)):
        assert streamd.stats_line("", {"a": 1}, 5, None, holds) != line
    # No reading is no clause -- never a zero that reads as an idle file.
    assert "flush_hold" not in streamd.stats_line("", {"a": 1}, 5, None)
    # Both publishers hand the store's counters to the one renderer.
    src = inspect.getsource(streamd)
    assert src.count("holds = self.store.take_flush_holds()") == 2
    assert src.count("sock, holds, steps, lag, rx))") == 1
    assert src.count("None, None, holds, steps, lag))") == 1


def test_a_flush_that_holds_a_whole_period_gets_its_own_line():
    assert streamd.LONG_FLUSH_HOLD_S == streamd.FLUSH_SECS
    assert streamd.long_flush_line(None, 10) is None
    assert streamd.long_flush_line(streamd.FLUSH_SECS - 0.1, 10) is None
    assert streamd.long_flush_line(streamd.FLUSH_SECS, 10) == "long flush: held 15.0s for 10 rows"
    assert streamd.long_flush_line(37.26, 133483) == "long flush: held 37.3s for 133483 rows"
    # The flusher is the one caller, and it passes what the flush returned.
    src = inspect.getsource(streamd)
    assert src.count("long_flush_line(out.held_s, out.rows)") == 1


def test_a_flush_times_its_hold_from_the_open_through_the_close(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    from hyxlab import streamstore
    from hyxlab.streamstore import StreamStore

    store = StreamStore(tmp_path / "s.duckdb")
    # Nothing buffered: the file is never opened, so nothing was held.
    out = store.flush_outcome()
    assert out.touched is False and out.held_s is None
    assert store.take_flush_holds() == (0, 0.0, 0.0)

    # A clock that advances one second per reading: the hold is then the
    # number of readings between the open and the close, and a hold timed
    # from BEFORE the connect or read before the close has a different one.
    ticks = iter(range(1000))
    order: list[str] = []
    monkeypatch.setattr(streamstore.time, "monotonic", lambda: order.append("t") or next(ticks))
    real_connect = streamstore.duck_connect

    class Conn:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            order.append("close")
            return self._inner.__exit__(*exc)

    def connect(*a, **kw):
        conn = Conn(real_connect(*a, **kw))  # the attach ledger reads the clock too
        order.append("open")
        return conn

    monkeypatch.setattr(streamstore, "duck_connect", connect)
    now = datetime.now(UTC)
    store.append_gap("kalshi", "books", now, now, "reconnect")
    out = store.flush_outcome()
    assert out.touched is True and out.rows == 1
    # The first reading after the open comes right after it, and the last
    # one right after the close -- ONE reading there, shared by the hold and
    # the step split. The readings in between are the steps' own
    # (`take_flush_steps`), so the hold is their count less one.
    opened = len(order) - 1 - order[::-1].index("open")
    assert order[opened + 1] == "t" and order[-2:] == ["close", "t"]
    hold = float(order[opened:].count("t") - 1)
    assert out.held_s == hold and hold >= 1.0
    store.append_gap("kalshi", "books", now, now, "reconnect")
    assert store.flush_outcome().held_s == hold
    assert store.take_flush_holds() == (2, 2 * hold, hold)
    # Taken is reset: the next period starts from nothing.
    assert store.take_flush_holds() == (0, 0.0, 0.0)


def test_a_flush_that_fails_after_the_open_still_counts_its_hold(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    import pytest

    from hyxlab.streamstore import StreamStore

    store = StreamStore(tmp_path / "s.duckdb")
    now = datetime.now(UTC)
    store.append_gap("kalshi", "books", now, now, "reconnect")

    def boom(*a, **kw):
        raise RuntimeError("insert failed")

    monkeypatch.setattr(store, "_insert", boom)
    with pytest.raises(RuntimeError):
        store.flush_outcome()
    n, total, worst = store.take_flush_holds()
    assert n == 1 and total > 0 and worst == total
    assert store.pending == 1  # and the batch is back in the buffer

    # A flush REFUSED at the open held nothing and counts nothing.
    from hyxlab import streamstore

    def refused(*a, **kw):
        raise OSError("Could not set lock on file")

    monkeypatch.setattr(streamstore, "duck_connect", refused)
    with pytest.raises(OSError):
        store.flush_outcome()
    assert store.take_flush_holds() == (0, 0.0, 0.0)


# -- the hold by step (2026-11-19) -------------------------------------------


def test_stats_line_splits_the_hold_by_step_only_beside_the_hold(monkeypatch):
    monkeypatch.setattr(streamd, "rss_anon_mb", lambda: 1.0)
    monkeypatch.setattr(streamd, "glibc_arena_heaps", lambda: None)
    steps = (0.36, 9.04, 1.8, 10.84)
    line = streamd.stats_line("", {"a": 1}, 5, None, (18, 24.6, 1.6), steps)
    assert line.endswith(" flush_hold=18/24.6/1.6 flush_steps=0.36/9.04/1.80/10.84")
    for i in range(4):  # each part reaches the line (#95)
        moved = tuple(s + (1.0 if j == i else 0.0) for j, s in enumerate(steps))
        assert streamd.stats_line("", {"a": 1}, 5, None, (18, 24.6, 1.6), moved) != line
    assert "flush_steps" not in streamd.stats_line("", {"a": 1}, 5, None, None, steps)
    src = inspect.getsource(streamd)
    assert src.count("steps = self.store.take_flush_steps()") == 2
    assert src.count("holds, steps, lag, rx))") == 2


def test_the_store_times_the_steps_of_a_flush_and_hands_them_over_once(tmp_path):
    from datetime import UTC, datetime

    from hyxlab.streamstore import BookEvent, StreamStore

    store = StreamStore(tmp_path / "s.duckdb")
    t = datetime(2026, 11, 19, tzinfo=UTC)

    def fill():
        store.append_events(
            [BookEvent("kalshi", "M", t, t, 1, i, "delta", "yes", 0.4, 1.0) for i in range(2000)]
        )

    fill()
    held = store.flush_outcome().held_s
    stage, load, commit, close = store.take_flush_steps()
    assert stage == 0.0  # a healthy flush stages before the open
    assert load > 0.0 and commit > 0.0 and close > 0.0
    assert load + commit + close <= held
    assert store.take_flush_steps() == (0.0, 0.0, 0.0, 0.0)  # handed over, not kept

    store._stage_in_hold = True  # the order after a failed flush
    fill()
    held = store.flush_outcome().held_s
    stage, load, commit, close = store.take_flush_steps()
    assert stage > 0.0 and stage + load + commit + close <= held
