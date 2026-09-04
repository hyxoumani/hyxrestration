"""What may be published as a PROCESS memory figure (EXP-1381).

Rungs 12-15 bounded heap terms with tracemalloc and quoted a process
peak from the same run. The quoted 1,025.9 MiB was substantially the
profiler: the same replay measures 409.0 MiB with tracemalloc off,
590.9 MiB at `start()` and 638.7 MiB at `start(25)`. These pin the three
properties that made that possible — a heap instrument is not a process
instrument, an endpoint read is not a peak, and the refusal is the part
that has to be explicit.
"""

import threading
import tracemalloc

import pytest

from hyxlab.memprobe import (
    MIB,
    MeasurementTainted,
    MemoryReading,
    PeakRss,
    process_peak,
    rss_bytes,
    vm_hwm_bytes,
)


def test_rss_is_the_process_not_the_heap():
    """`rss_bytes` reads the kernel's number, and it moves with real pages.

    An interpreter that has imported duckdb is already hundreds of MiB of
    RSS against a few MiB of heap, so any implementation that answered
    from `tracemalloc` or `sys.getsizeof` fails the floor alone.
    """
    before = rss_bytes()
    assert before > 8 * MIB
    ballast = bytearray(64 * MIB)
    ballast[::4096] = b"\x01" * (len(ballast) // 4096)  # fault the pages in
    after = rss_bytes()
    assert after - before >= 48 * MIB
    del ballast
    # VmHWM is maintained coarsely and lags `statm`, which is the second
    # reason `.peak` takes the max of the two rather than trusting the
    # kernel's mark alone. The slack is sized to the lag's TAIL, not its
    # typical value: the kernel refreshes `hiwater_rss` only at its own
    # checkpoints, so the lag is 0.00 MiB almost always (measured
    # 2026-09-04: 0.00 min/p50/max over 25 isolated samples, and no sample
    # above 0.5 MiB across all 1009 teardowns of a full suite run) and then
    # occasionally is not — the promote gate on 2026-09-04 saw 4.52 MiB on a
    # loaded box and failed a healthy tree. 16 MiB covers that observation
    # 3.5x over while staying two orders of magnitude below the defect this
    # line exists to catch: an implementation answering from a HEAP
    # instrument reads ~450 MiB low here, not ~5.
    assert vm_hwm_bytes() >= after - 16 * MIB


def test_peak_survives_what_an_endpoint_read_misses():
    """The measured replay peaks 18.8 MiB above where it ends.

    So the sampler must catch a transient that is gone by the time the
    work returns — the exact shape of a `fetchmany` batch or a query's
    hash table. A before/after pair of reads cannot, which is why this
    asserts against the ENDING rss and not merely against zero.
    """
    with PeakRss(interval=0.005) as p:
        ballast = bytearray(96 * MIB)
        ballast[::4096] = b"\x01" * (len(ballast) // 4096)
        transient = rss_bytes()
        # Hold it across real GIL releases. Allocate-touch-free is one
        # burst of C that never yields, so a sampler thread is not slow
        # here — it is unscheduled. A replay yields constantly (DuckDB
        # calls, file reads), which is the case being modelled.
        for _ in range(200):
            rss_bytes()
        del ballast
        for _ in range(200):
            rss_bytes()
    ended = rss_bytes()
    assert p.peak >= transient - 8 * MIB
    assert p.peak > ended


def test_process_peak_refuses_a_traced_process():
    """The mistake itself: a process figure taken while profiling.

    tracemalloc cost +44% (1 frame) and +56% (25 frames) of a 409.0 MiB
    replay, so a reading taken under it is not the run's. Refusing is the
    default; an attribution pass may opt in, and what it gets back says
    so.
    """
    tracemalloc.start()
    try:
        with pytest.raises(MeasurementTainted) as e:
            process_peak()
        assert "tracemalloc" in str(e.value)

        r = process_peak(allow_tracing=True)
        assert r.tracing is True
        assert "TAINTED" in r.summary()
        # The two instruments stay separate fields: the heap figure is
        # small and the process figure is not, and neither is the other.
        assert r.traced_peak is not None
        assert r.rss > r.traced_peak
    finally:
        tracemalloc.stop()


def test_untraced_reading_reports_both_and_conflates_neither():
    assert not tracemalloc.is_tracing()
    with PeakRss(interval=0.005) as p:
        ballast = bytearray(48 * MIB)
        ballast[::4096] = b"\x01" * (len(ballast) // 4096)
        del ballast
    r = process_peak(p)
    assert r.tracing is False
    assert r.traced_peak is None  # no heap number is invented from RSS
    assert r.sampled_peak == p.peak
    assert r.peak >= r.rss
    assert r.peak == max(r.vm_hwm, p.peak)
    s = r.summary()
    assert "peak_rss=" in s and "traced_heap_peak=n/a" in s and "TAINTED" not in s


def test_peak_prefers_the_kernel_when_the_sampler_missed_it():
    """`.peak` is the best available, not the sampler's.

    A sampler CAN miss a spike between two 50 ms reads; VmHWM cannot. A
    `.peak` that returned `sampled_peak` alone would under-report exactly
    the spike the ladder cares about.
    """
    r = MemoryReading(rss=100, vm_hwm=900, sampled_peak=200, traced_peak=None, tracing=False)
    assert r.peak == 900
    r2 = MemoryReading(rss=100, vm_hwm=300, sampled_peak=800, traced_peak=None, tracing=False)
    assert r2.peak == 800
    r3 = MemoryReading(rss=100, vm_hwm=300, sampled_peak=None, traced_peak=None, tracing=False)
    assert r3.peak == 300


def test_sampler_thread_does_not_outlive_the_measurement():
    """The instrument may not keep the process alive or keep sampling.

    A non-daemon thread, or one never stopped, turns a measuring helper
    into a leak in every run that uses it.
    """
    before = threading.active_count()
    with PeakRss(interval=0.005) as p:
        assert p._thread is not None and p._thread.daemon
    assert not p._thread.is_alive()
    assert threading.active_count() == before
