"""The digest reads a RUNNING unit's cgroup memory counters live.

`scripts/cgmem.sh` publishes `memory.events` at STOP, which covers oneshots
and covers a daemon only when it dies. A healthy daemon never stops (shadow
was 209h up on 2026-10-14), so until this pass its verdict -- `oom_kill`, and
whether `peak == cap` is reclaim or footprint -- was reachable only by hand.
These arms pin: the parse, the words the ops rule requires, that only BUSY
units are read, that a vanished cgroup is not a fault, and that the line and
the ATTENTION headline reach stdout.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from collector import health

STAT = "anon 166502400\nfile 731754496\nkernel 10366976\n"
EVENTS = "low 0\nhigh 0\nmax 37742\noom 0\noom_kill 0\noom_group_kill 0\nsock_throttled 0\n"


def test_parse_reads_anon_peak_cap_and_events() -> None:
    c = health.parse_cgmem("u.service", STAT, EVENTS, "2147549184\n", "2147483648\n")
    assert c == health.CgMem("u.service", 166502400, 2147549184, 2147483648, 37742, 0, 0)


def test_an_uncapped_unit_reads_cap_none_not_zero() -> None:
    c = health.parse_cgmem("u.service", STAT, EVENTS, "298000000", "max\n")
    assert c.cap is None
    assert "uncapped" in health.judge_cgmem(c).detail


def test_a_missing_counter_is_none_never_zero() -> None:
    c = health.parse_cgmem("u.service", None, None, None, None)
    assert c.anon is None and c.peak is None and c.max_events is None and c.oom_kill is None
    v = health.judge_cgmem(c)
    assert v.state == "CGMEM"
    assert "oom_kill ?" in v.detail


def test_peak_at_cap_is_worded_as_reclaim_not_a_kill() -> None:
    """ops.md: `MemoryPeak == cap` on a healthy I/O-heavy unit is page-cache
    reclaim. The number alone reads as a footprint; the line must say which."""
    c = health.parse_cgmem("hyxlab-stream.service", STAT, EVENTS, "2147549184", "2147483648")
    v = health.judge_cgmem(c)
    assert v.state == "CGMEM"
    assert "== cap" in v.detail
    assert "37742 reclaim events, not kills" in v.detail
    assert "anon 158M" in v.detail


def test_peak_under_cap_says_the_cap_was_never_reached() -> None:
    c = health.parse_cgmem(
        "hyxlab-simui.service", "anon 28721152\n", "max 0\noom_kill 0\n", "36012032", "1073741824"
    )
    assert "of cap 1.0G (cap never reached)" in health.judge_cgmem(c).detail


def test_an_oom_kill_is_the_fault_and_only_that() -> None:
    killed = health.parse_cgmem(
        "u.service", STAT, EVENTS.replace("oom_kill 0", "oom_kill 2"), "1", "1"
    )
    assert health.judge_cgmem(killed).state == "OOM-KILLED"
    # 37,742 reclaim events with zero kills is a healthy unit, not an alarm.
    assert health.judge_cgmem(health.parse_cgmem("u", STAT, EVENTS, "1", "1")).state == "CGMEM"


def test_read_cgmem_reads_the_four_files_under_the_root(tmp_path: Path) -> None:
    d = tmp_path / "user.slice" / "x.service"
    d.mkdir(parents=True)
    (d / "memory.stat").write_text(STAT)
    (d / "memory.events").write_text(EVENTS)
    (d / "memory.peak").write_text("5\n")
    (d / "memory.max").write_text("9\n")
    c = health.read_cgmem("x.service", "/user.slice/x.service", root=tmp_path)
    assert c == health.CgMem("x.service", 166502400, 5, 9, 37742, 0, 0)


def test_a_cgroup_that_vanished_between_show_and_read_is_none(tmp_path: Path) -> None:
    """A oneshot can stop between `systemctl show` and the read; its
    `cgmem.sh` line already published the verdict. Not a fault, not a raise."""
    assert health.read_cgmem("x.service", "/user.slice/gone.service", root=tmp_path) is None
    assert health.read_cgmem("x.service", "", root=tmp_path) is None


def test_a_cgroup_missing_one_file_still_reads(tmp_path: Path) -> None:
    d = tmp_path / "x.service"
    d.mkdir()
    (d / "memory.events").write_text(EVENTS)
    c = health.read_cgmem("x.service", "/x.service", root=tmp_path)
    assert c is not None and c.peak is None and c.max_events == 37742


def test_control_group_is_a_field_the_digest_asks_for() -> None:
    """`show` requests exactly `PROPS`; a field read but not requested comes
    back empty and every running unit silently reads as having no cgroup."""
    assert "ControlGroup" in health.PROPS


def test_only_busy_units_are_read(monkeypatch: pytest.MonkeyPatch) -> None:
    props = {
        "a.service": {"ActiveState": "active", "ControlGroup": "/a"},
        "b.service": {"ActiveState": "inactive", "ControlGroup": ""},
        "c.service": {"ActiveState": "deactivating", "ControlGroup": "/c"},
    }
    read: list[tuple[str, str]] = []
    monkeypatch.setattr(health, "show", lambda u: props[u])
    monkeypatch.setattr(
        health,
        "read_cgmem",
        lambda u, cg, root=None: read.append((u, cg)) or health.CgMem(u, 1, 1, 1, 0, 0),
    )
    out = health.live_cgmem(list(props))
    assert read == [("a.service", "/a"), ("c.service", "/c")]
    assert [c.unit for c in out] == ["a.service", "c.service"]


def test_the_line_and_the_attention_headline_reach_stdout(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verdict computed and not printed is the defect the digest exists to
    answer; an oom_kill on a daemon must also reach the ATTENTION list, since
    `Restart=always` hides it from the unit's own state."""
    monkeypatch.setattr(health, "report", lambda now=None, failures=None: [])
    monkeypatch.setattr(health, "read_unit_failures", lambda units, hours=24: [])
    monkeypatch.setattr(health, "failure_history_lines", lambda *a, **k: [])
    monkeypatch.setattr(health, "drift_report", lambda: [])
    monkeypatch.setattr(health, "judge_qa", lambda *a, **k: "QA (stubbed)")
    monkeypatch.setattr(
        health,
        "live_cgmem",
        lambda units=None: [
            health.CgMem("hyxlab-stream.service", 160 * 2**20, 2**31, 2**31, 37742, 0),
            health.CgMem("hyxlab-shadow.service", 2**28, 2**30, 2**30, 986, 1),
        ],
    )
    health.main()
    out = capsys.readouterr().out
    assert "CGMEM      hyxlab-stream.service" in out
    assert "37742 reclaim events, not kills" in out
    assert "OOM-KILLED hyxlab-shadow.service" in out
    assert "ATTENTION: ['hyxlab-shadow.service']" in out


def test_sock_throttled_is_read_and_a_zero_is_a_measurement() -> None:
    """`memory.events` has one key that says reclaim had a COST: the kernel
    throttled the cgroup's sockets under memory pressure. `cgmem.sh` printed
    it at stop (441 on the stream's 10-10..10-14 run) and the live line
    dropped it, so the ops-rule wording -- reclaim, not kills -- stood for a
    run whose sockets were clamped 441 times."""
    c = health.parse_cgmem("hyxlab-stream.service", STAT, EVENTS, "2147549184", "2147483648")
    assert c.sock_throttled == 0
    assert health.judge_cgmem(c).detail.endswith("oom_kill 0, sock_throttled 0")


def test_a_nonzero_sock_throttled_is_worded_as_reclaim_with_a_cost() -> None:
    c = health.parse_cgmem(
        "hyxlab-stream.service",
        STAT,
        EVENTS.replace("sock_throttled 0", "sock_throttled 441"),
        "1",
        "1",
    )
    v = health.judge_cgmem(c)
    assert v.state == "CGMEM"  # cumulative over the run; the timed signal is streamd's own line
    assert "sock_throttled 441 (reclaim clamped its sockets)" in v.detail


def test_a_kernel_without_the_key_reads_unknown_not_zero() -> None:
    c = health.parse_cgmem("u", STAT, "max 0\noom_kill 0\n", "1", "2")
    assert c.sock_throttled is None
    assert "sock_throttled ?" in health.judge_cgmem(c).detail
