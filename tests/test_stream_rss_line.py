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
    line = streamd.stats_line("", {"kalshi_trades": 2}, 150, "sock_throttled=441 (+0)")
    assert line == (
        "stats {'kalshi_trades': 2} (flushed 150 this round)"
        " rss_anon_mb=123.5 sock_throttled=441 (+0)"
    )
    # Perturbation: the reading reaches the line (not read into a local and
    # dropped, #95).
    monkeypatch.setattr(streamd, "rss_anon_mb", lambda: 7.0)
    assert streamd.stats_line("", {"kalshi_trades": 2}, 150, "sock_throttled=441 (+0)") != line
    # An unmeasured sample is None, never 0.0 (#91).
    monkeypatch.setattr(streamd, "rss_anon_mb", lambda: None)
    shut = streamd.stats_line("shutdown; ", {"a": 1}, None, None)
    # The shutdown marker the e2e test and the journal grep look for stays
    # a prefix, and the shutdown publisher carries the same reading.
    assert shut.startswith("shutdown; stats {'a': 1}")
    assert shut.endswith(" rss_anon_mb=None")
    assert "flushed" not in shut and "sock_throttled" not in shut
    # One renderer: no second site may format `stats {` into a line.
    src = inspect.getsource(streamd)
    assert src.count("stats {stats}") == 1
    assert src.count('f"stats {') == 0 and src.count('f"shutdown; stats {') == 0
    assert src.count("stats_line(") == 3  # the def + the two publishers
