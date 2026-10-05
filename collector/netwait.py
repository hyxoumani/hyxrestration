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
