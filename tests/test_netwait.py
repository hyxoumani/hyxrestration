"""The boot catch-up gate (2026-10-05): a Persistent= sweep that fires before
the resolver is up must wait for it, bounded, instead of spending the day's slot
on a NameResolutionError."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from collector import netwait
from collector.venues import kalshi, polymarket

UNITS = Path(__file__).resolve().parent.parent / "scripts" / "systemd"


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def test_waits_until_the_resolver_comes_up() -> None:
    clock = FakeClock()
    pending, waited = netwait.wait_for(
        ["a", "b"], 600, 5, lambda h: clock.t >= 12, clock.now, clock.sleep
    )
    assert pending == [] and waited == 15.0 and clock.sleeps == [5, 5, 5]


def test_ready_hosts_cost_no_sleep() -> None:
    clock = FakeClock()
    assert netwait.wait_for(["a"], 600, 5, lambda h: True, clock.now, clock.sleep) == ([], 0.0)
    assert clock.sleeps == []


def test_the_budget_is_a_ceiling_and_names_what_never_resolved() -> None:
    clock = FakeClock()
    pending, waited = netwait.wait_for(
        ["up", "down"], 20, 5, lambda h: h == "up", clock.now, clock.sleep
    )
    assert pending == ["down"] and waited <= 20


def test_exhaustion_fails_the_unit(monkeypatch) -> None:
    monkeypatch.setattr(netwait, "_resolves", lambda h: False)
    monkeypatch.setattr(netwait.time, "sleep", lambda s: None)
    assert netwait.main(["nowhere.invalid", "--budget-s", "0"]) == 1


def test_a_real_unresolvable_name_is_unresolved() -> None:
    # .invalid is reserved (RFC 6761): it never resolves, online or not.
    assert not netwait._resolves("hyxlab-netwait.invalid")


def _gate_hosts(unit: str) -> tuple[list[str], int, int]:
    text = (UNITS / unit).read_text()
    lines = text.splitlines()
    pre = [i for i, ln in enumerate(lines) if ln.startswith("ExecStartPre=")]
    run = [i for i, ln in enumerate(lines) if ln.startswith("ExecStart=")]
    (i,) = pre
    (j,) = run
    m = re.search(r"-m collector\.netwait (.+)$", lines[i])
    assert m, f"{unit}: ExecStartPre is not the netwait gate"
    return m.group(1).split(), i, j


def test_the_sweeps_are_gated_on_the_hosts_their_clients_call() -> None:
    """Hosts are derived from the venue clients' own base URLs, so a client
    repointed at a new host cannot leave the gate probing the old one."""
    host = lambda url: urlparse(url).hostname  # noqa: E731
    cases = {
        "hyxlab-sweep.service": {host(kalshi.BASE)},
        "hyxlab-poly-sweep.service": {
            host(polymarket.GAMMA),
            host(polymarket.CLOB),
            host(polymarket.DATA_API),
        },
    }
    for unit, want in cases.items():
        hosts, _, _ = _gate_hosts(unit)
        assert set(hosts) == want, f"{unit} gates on {hosts}, its client calls {want}"
