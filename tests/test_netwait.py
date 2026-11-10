"""The boot catch-up gate (2026-10-05): a Persistent= sweep that fires before
the resolver is up must wait for it, bounded, instead of spending the day's slot
on a NameResolutionError."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

import pytest
import requests

from collector import netwait
from collector.venues import kalshi, polymarket

_REAL_REACHABLE = netwait._reachable  # conftest stubs the module attribute per test
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


# --- the mid-run outage pause (2026-11-10, mistakes #172) --------------------


def _pause(clock: FakeClock, up_at: float, **kw) -> netwait.OutagePause:
    return netwait.OutagePause(
        ["v"],
        tag="t",
        reachable=lambda h: clock.t >= up_at,
        clock=clock.now,
        sleep=clock.sleep,
        **kw,
    )


class Flaky:
    """Raises `exc` for the first `fails` calls, then returns 'ok'."""

    def __init__(self, exc: Exception, fails: int = 1) -> None:
        self.exc, self.fails, self.calls = exc, fails, 0

    def __call__(self) -> str:
        self.calls += 1
        if self.calls <= self.fails:
            raise self.exc
        return "ok"


def test_an_outage_is_waited_out_and_the_work_retried_once() -> None:
    clock = FakeClock()
    p = _pause(clock, up_at=780)  # the 11-09 drop: ~13 min
    call = Flaky(requests.ConnectionError("dns"))
    assert p.run(call, "KXETH") == "ok"
    assert call.calls == 2
    assert p.block() == {"n": 1, "healed": 1, "recovered": 1, "waited_s": 780.0}


def test_a_transport_error_with_the_venue_reachable_is_not_an_outage() -> None:
    """The venue's own hiccup keeps its old accounting: no wait, no retry."""
    clock = FakeClock()
    p = _pause(clock, up_at=0)
    call = Flaky(requests.ReadTimeout("slow"))
    with pytest.raises(requests.ReadTimeout):
        p.run(call, "x")
    assert call.calls == 1 and clock.sleeps == []
    assert p.block() == {"n": 0, "healed": 0, "recovered": 0, "waited_s": 0.0}


def test_an_answer_from_the_venue_never_pauses() -> None:
    clock = FakeClock()
    p = _pause(clock, up_at=10**9)  # unreachable forever: must not even be probed into a wait
    call = Flaky(requests.HTTPError("500"))
    with pytest.raises(requests.HTTPError):
        p.run(call, "x")
    assert call.calls == 1 and clock.sleeps == [] and p.n == 0


def test_a_pause_that_outlasts_its_budget_fails_the_work_as_before() -> None:
    clock = FakeClock()
    p = _pause(clock, up_at=10**9, per_pause_s=60, total_s=100)
    with pytest.raises(requests.ConnectionError):
        p.run(Flaky(requests.ConnectionError("x")), "a")
    assert p.block() == {"n": 1, "healed": 0, "recovered": 0, "waited_s": 60.0}
    # The run budget bounds the sum: the second pause gets only what is left,
    # and once it is spent the loop is back to failing fast (the callers'
    # consecutive-error breaker must still be reachable on a day-long outage).
    with pytest.raises(requests.ConnectionError):
        p.run(Flaky(requests.ConnectionError("x")), "b")
    assert p.waited_s == 100.0 and p.n == 2
    before = len(clock.sleeps)
    with pytest.raises(requests.ConnectionError):
        p.run(Flaky(requests.ConnectionError("x")), "c")
    assert len(clock.sleeps) == before and p.n == 2


def test_a_retry_that_fails_again_raises_and_is_not_counted_recovered() -> None:
    clock = FakeClock()
    p = _pause(clock, up_at=30)
    call = Flaky(requests.ConnectionError("flap"), fails=2)
    with pytest.raises(requests.ConnectionError):
        p.run(call, "x")
    assert call.calls == 2
    assert p.block() == {"n": 1, "healed": 1, "recovered": 0, "waited_s": 30.0}


def test_an_unreachable_name_is_unreachable() -> None:
    assert not _REAL_REACHABLE("hyxlab-netwait.invalid", timeout_s=1.0)


def test_both_sweeps_route_their_unit_of_work_through_the_pause() -> None:
    """And probe the hosts their per-unit fetches call, from the clients' own
    base URLs. Each done line carries the block, so a run that met an outage
    says so without a journal scrape."""
    from collector import poly_sweep, sweep

    for mod in (sweep, poly_sweep):
        src = Path(mod.__file__).read_text()
        assert "pause.run(" in src and 'totals["net_pauses"] = pause.block()' in src, mod
    assert "urlparse(kalshi.BASE).hostname" in Path(sweep.__file__).read_text()
    assert "(poly.CLOB, poly.DATA_API)" in Path(poly_sweep.__file__).read_text()
