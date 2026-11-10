"""Wait, bounded, until the venues' hostnames resolve. A unit's ExecStartPre.

    python -m collector.netwait HOST [HOST ...] [--budget-s 600] [--every-s 5]

WHY (2026-10-05). Both daily sweeps carry `Persistent=true`, so a timer
slot missed while the box was down fires once at the next boot -- and at
the 09:01Z boot that catch-up started 4s after the kernel, before the
resolver was up. `collector.sweep` and `collector.poly_sweep` make their
first request with no retry, so both died on `NameResolutionError` in the
same second, and the timer stamp recorded the slot as TAKEN: the next
attempt was the following day's. `signals` and `tradepass`, started in the
same second, survived only because they retry per request.

The user manager has no `network-online.target` to order against (that is
a system-manager target), so the gate is a resolver probe. It does no HTTP:
whether the venue answers is the unit's own business, and a probe that
fetched would be a second, unledgered client of the API. On budget
exhaustion it exits 1 and the unit fails LOUDLY -- a gate that gave up
quietly and let the run proceed would just move the same traceback down a
line.
"""

from __future__ import annotations

import argparse
import socket
import sys
import time
from collections.abc import Callable
from typing import TypeVar

import requests

T = TypeVar("T")


def _resolves(host: str) -> bool:
    try:
        socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        return False
    return True


def wait_for(
    hosts: list[str],
    budget_s: float,
    every_s: float,
    resolves: Callable[[str], bool] = _resolves,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[list[str], float]:
    """Poll until every host resolves or the budget is spent.

    Returns (hosts still unresolved, seconds waited). Empty list = ready."""
    t0 = clock()
    pending = list(hosts)
    while True:
        pending = [h for h in pending if not resolves(h)]
        waited = clock() - t0
        if not pending or waited + every_s > budget_s:
            return pending, waited
        sleep(every_s)


def _reachable(host: str, timeout_s: float = 5.0) -> bool:
    """A TCP handshake on 443, closed at once. Stronger than `_resolves` (the
    first four minutes of the 2026-11-09 drop were connect timeouts with the
    name still resolving) and still no HTTP: nothing the venue rate-limits."""
    try:
        socket.create_connection((host, 443), timeout=timeout_s).close()
    except OSError:
        return False
    return True


#: The failures an unreachable venue raises through the clients' own retry
#: ladders. `HTTPError` is deliberately absent: the venue ANSWERED.
TRANSPORT_ERRORS = (requests.ConnectionError, requests.Timeout)


class OutagePause:
    """Mid-run twin of the boot gate: stop walking while the venue is gone.

    WHY (2026-11-09, mistakes #172). A ~13-minute router/DNS drop at 10:18Z
    landed inside both daily sweeps. Each client's transport ladder is
    seconds long, so each unit of work failed fast and the loop took the
    NEXT one into the same dead network: the kalshi sweep burned 21 series
    in 5m19s (KXETH after 2,601 s of fetching, KXETHD untouched -- 14,307
    markets deferred, and the 11-10 catch-up hit both series' budgets and
    ran ~1.5h long), the poly sweep 83 markets, each of which lost its
    prices and trade tail for the day. The outage cost 13 minutes of wall;
    walking through it cost a day.

    `run(call)` executes `call`; on a transport error it probes the hosts,
    and only if one is UNREACHABLE (an outage witnessed, not a venue
    hiccup) waits -- bounded per pause and across the run -- and then
    retries the call ONCE. Everything else re-raises unchanged, so the
    callers' error accounting and consecutive-error breakers still see
    every failure that is not an outage that healed. Once the run budget
    is spent it stops pausing: a venue gone for the day must still reach
    the breaker rather than sleep at every unit of work.
    """

    def __init__(
        self,
        hosts: list[str],
        *,
        tag: str,
        per_pause_s: float = 900.0,
        total_s: float = 2700.0,
        every_s: float = 10.0,
        reachable: Callable[[str], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.hosts, self.tag = list(hosts), tag
        self.per_pause_s, self.total_s, self.every_s = per_pause_s, total_s, every_s
        # Resolved per probe, so the test suite's autouse stub (conftest) holds.
        self._reachable = reachable or (lambda h: _reachable(h))
        self._clock, self._sleep = clock, sleep
        self.n = 0  # pauses entered (a host was unreachable)
        self.healed = 0  # pauses that ended with every host back
        self.recovered = 0  # retries that then succeeded
        self.waited_s = 0.0

    def run(self, call: Callable[[], T], what: str = "") -> T:
        try:
            return call()
        except TRANSPORT_ERRORS as exc:
            if not self._pause(exc, what):
                raise
        out = call()
        self.recovered += 1
        return out

    def _pause(self, exc: BaseException, what: str) -> bool:
        """True when an outage was witnessed and every host came back."""
        down = [h for h in self.hosts if not self._reachable(h)]
        if not down:
            return False
        left = self.total_s - self.waited_s
        if left < self.every_s:
            return False
        self.n += 1
        print(
            f"[{self.tag}] OUTAGE PAUSE {self.n}: {type(exc).__name__} at {what},"
            f" unreachable {down}; waiting up to {min(self.per_pause_s, left):.0f}s",
            flush=True,
        )
        pending, waited = wait_for(
            down,
            min(self.per_pause_s, left),
            self.every_s,
            self._reachable,
            self._clock,
            self._sleep,
        )
        self.waited_s += waited
        if pending:
            print(
                f"[{self.tag}] OUTAGE PAUSE {self.n} gave up after {waited:.0f}s:"
                f" still unreachable {pending}; {what} fails as before",
                flush=True,
            )
            return False
        self.healed += 1
        print(
            f"[{self.tag}] OUTAGE PAUSE {self.n} over after {waited:.0f}s"
            f" ({self.waited_s:.0f}s of {self.total_s:.0f}s run budget); retrying {what} once",
            flush=True,
        )
        return True

    def block(self) -> dict:
        """The done-line reading. All zeros is a run that met no outage."""
        return {
            "n": self.n,
            "healed": self.healed,
            "recovered": self.recovered,
            "waited_s": round(self.waited_s, 1),
        }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("hosts", nargs="+")
    ap.add_argument("--budget-s", type=float, default=600.0)
    ap.add_argument("--every-s", type=float, default=5.0)
    args = ap.parse_args(argv)
    pending, waited = wait_for(args.hosts, args.budget_s, args.every_s)
    if pending:
        print(f"[netwait] UNRESOLVED after {waited:.1f}s: {pending}", flush=True)
        return 1
    # Printed even at 0.0s: the boot catch-up's wait is the number this exists
    # to show, and a silent pass cannot be told from a gate that never ran.
    print(f"[netwait] {len(args.hosts)} host(s) resolved after {waited:.1f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
