"""Process memory, measured with an instrument that can see the engine.

EXP-1381. `tracemalloc` sees the CPython heap and nothing else. A replay
process is mostly not the CPython heap: DuckDB's buffer manager, its
extension images, and the allocator's retained arenas all live outside
it. Rungs 12-15 of the memory ladder bounded heap terms with tracemalloc
and then quoted a PROCESS figure taken in the same run — and the process
figure was substantially the profiler.

**MEASURED 2026-08-28**, one `simulator.run_l2` (`hylshi_fade`,
2026-08-27 12:00-15:00Z, live stream archive, 293,568 snapshots, the
identical answer every time), peak RSS by a 50 ms sampler:

    tracemalloc off        409.0 MiB     <- the run
    tracemalloc.start()    590.9 MiB     +181  (+44%)
    tracemalloc.start(25)  638.7 MiB     +230  (+56%)

So the ladder's standing "the PROCESS is 1,025.9 MiB peak RSS" was never
a property of the replay. The honest 409.0 MiB attributes completely,
by phase, with no unexplained term:

    69.6  interpreter + imports
   +60.8  attaching the stream archive (engine image, before any query)
   +72.5  the DISTINCT market_id scan  (engine reports 46.5, retained)
  +120.1  `store.markets()` for 614 ids (engine 99.2, freed at close)
    +8.6  seed replay
  +115.6  trading replay              (engine 55.5 at the end)

DUCKDB UNDER-REPORTS ITSELF, WHICH IS WHY RSS IS THE INSTRUMENT.
Closing the stream connection returned **189.4 MiB** while
`duckdb_memory()` claimed 55.5 MiB was held. A figure that comes from
asking the engine what it holds is a lower bound on what the cgroup
will charge; only RSS is the number the kernel kills against.

AND A PEAK IS NOT AN ENDPOINT. The trading phase peaks 18.8 MiB above
where it ends, so a before/after pair of `rss_bytes()` reads under-
reports it. `PeakRss` samples instead.

WHY `process_peak` REFUSES BY DEFAULT. The taint above is not a mistake
anyone repeats knowingly — it is what happens when one script grows an
attribution pass and a headline number, and the attribution pass needs
tracemalloc on. Refusing unless the caller writes `allow_tracing=True`
puts the claim where the cost is. This is a measuring instrument, not
hardening bolted onto a connect: unlike `private_spill`, raising here
loses nothing but a wrong number.
"""

from __future__ import annotations

import os
import threading
import tracemalloc
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "MeasurementTainted",
    "MemoryReading",
    "PeakRss",
    "process_peak",
    "rss_bytes",
    "vm_hwm_bytes",
]

MIB = 1024 * 1024

#: Sampler period. The measured run peaks 18.8 MiB above its endpoint
#: over a ~50 s trading phase, so anything near a second is an endpoint
#: read with extra steps.
SAMPLE_INTERVAL = 0.05

_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")


class MeasurementTainted(RuntimeError):
    """A process figure was requested from an instrumented process."""


def rss_bytes() -> int:
    """This process's resident set, now.

    From `statm` rather than `status`: it is one short line, so sampling
    it at 20 Hz costs nothing measurable against the run being measured.
    """
    return int(Path("/proc/self/statm").read_text().split()[1]) * _PAGE_SIZE


def vm_hwm_bytes() -> int:
    """The kernel's own high-water mark for this process's RSS.

    Independent of any sampler — it cannot miss a spike between samples,
    but it also cannot be attributed to a phase, so the two are reported
    together rather than one instead of the other.
    """
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmHWM:"):
            return int(line.split()[1]) * 1024
    raise OSError("no VmHWM in /proc/self/status")  # pragma: no cover


class PeakRss:
    """Context manager sampling RSS on a thread; `.peak` in bytes.

    A daemon thread so an exception in the measured work cannot leave the
    process alive on the sampler's account.
    """

    def __init__(self, interval: float = SAMPLE_INTERVAL) -> None:
        self.interval = interval
        self.peak = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def sample(self) -> int:
        self.peak = max(self.peak, rss_bytes())
        return self.peak

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self.sample()

    def __enter__(self) -> PeakRss:
        self.sample()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self.sample()


@dataclass(frozen=True)
class MemoryReading:
    """A process figure and the heap figure, never one as the other."""

    rss: int
    vm_hwm: int
    sampled_peak: int | None
    traced_peak: int | None
    tracing: bool

    @property
    def peak(self) -> int:
        """The best available process peak: the kernel's, at worst."""
        return max(self.vm_hwm, self.sampled_peak or 0)

    def summary(self) -> str:
        parts = [
            f"peak_rss={self.peak / MIB:.1f} MiB",
            f"rss={self.rss / MIB:.1f} MiB",
            f"vm_hwm={self.vm_hwm / MIB:.1f} MiB",
        ]
        if self.sampled_peak is not None:
            parts.append(f"sampled_peak={self.sampled_peak / MIB:.1f} MiB")
        parts.append(
            f"traced_heap_peak={self.traced_peak / MIB:.1f} MiB"
            if self.traced_peak is not None
            else "traced_heap_peak=n/a"
        )
        if self.tracing:
            parts.append("TAINTED(tracemalloc on: +44%/+56% measured, EXP-1381)")
        return " ".join(parts)


def process_peak(sampler: PeakRss | None = None, *, allow_tracing: bool = False) -> MemoryReading:
    """Process memory, refusing to answer from a profiled process.

    `allow_tracing=True` returns the reading anyway, flagged — an
    attribution pass legitimately runs both instruments at once, it just
    may not publish the process number as the run's.
    """
    tracing = tracemalloc.is_tracing()
    if tracing and not allow_tracing:
        raise MeasurementTainted(
            "tracemalloc is tracing: RSS here includes the profiler "
            "(+181 MiB / +230 MiB on a measured 409.0 MiB replay, EXP-1381). "
            "Stop tracing for a process figure, or pass allow_tracing=True "
            "and report it as instrumented."
        )
    return MemoryReading(
        rss=rss_bytes(),
        vm_hwm=vm_hwm_bytes(),
        sampled_peak=sampler.peak if sampler is not None else None,
        traced_peak=tracemalloc.get_traced_memory()[1] if tracing else None,
        tracing=tracing,
    )
