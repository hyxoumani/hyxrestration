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
        "stats {'kalshi_trades': 2} (flushed 150 this round)"
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
