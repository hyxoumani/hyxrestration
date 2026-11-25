"""HOSTNET: the host's uplink, which every capture unit shares (2026-11-20).

`enp7s0: Link is Down` at 01:59:35Z took the box off the internet for 11.7
minutes, until a WiFi network was joined by hand. The digest printed the
dead air and two cleared breadth failures with no cause, and nothing said
the default route had moved to WiFi. The fixtures are the journal's own
lines from that night and from the 11-18 reboot.
"""

from __future__ import annotations

from datetime import UTC, datetime

from collector import health

NM_LINES = """\
2026-11-18T03:53:03+00:00 hyz NetworkManager[699]: <info>  [1794973983.1] manager: NetworkManager state is now CONNECTED_SITE
-- Boot 5f0c1d2e3a4b4c5d8e9f0a1b2c3d4e5f --
2026-11-18T03:54:38+00:00 hyz NetworkManager[699]: <info>  [1794974078.1] manager: NetworkManager state is now CONNECTING
2026-11-18T03:54:45+00:00 hyz NetworkManager[699]: <info>  [1794974085.1] manager: NetworkManager state is now CONNECTED_SITE
2026-11-18T03:54:45+00:00 hyz NetworkManager[699]: <info>  [1794974085.2] manager: NetworkManager state is now CONNECTED_GLOBAL
2026-11-20T01:58:12+00:00 hyz NetworkManager[699]: <info>  [1795139892.6930] device (wlp6s0): set-hw-addr: set MAC address to 3A:1B:DC:80:30:31 (scanning)
2026-11-20T01:59:41+00:00 hyz NetworkManager[699]: <info>  [1795139981.4763] manager: NetworkManager state is now CONNECTED_LOCAL
2026-11-20T02:10:47+00:00 hyz NetworkManager[699]: <info>  [1795140647.1774] manager: NetworkManager state is now CONNECTING
2026-11-20T02:11:22+00:00 hyz NetworkManager[699]: <info>  [1795140682.8] manager: NetworkManager state is now CONNECTED_SITE
2026-11-20T02:11:23+00:00 hyz NetworkManager[699]: <info>  [1795140683.1] manager: NetworkManager state is now CONNECTED_GLOBAL
"""

KERNEL_LINES = """\
2026-11-18T03:54:36+00:00 hyz kernel: r8169 0000:07:00.0 enp7s0: Link is Down
2026-11-18T03:54:38+00:00 hyz kernel: r8169 0000:07:00.0 enp7s0: Link is Up - 1Gbps/Full - flow control rx/tx
2026-11-20T01:59:35+00:00 hyz kernel: r8169 0000:07:00.0 enp7s0: Link is Down
2026-11-20T02:00:01+00:00 hyz kernel: audit: type=1100 something else entirely
"""

ROUTE = (
    "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
    "wlp6s0\t00000000\tFE01A8C0\t0003\t0\t0\t600\t00000000\t0\t0\t0\n"
    "enp7s0\t00000000\tFE01A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
    "cilium_host\t0000000A\t2B00000A\t0003\t0\t0\t0\t00FFFFFF\t0\t0\t0\n"
)


def _ts(s: str) -> float:
    return datetime.fromisoformat(s).replace(tzinfo=UTC).timestamp()


NOW = _ts("2026-11-20T02:15:00")


def _hn(**kw) -> health.HostNet:
    base = dict(
        states=health.parse_nm_states(NM_LINES),
        links=health.parse_link_events(KERNEL_LINES),
        route_iface="wlp6s0",
        route_read=True,
        route_wifi=True,
    )
    base.update(kw)
    return health.HostNet(**base)


def test_only_state_transitions_are_parsed_and_a_boot_marks_the_next_one() -> None:
    states = health.parse_nm_states(NM_LINES)
    assert [s for _, s, _ in states] == [
        "CONNECTED_SITE",
        "CONNECTING",
        "CONNECTED_SITE",
        "CONNECTED_GLOBAL",
        "CONNECTED_LOCAL",
        "CONNECTING",
        "CONNECTED_SITE",
        "CONNECTED_GLOBAL",
    ]
    assert [b for _, _, b in states] == [False, True] + [False] * 6


def test_an_empty_journal_read_is_no_transition() -> None:
    # What journalctl prints to STDOUT when it found nothing (mistakes #99).
    assert health.parse_nm_states("-- No entries --\n") == []
    assert health.net_episodes([]) == []


def test_the_link_loss_is_one_episode_from_the_first_non_online_state_to_global() -> None:
    eps = health.net_episodes(health.parse_nm_states(NM_LINES))
    assert eps[1] == health.NetEpisode(
        _ts("2026-11-20T01:59:41"), _ts("2026-11-20T02:11:23"), "CONNECTED_LOCAL", False
    )


def test_a_span_holding_a_boot_boundary_is_marked_a_reboot() -> None:
    eps = health.net_episodes(health.parse_nm_states(NM_LINES))
    assert len(eps) == 2 and eps[0].reboot and eps[0].state == "CONNECTED_SITE"


def test_a_window_opening_on_global_is_the_end_of_an_episode_it_does_not_hold() -> None:
    eps = health.net_episodes([(100.0, "CONNECTED_GLOBAL", False)])
    assert eps == [health.NetEpisode(None, 100.0, "state before the window unread", False)]


def test_an_episode_with_no_global_after_it_is_still_open() -> None:
    eps = health.net_episodes([(100.0, "CONNECTED_LOCAL", False), (200.0, "CONNECTING", False)])
    assert eps == [health.NetEpisode(100.0, None, "CONNECTED_LOCAL", False)]


def test_kernel_link_lines_are_parsed_with_interface_and_direction() -> None:
    evs = health.parse_link_events(KERNEL_LINES)
    assert [(e.iface, e.up) for e in evs] == [
        ("enp7s0", False),
        ("enp7s0", True),
        ("enp7s0", False),
    ]


def test_the_default_route_is_the_lowest_metric_one_and_none_when_absent() -> None:
    assert health.parse_default_route(ROUTE) == "enp7s0"
    assert health.parse_default_route(ROUTE.replace("\t100\t", "\t700\t")) == "wlp6s0"
    assert health.parse_default_route(ROUTE.splitlines()[0] + "\n") is None


def test_the_line_names_the_episode_the_dead_link_and_the_wifi_route() -> None:
    line = health.host_net_line(_hn(), NOW, window_h=72)
    assert line == (
        "72h: 2 offline episode(s), 13.4 min: "
        "11-18 03:53:03Z..11-18 03:54:45Z (1.7 min, across a REBOOT, CONNECTED_SITE); "
        "11-20 01:59:41Z..11-20 02:11:23Z (11.7 min, CONNECTED_LOCAL)"
        " | link: enp7s0 2 link-down(s), DOWN since 11-20 01:59:35Z"
        " | default route via wlp6s0 (WIFI)"
    )


def test_every_term_of_the_reading_moves_the_line() -> None:
    base = health.host_net_line(_hn(), NOW)
    variants = [
        _hn(states=None),
        _hn(states=[]),
        _hn(states=health.parse_nm_states(NM_LINES)[:-1]),
        _hn(links=None),
        _hn(links=[]),
        _hn(links=health.parse_link_events(KERNEL_LINES)[:2]),
        _hn(route_iface="enp7s0"),
        _hn(route_iface=None),
        _hn(route_read=False),
        _hn(route_wifi=False),
        _hn(route_wifi=None),
    ]
    lines = [health.host_net_line(v, NOW) for v in variants]
    assert base not in lines and len(set(lines)) == len(lines)


def test_unread_is_never_rendered_as_quiet() -> None:
    line = health.host_net_line(_hn(states=None, links=None, route_read=False), NOW)
    assert line == (
        "24h: NetworkManager journal UNREAD (no claim about the uplink)"
        " | link: kernel journal UNREAD | default route unread"
    )
    quiet = health.host_net_line(_hn(states=[], links=[], route_wifi=False), NOW)
    assert (
        quiet
        == "24h: 0 offline episode(s) | link: no link event | default route via wlp6s0 (wired)"
    )


def test_open_and_pre_window_episodes_are_printed_as_bounds() -> None:
    still = health.host_net_line(_hn(states=health.parse_nm_states(NM_LINES)[4:5]), NOW)
    assert "11-20 01:59:41Z.. STILL OFFLINE (15.3 min so far, CONNECTED_LOCAL)" in still
    before = health.host_net_line(_hn(states=health.parse_nm_states(NM_LINES)[-1:]), NOW)
    assert "..11-20 02:11:23Z (>= 1436.4 min, began before the window)" in before


def test_the_live_reader_never_raises_and_the_digest_prints_the_line(capsys, monkeypatch) -> None:
    real = health.read_hostnet(sys_net="/nonexistent")
    assert real.route_read  # /proc/net/route exists on every Linux box
    monkeypatch.setattr(health, "read_hostnet", lambda: _hn())
    monkeypatch.setattr(health, "read_kernel_oom_window", lambda: None)
    health.main()
    out = capsys.readouterr().out
    assert "HOSTNET    (host)" in out and "default route via wlp6s0 (WIFI)" in out


# --- roams (mistakes #221): the journal's own lines from 11-20 16:25-16:33Z.
ROAM_KERNEL = """\
2026-11-20T16:25:37+00:00 hyz kernel: wlp6s0: disconnect from AP a8:40:f8:6f:f5:9c for new auth to a8:40:f8:6f:f5:98
2026-11-20T16:25:37+00:00 hyz kernel: wlp6s0: authenticate with a8:40:f8:6f:f5:98 (local address=dc:56:7b:84:35:87)
2026-11-20T16:26:22+00:00 hyz kernel: wlp6s0: deauthenticating from a8:40:f8:6f:f5:98 by local choice (Reason: 3=DEAUTH_LEAVING)
2026-11-20T16:31:42+00:00 hyz kernel: wlp6s0: disconnect from AP a8:40:f8:6f:f5:98 for new auth to a8:40:f8:6f:f5:9c
2026-11-20T16:32:26+00:00 hyz kernel: wlp6s0: disconnect from AP a8:40:f8:6f:f5:9c for new auth to a8:40:f8:6f:f5:98
"""
ROAM_NM = """\
2026-11-20T16:26:22+00:00 hyz NetworkManager[699]: <info>  [1795191982.1] manager: NetworkManager state is now CONNECTED_LOCAL
2026-11-20T16:26:37+00:00 hyz NetworkManager[699]: <info>  [1795191997.0] manager: NetworkManager state is now CONNECTED_GLOBAL
"""
ROAM_NOW = _ts("2026-11-20T16:40:00")


def _roam_hn(**kw) -> health.HostNet:
    base = dict(
        states=health.parse_nm_states(ROAM_NM),
        links=[],
        roams=health.parse_roam_events(ROAM_KERNEL),
    )
    base.update(kw)
    return _hn(**base)


def test_roam_lines_are_parsed_and_the_local_deauth_is_not_one() -> None:
    roams = health.parse_roam_events(ROAM_KERNEL)
    assert [(r.iface, r.dst[-2:]) for r in roams] == [
        ("wlp6s0", "98"),
        ("wlp6s0", "9c"),
        ("wlp6s0", "98"),
    ]
    assert roams[0].ts == _ts("2026-11-20T16:25:37")
    assert health.parse_roam_events("-- No entries --\n") == []


def test_an_episode_is_charged_from_the_roam_that_led_it() -> None:
    line = health.host_net_line(_roam_hn(), ROAM_NOW)
    # NM saw 15 s; the path had no lease from the roam 45 s before that.
    assert "no path from a roam 45 s earlier = 1.0 min" in line
    assert "0.2 min by NetworkManager's state, 1.0 min from the roams" in line
    assert (
        "3 wifi roam(s), 1 followed by an offline episode "
        "(1 of 2 onto a8:40:f8:6f:f5:98, 0 of 1 onto a8:40:f8:6f:f5:9c)"
    ) in line


def test_a_roam_outside_the_lead_or_after_the_start_charges_nothing() -> None:
    start = _ts("2026-11-20T16:26:22")
    mk = lambda dt: [health.RoamEvent(start + dt, "wlp6s0", "a", "b")]  # noqa: E731
    assert health.roam_before(mk(-health.ROAM_LEAD_S - 1), start) is None
    assert health.roam_before(mk(1), start) is None
    assert health.roam_before(mk(-health.ROAM_LEAD_S), start) is not None
    quiet = health.host_net_line(_roam_hn(roams=mk(-600)), ROAM_NOW)
    assert "no path from a roam" not in quiet and "from the roams" not in quiet
    assert "1 wifi roam(s), 0 followed by an offline episode (0 of 1 onto b)" in quiet


def test_no_roam_leaves_the_line_as_it_was() -> None:
    assert "roam" not in health.host_net_line(_hn(), NOW)
    assert "roam" not in health.host_net_line(_hn(roams=[]), NOW)


def test_the_no_path_spans_open_at_the_roam_and_close_at_the_new_lease() -> None:
    spans = health.net_dead_spans(_roam_hn(), ROAM_NOW)
    assert spans == [(_ts("2026-11-20T16:25:37"), _ts("2026-11-20T16:26:37"))]
    # Without the roam the span is NetworkManager's own.
    assert health.net_dead_spans(_roam_hn(roams=[]), ROAM_NOW) == [
        (_ts("2026-11-20T16:26:22"), _ts("2026-11-20T16:26:37"))
    ]


def test_an_unread_manager_journal_is_no_cross_reading() -> None:
    assert health._flow_net(_hn(states=None), NOW) is None
    assert health._flow_net(_hn(), NOW) == health.net_dead_spans(_hn(), NOW)


# --- probe episodes (2026-11-25): NM's own lines from the wired route, where
# seven CONNECTED_SITE spans in 36 h sat inside periods the stream received
# straight through (`rx_gap` 0.28-0.81 s).
PROBE_NM = """\
2026-11-24T17:22:08+00:00 hyz NetworkManager[699]: <info>  [1795540928.1] manager: NetworkManager state is now CONNECTED_SITE
2026-11-24T17:22:59+00:00 hyz NetworkManager[699]: <info>  [1795540979.1] manager: NetworkManager state is now CONNECTED_GLOBAL
2026-11-24T23:53:45+00:00 hyz NetworkManager[699]: <info>  [1795564425.1] manager: NetworkManager state is now CONNECTED_SITE
2026-11-24T23:54:12+00:00 hyz NetworkManager[699]: <info>  [1795564452.1] manager: NetworkManager state is now CONNECTED_GLOBAL
"""
PROBE_NOW = _ts("2026-11-25T02:15:00")
A1, B1 = _ts("2026-11-24T17:22:08"), _ts("2026-11-24T17:22:59")
A2, B2 = _ts("2026-11-24T23:53:45"), _ts("2026-11-24T23:54:12")
# The second episode straddles a stats line (23:53:59Z), as it did.
PROBE_RX = (
    (_ts("2026-11-24T17:18:47"), _ts("2026-11-24T17:23:58"), 0.30),
    (_ts("2026-11-24T23:48:46"), _ts("2026-11-24T23:53:59"), 0.38),
    (_ts("2026-11-24T23:53:59"), _ts("2026-11-24T23:59:12"), 0.81),
)


def _probe_hn(**kw) -> health.HostNet:
    base = dict(
        states=health.parse_nm_states(PROBE_NM), links=[], route_iface="enp7s0", route_wifi=False
    )
    base.update(kw)
    return _hn(**base)


def test_an_episode_the_stream_received_through_is_the_probe_not_the_path() -> None:
    line = health.host_net_line(_probe_hn(), PROBE_NOW, rx_periods=PROBE_RX)
    assert line.startswith(
        "24h: 2 offline episode(s), 1.3 min -- 2 of them (1.3 min) with the stream receiving "
        "throughout: NetworkManager's connectivity probe, not the path: "
        "11-24 17:22:08Z..11-24 17:22:59Z (0.8 min; stream received throughout, "
        "longest silence 0.30 s, CONNECTED_SITE); "
        "11-24 23:53:45Z..11-24 23:54:12Z (0.5 min; stream received throughout, "
        "longest silence 0.81 s, CONNECTED_SITE)"
    )
    assert health.net_dead_spans(_probe_hn(), PROBE_NOW, rx_periods=PROBE_RX) == []
    assert health._flow_net(_probe_hn(), PROBE_NOW, PROBE_RX) == []


def test_without_the_streams_series_the_line_and_the_spans_are_networkmanagers() -> None:
    for rx in (None, ()):
        line = health.host_net_line(_probe_hn(), PROBE_NOW, rx_periods=rx)
        assert "stream receiv" not in line and "2 offline episode(s), 1.3 min: " in line
        assert health.net_dead_spans(_probe_hn(), PROBE_NOW, rx_periods=rx) == [(A1, B1), (A2, B2)]


def test_a_silence_at_the_bar_or_as_long_as_the_episode_proves_nothing() -> None:
    gap = health.episode_alive_gap
    per = lambda g: ((A1 - 200, B1 + 60, g),)  # noqa: E731
    assert gap(per(1.99), A1, B1) == 1.99
    assert gap(per(health.HOSTNET_ALIVE_GAP_S), A1, B1) is None
    assert gap(per(11.08), A1, B1) is None  # the 19:52Z gateway blink's reading
    # A 1.5 s episode beside a 1.5 s silence may be the same 1.5 s.
    assert gap(per(1.5), A1, A1 + 1.5) is None and gap(per(1.4), A1, A1 + 1.5) == 1.4


def test_a_span_not_wholly_inside_measured_periods_proves_nothing() -> None:
    gap = health.episode_alive_gap
    assert gap(((A1 + 1, B1 + 60, 0.3),), A1, B1) is None  # opens after the start
    assert gap(((A1 - 200, B1 - 1, 0.3),), A1, B1) is None  # closes before the end
    hole = ((A2 - 200, A2 + 5, 0.3), (A2 + 6, B2 + 60, 0.3))
    assert gap(hole, A2, B2) is None  # a restart between two periods
    assert gap(((A2 - 200, A2 + 5, 0.3), (A2 + 5, B2 + 60, 0.3)), A2, B2) == 0.3
    assert gap(None, A1, B1) is None and gap((), A1, B1) is None


def test_a_roam_led_episode_is_read_from_the_roam_and_a_reboot_never_alive() -> None:
    # The stream was silent from the roam: a period covering only NM's 15 s
    # cannot clear it, and one covering the roam carries the silence.
    a, b = _ts("2026-11-20T16:26:22"), _ts("2026-11-20T16:26:37")
    short = ((a - 5, b + 60, 0.3),)
    assert "stream receiv" not in health.host_net_line(_roam_hn(), ROAM_NOW, rx_periods=short)
    assert health.net_dead_spans(_roam_hn(), ROAM_NOW, rx_periods=short) == [(a - 45, b)]
    wide = ((a - 200, b + 60, 0.3),)
    assert "stream receiv" in health.host_net_line(_roam_hn(), ROAM_NOW, rx_periods=wide)
    reboot = ((_ts("2026-11-18T03:50:00"), _ts("2026-11-18T03:56:00"), 0.3),)
    line = health.host_net_line(_hn(), NOW, window_h=72, rx_periods=reboot)
    assert "across a REBOOT" in line and "stream receiv" not in line


def test_an_open_episode_is_never_called_alive() -> None:
    hn = _probe_hn(states=health.parse_nm_states(PROBE_NM)[:1])
    rx = ((A1 - 200, PROBE_NOW + 1, 0.3),)
    line = health.host_net_line(hn, PROBE_NOW, rx_periods=rx)
    assert "STILL OFFLINE" in line and "stream receiv" not in line
    assert health.net_dead_spans(hn, PROBE_NOW, rx_periods=rx) == [(A1, PROBE_NOW)]


def test_the_digest_hands_one_stream_reading_to_hostnet_streamflow_and_streamrx() -> None:
    import inspect

    src = inspect.getsource(health.main)
    assert src.count("read_stream_rx()") == 1
    assert "host_net_line(hostnet, now.timestamp(), rx_periods=rx_periods)" in src
    assert "_flow_net(hostnet, now.timestamp(), rx_periods)" in src
    assert health.rx_periods_of("unreadable") is None and health.rx_periods_of(None) is None
