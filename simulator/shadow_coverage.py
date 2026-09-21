"""Shadow outcome coverage: what share of the shadow ledger's fills are
in markets that expire AFTER the run that opened them has already died.

    python -m simulator.shadow_coverage [--ledger data/hyxshadow.duckdb]
                                        [--db data/hyxlab.duckdb]

Why this exists (2026-08-01). `simulator.shadow` documents at module
level that "Restart = fresh sim state (positions reset); each run gets a
run_id". That is a deliberate design call and this report does not
contest it — a run is one sim under one code version, and adopting a
prior run's book would silently mix fill semantics across code changes.

The consequence, however, was never measured, and it is severe. A
shadow run only ever observes the OUTCOME of a position if it is still
alive when that market closes. The weather ladders that supply most of
the ledger's fills expire ~24h after they open, and the run lifetime has
collapsed as the development cadence sped up — 126h, 42h, 24h, 12h,
then five consecutive runs near 6h. Measured on the live ledger at the
time of writing, the share of fills whose market closes after the run
ended runs 32.8% -> 46.9% -> 71.3% -> 99.8% -> 96.9% and then
**100.0% for every run since 2026-07-31 08:20**.

CORRECTION (2026-08-01 20:20), and it bounds the paragraph above. That
first reading pooled 4,802 fills over five runs and called every one of
them unobserved — but one of the five was STILL RUNNING when the report
was taken, and contributed 1,059 fills (22%) whose markets had not yet
closed. Those fills had not failed to be observed; they had not got
there yet. A live run's zero is CENSORING, not failure, and folding it
into the pool guarantees a 0.0 that no data could have avoided. Coverage
is therefore computed over observed + MISSED only, with pending fills
reported separately — see build_coverage.

The corrected picture is still bad, and the direction of the original
finding stands: every run that actually DIED short reads 0.0. But the
live run is not evidence for it.

Two things follow, and both are validity bounds on readings elsewhere:

  1. `shadow_equity` is not a strategy's equity curve. It is a ~6h
     fragment that opens positions and is killed before any of them
     resolve, so it measures enter-and-hold-for-6h. Pooling equity or
     drawdown across runs does not recover the missing settlement leg —
     for a weather ladder that leg is where the PnL is.
  2. The settlement and mark-to-market paths hardened on 07-31
     (`_mark`'s carried mid, `_settle`'s contract retirement) are
     unreachable in production at this cadence. Three consecutive audits
     asked whether a live position crossed a clear and got "no" all
     three times. That is not luck; at 100% unobserved it is arithmetic.

Unit of counting, per the standing lesson. Coverage is reported by fill
COUNT and by NOTIONAL (|qty| * price), because a count can read
reassuring while every large position sits on the unobserved side. Read
`coverage_notional` when sizing matters; they are reported side by side
rather than one being chosen for you.

`undated` markets (no `close_time` in the archive) are counted and
reported separately, never folded into either side of the ratio — an
unknown expiry is not evidence of coverage.

CORRECTION (2026-08-03 08:20), and it bounds this report's own headline
number. On 08-03 run `20260802T204103` became the first run in 39 to
read a non-zero `coverage_fills` (0.1098, 343 fills) — 40 of its markets
closed while it was alive. `shadow_settlements` was nonetheless EMPTY,
and item 2 above is the reason it matters: coverage exists to say
whether the settlement path could fire, and it answered a DIFFERENT
question.

`_settle` does not gate on the clock. It gates on `markets.result`, and
`result` is not written when a market closes. The collector's 5-minute
upsert only carries markets that are still live, so a settled result
reaches the archive solely through the daily kalshi sweep at 11:10 UTC.
Measured on the live archive at 08:20: every kalshi market that closed
on 08-03 is unresolved (124 markets over 03:00–08:00 UTC), while
everything through 08-02 21:00 is resolved. The write is a once-daily
batch, so a market closing at 04:59 UTC waits 6.2h for its result and
one closing at 11:30 UTC waits 23.7h — ON TOP of the close itself.

So `hours_to_first_outcome` understates the lifetime a run needs before
it can settle anything, by between 6 and 24 hours. Run
`20260802T204103` needed to survive to 11:10 UTC (14.5h); it was
restarted at 08:00, and its 2.62h `h_to_1st` said it had cleared the
bar 5.4h earlier. It had cleared the CLOSE bar. Nothing settled.

`settle_coverage_*` therefore partitions the same fills against the
predicate `_settle` actually uses — was this market's result available
before the run ended — using the identical observed/pending/missed
three-way split. `coverage_*` keeps its close-time meaning UNCHANGED so
archived reports stay comparable, per the `concentration` /
`unobserved_*` precedent; the two are a bracket on WHAT WAS OBSERVED,
and a run can pass one and fail the other.

The resolution instant is not recorded, so it is BRACKETED rather than
guessed. `markets.updated_at` is the row's LAST write, and for a settled
market the sweep that wrote `result` is normally the last writer, so it
is a close estimate — but it is an estimate, so the report carries both
ends: the floor requires `updated_at <= run_end` (conservative: a row
re-touched later reads as unsettled), the ceiling requires only that the
market closed before run end and has a result NOW (optimistic: it
assumes the result was there the whole time). Read them together. Where
they disagree the answer is unknown, which is the point of a bracket.

This is a bound on OTHER readings, not a verdict on any strategy.
"""

import argparse
import json
import math
import statistics
from datetime import UTC, datetime
from pathlib import Path

from hyxlab.store import connect_retry

# Runs pooled into the `recent` block. Five is the span over which the
# live ledger first read 100% unobserved, so it is the window that shows
# the current regime rather than the historical average that dilutes it.
RECENT_RUNS = 5

# A run is LIVE if its last equity tick is this recent. The shadow daemon
# writes one tick per poll, so the threshold is a bet on the inter-tick
# gap distribution.
#
# CORRECTION (2026-09-21). This constant shipped with the prose "tops out
# at ~37s (p99 ~35s) across every recent run, so 5 minutes is ~8x the
# worst observed gap". The p99 is right and the max is not, and the max
# is the only end of the distribution this threshold is exposed to. The
# 8x was measured OUTSIDE the code, so no artifact could contradict it;
# `tick_gap` (build_coverage) now measures it IN the report, over every
# consecutive-tick pair in the ledger and gated on nothing — the sample
# can exceed LIVE_GRACE_S, which is the whole point (mistakes #67).
#
# Measured on the live ledger at the correction, 308,027 ticks over 54
# runs: median 16.9s and p99 ~36s as claimed, but the max is 21,027s
# (5.8h) and TWENTY of the 54 runs contain at least one gap past the
# grace — 150 gaps in total. The current 221h run's own max is 1,269s,
# so the real figure is 0.24x the worst observed gap, not 8x.
#
# The consequence is the 08-01 correction running backwards: inside such
# a stall a LIVE run reads dead, and its open fills move from `pending`
# (censoring) to `missed` (failure) — the exact conflation this module
# was built to refuse. Pooled over the whole ledger 1.92% of shadow
# wall-clock sits inside a stall, so ~1 reading in 52 is taken there.
# None of the 9 archived readings landed in one (0.17 expected), so
# nothing archived moves and nothing is being rescued.
#
# The constant is UNCHANGED, deliberately, per the standing comparability
# precedent and because re-sizing a threshold off the observed max is how
# the wrong number got here. `tick_gap.over_grace_n` and
# `stall_exposure_frac` are the tripwire instead: they say how exposed
# each reading is, in the artifact, where a later pass can check it.
LIVE_GRACE_S = 300


def _p95(ordered: list[float]) -> float:
    """Nearest-rank 95th percentile: the smallest value >= 95% of the
    sample. `ordered[int(0.95 * n)]` has rank >= 95% by construction and
    equals the maximum whenever 0.95n is an integer (prioritycheck._p95,
    same fix). Nearest rank is ceil(0.95n) - 1."""
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _tick_gap(gaps: list[float]) -> dict | None:
    """The inter-tick gap distribution LIVE_GRACE_S is a bet on.

    `gaps` is every consecutive-tick delta in seconds, collected with no
    predicate on its size — a statistic offered as evidence about a
    threshold has to come from a sample that could have violated it.

    None when fewer than two ticks exist: one tick has no gap, and a
    zero would read as a perfect record rather than as no record.
    """
    if not gaps:
        return None
    ordered = sorted(gaps)
    span = sum(gaps)
    # Wall-clock the run spent PAST the grace with no tick — the share of
    # this run's life in which a reading would have called it dead.
    exposed = sum(g - LIVE_GRACE_S for g in gaps if g > LIVE_GRACE_S)
    return {
        "n": len(ordered),
        "median_s": round(statistics.median(ordered), 3),
        "p95_s": round(_p95(ordered), 3),
        "max_s": round(ordered[-1], 3),
        "over_grace_n": sum(1 for g in ordered if g > LIVE_GRACE_S),
        # The number the old prose asserted was ~8. < 1 means the grace
        # is SHORTER than a gap this run actually took.
        "grace_multiple_of_max": round(LIVE_GRACE_S / ordered[-1], 3) if ordered[-1] > 0 else None,
        "stall_exposure_frac": round(exposed / span, 6) if span > 0 else None,
    }


def _ratio(observed: float, missed: float) -> float | None:
    """Coverage = observed / (observed + missed), or None when the run
    has had no OPPORTUNITY to observe anything. None, not 0.0 or 1.0 — a
    run with nothing to observe has no coverage, and defaulting either
    way would read as a finding.

    `missed` excludes PENDING fills (see build_coverage): a live run's
    open position has not failed to be observed, it simply has not got
    there yet, and putting it in the denominator manufactures a 0.0.
    """
    total = observed + missed
    if total <= 0:
        return None
    return round(observed / total, 4)


#: `markets.result` values that mean the market actually resolved. An
#: empty string is the archive's "closed but not yet swept" state and is
#: emphatically NOT a resolution — `_settle` (sim.py) tests membership in
#: exactly this set, so the report must too or it measures a different
#: gate than the code it exists to watch.
RESOLVED = ("yes", "no")


def settled_before(end, close, result, updated_at, bound: str = "floor") -> bool:
    """Was this market's result available to `_settle` before `end`?

    The resolution instant is not recorded anywhere, so the two bounds
    bracket it (see module docstring):

      floor    `updated_at <= end`. The row's last write is at or after
               the result write, so this can only UNDER-count: a row
               re-touched after the run ended reads as unsettled even if
               its result landed long before.
      ceiling  `close <= end` and a result exists now. Assumes the
               result was available the instant the market closed, which
               is exactly the assumption this report was built to
               refute — kept so the two ends can be read together.

    An unresolved market is False under BOTH bounds: no bound can settle
    a contract the archive has no result for.
    """
    if end is None or result not in RESOLVED:
        return False
    if bound == "ceiling":
        return close is not None and close <= end
    if updated_at is None:
        return False
    return updated_at <= end


def _pool_settle(subset: list[dict], bound: str):
    """Pool one settlement bound across runs.

    Ratios are recomputed from the pooled counts, never averaged from
    the per-run ratios — a run with three fills would otherwise weigh as
    much as one with three thousand.
    """
    obs = sum(r[f"settle_observed_fills_{bound}"] for r in subset)
    missed = sum(r[f"settle_missed_fills_{bound}"] for r in subset)
    pending = sum(r[f"settle_pending_fills_{bound}"] for r in subset)
    obs_v = sum(r[f"settle_observed_notional_{bound}"] for r in subset)
    missed_v = sum(r[f"settle_missed_notional_{bound}"] for r in subset)
    return (
        ("observed_fills", obs),
        ("missed_fills", missed),
        ("pending_fills", pending),
        ("coverage_fills", _ratio(obs, missed)),
        ("observed_notional", round(obs_v, 2)),
        ("missed_notional", round(missed_v, 2)),
        ("coverage_notional", _ratio(obs_v, missed_v)),
    )


def _bucket(condition: bool, live: bool) -> str:
    """The standing three-way split, shared by both partitions.

    `condition` is the thing having happened by run end. If it did, the
    run observed it. If it did not, a LIVE run has simply not got there
    yet (censoring) and a DEAD one never will (failure). Extracted so
    tests exercise the shipped path rather than re-deriving it — the
    08-03 lesson from `over_award_split`.
    """
    if condition:
        return "observed"
    return "pending" if live else "missed"


def build_coverage(
    ledger, markets_conn, recent_runs: int = RECENT_RUNS, now: datetime | None = None
) -> dict:
    """Per-run and pooled outcome coverage.

    `ledger` is a connection to the shadow ledger; `markets_conn` a
    connection carrying a `markets` table with `market_id`/`close_time`.
    They are separate databases in production and separate arguments
    here so the report can be tested without an ATTACH.

    A dated fill lands in exactly one of three buckets:

      observed  its market closed at or before the run end — the run
                saw the outcome, which is the thing being counted.
      pending   its market closes after the run end and the run is still
                LIVE. The outcome has not happened yet. This is
                CENSORING, not failure, and it is excluded from both
                sides of the ratio.
      missed    its market closes after the run end and the run is DEAD.
                Permanently unobservable — the run was killed first.

    `now` is naive UTC (the ledger stores naive UTC via shadow._naive)
    and is injectable so liveness is testable without freezing a clock.
    """
    if now is None:
        now = datetime.now(UTC).replace(tzinfo=None)
    # `result`/`updated_at` come along for the settlement partition: a
    # close is not a resolution, and the report must read the same field
    # `_settle` reads.
    meta = {
        mid: (ct, res, upd)
        for mid, ct, res, upd in markets_conn.execute(
            "SELECT market_id, close_time, result, updated_at FROM markets"
        ).fetchall()
    }

    # Run end is the last EQUITY tick, not the last fill: a run keeps
    # marking after it stops trading, and using the last fill would
    # shorten the observation window and overstate the problem.
    ends = dict(
        ledger.execute("SELECT run_id, max(ts) FROM shadow_equity GROUP BY run_id").fetchall()
    )

    # Every consecutive-tick gap, per run. No WHERE on the gap: this is
    # the sample that has to be able to exceed LIVE_GRACE_S for the
    # liveness threshold's margin to be checkable at all.
    gaps: dict[str, list[float]] = {}
    for run_id, gap in ledger.execute(
        "SELECT run_id, gap FROM ("
        "  SELECT run_id,"
        "    datesub('millisecond', lag(ts) OVER (PARTITION BY run_id ORDER BY ts), ts) / 1000.0"
        "      AS gap"
        "  FROM shadow_equity"
        ") WHERE gap IS NOT NULL"
    ).fetchall():
        gaps.setdefault(run_id, []).append(float(gap))

    rows = ledger.execute(
        "SELECT run_id, started_at FROM shadow_runs ORDER BY started_at"
    ).fetchall()

    runs = []
    for run_id, started_at in rows:
        end = ends.get(run_id)
        fills = ledger.execute(
            "SELECT market_id, qty, price FROM shadow_fills WHERE run_id = ?",
            [run_id],
        ).fetchall()
        if not fills:
            continue

        live = end is not None and (now - end).total_seconds() <= LIVE_GRACE_S

        obs_n = missed_n = pending_n = undated_n = 0
        obs_v = missed_v = pending_v = undated_v = 0.0
        # Settlement partition, one counter set per bound. Same buckets,
        # different predicate — see settled_before.
        set_n = {b: dict(observed=0, pending=0, missed=0) for b in ("floor", "ceiling")}
        set_v = {b: dict(observed=0.0, pending=0.0, missed=0.0) for b in ("floor", "ceiling")}
        first_close = None
        first_resolved = None
        for market_id, qty, price in fills:
            notional = abs(qty or 0.0) * (price or 0.0)
            close, result, updated_at = meta.get(market_id, (None, None, None))
            if close is None:
                undated_n += 1
                undated_v += notional
                continue
            if first_close is None or close < first_close:
                first_close = close
            resolved_at = updated_at if result in RESOLVED else None
            if resolved_at is not None and (first_resolved is None or resolved_at < first_resolved):
                first_resolved = resolved_at
            bucket = _bucket(end is not None and close <= end, live)
            if bucket == "observed":
                obs_n += 1
                obs_v += notional
            elif bucket == "pending":
                pending_n += 1
                pending_v += notional
            else:
                missed_n += 1
                missed_v += notional
            for bound in ("floor", "ceiling"):
                b = _bucket(settled_before(end, close, result, updated_at, bound), live)
                set_n[bound][b] += 1
                set_v[bound][b] += notional

        life_h = None
        if end is not None and started_at is not None:
            life_h = round((end - started_at).total_seconds() / 3600.0, 2)

        # How much longer the run needed to live to observe its FIRST
        # outcome. For a live run that is hours from now; for a dead one
        # it is the shortfall it was killed by. None once something has
        # already been observed — there is no first outcome still owed.
        hours_to_first = None
        if first_close is not None and end is not None and first_close > end:
            hours_to_first = round((first_close - end).total_seconds() / 3600.0, 2)

        # The shortfall that actually gates settlement. None when no
        # fill's market has a recorded resolution AT ALL — that is not a
        # zero shortfall, it is an unknown one, and the companion
        # `unresolved_fills` says how much of the book is in that state.
        # Reporting 0.0 here would read as "nothing more was needed" for
        # precisely the runs that needed the most.
        hours_to_first_settle = None
        if first_resolved is not None and end is not None and first_resolved > end:
            hours_to_first_settle = round((first_resolved - end).total_seconds() / 3600.0, 2)
        unresolved_n = sum(
            1
            for market_id, _q, _p in fills
            if meta.get(market_id, (None, None, None))[1] not in RESOLVED
        )

        runs.append(
            {
                "run_id": run_id,
                "started_at": started_at.isoformat() if started_at else None,
                "ended_at": end.isoformat() if end else None,
                "life_hours": life_h,
                "live": live,
                # How close this reading's `live` call was to its own
                # threshold, so the margin is in the artifact rather than
                # implied by the boolean.
                "since_last_tick_s": (
                    None if end is None else round((now - end).total_seconds(), 3)
                ),
                "tick_gap": _tick_gap(gaps.get(run_id, [])),
                "first_close": first_close.isoformat() if first_close else None,
                "hours_to_first_outcome": hours_to_first,
                "fills": len(fills),
                "observed_fills": obs_n,
                # Kept as missed + pending, i.e. the pre-partition meaning,
                # so archived reports stay comparable. `coverage_*` IS
                # replaced rather than kept: for a live run the old value
                # was an artifact of counting censored fills as failures,
                # and a coarser-but-valid bound it was not.
                "unobserved_fills": missed_n + pending_n,
                "missed_fills": missed_n,
                "pending_fills": pending_n,
                "undated_fills": undated_n,
                "coverage_fills": _ratio(obs_n, missed_n),
                "observed_notional": round(obs_v, 2),
                "unobserved_notional": round(missed_v + pending_v, 2),
                "missed_notional": round(missed_v, 2),
                "pending_notional": round(pending_v, 2),
                "undated_notional": round(undated_v, 2),
                "coverage_notional": _ratio(obs_v, missed_v),
                "hours_to_first_settleable": hours_to_first_settle,
                "unresolved_fills": unresolved_n,
                **{
                    f"settle_{k}_{bound}": v
                    for bound in ("floor", "ceiling")
                    for k, v in (
                        ("observed_fills", set_n[bound]["observed"]),
                        ("missed_fills", set_n[bound]["missed"]),
                        ("pending_fills", set_n[bound]["pending"]),
                        (
                            "coverage_fills",
                            _ratio(set_n[bound]["observed"], set_n[bound]["missed"]),
                        ),
                        ("observed_notional", round(set_v[bound]["observed"], 2)),
                        ("missed_notional", round(set_v[bound]["missed"], 2)),
                        (
                            "coverage_notional",
                            _ratio(set_v[bound]["observed"], set_v[bound]["missed"]),
                        ),
                    )
                },
            }
        )

    def _pool(subset: list[dict]) -> dict:
        obs_n = sum(r["observed_fills"] for r in subset)
        missed_n = sum(r["missed_fills"] for r in subset)
        pending_n = sum(r["pending_fills"] for r in subset)
        obs_v = sum(r["observed_notional"] for r in subset)
        missed_v = sum(r["missed_notional"] for r in subset)
        pending_v = sum(r["pending_notional"] for r in subset)
        return {
            "runs": len(subset),
            "live_runs": sum(1 for r in subset if r["live"]),
            # Pooled over the raw gaps, never averaged from the per-run
            # blocks: a 20-tick run would otherwise weigh as much as a
            # 40,000-tick one.
            "tick_gap": _tick_gap([g for r in subset for g in gaps.get(r["run_id"], [])]),
            "runs_with_gap_over_grace": sum(
                1 for r in subset if (r["tick_gap"] or {}).get("over_grace_n", 0) > 0
            ),
            "fills": sum(r["fills"] for r in subset),
            "observed_fills": obs_n,
            "unobserved_fills": missed_n + pending_n,
            "missed_fills": missed_n,
            "pending_fills": pending_n,
            "undated_fills": sum(r["undated_fills"] for r in subset),
            "coverage_fills": _ratio(obs_n, missed_n),
            "observed_notional": round(obs_v, 2),
            "unobserved_notional": round(missed_v + pending_v, 2),
            "missed_notional": round(missed_v, 2),
            "pending_notional": round(pending_v, 2),
            "coverage_notional": _ratio(obs_v, missed_v),
            "unresolved_fills": sum(r["unresolved_fills"] for r in subset),
            **{
                f"settle_{k}_{bound}": v
                for bound in ("floor", "ceiling")
                for k, v in _pool_settle(subset, bound)
            },
        }

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "recent_runs_window": recent_runs,
        "runs": runs,
        "pooled": _pool(runs),
        "recent": _pool(runs[-recent_runs:]),
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description="shadow outcome coverage: fills whose market outlives the run"
    )
    ap.add_argument("--ledger", default="data/hyxshadow.duckdb")
    ap.add_argument("--db", default="data/hyxlab.duckdb")
    ap.add_argument("--out", default="reports/shadow_coverage")
    args = ap.parse_args()

    ledger = connect_retry(args.ledger)
    markets_conn = connect_retry(args.db)
    try:
        report = build_coverage(ledger, markets_conn)
    finally:
        ledger.close()
        markets_conn.close()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{datetime.now(UTC):%Y%m%dT%H%M%S}.json"
    out.write_text(json.dumps(report, indent=1) + "\n")

    recent = report["recent"]
    print(
        f"[shadow_coverage] {len(report['runs'])} runs;"
        f" recent {recent['runs']} ({recent['live_runs']} live):"
        f" coverage_fills={recent['coverage_fills']}"
        f" coverage_notional={recent['coverage_notional']}"
        f" ({recent['missed_fills']} missed, {recent['pending_fills']} pending"
        f" of {recent['fills']} fills)"
    )
    # The settlement line is printed BESIDE the close line, never
    # instead of it: a run can clear the close bar and settle nothing,
    # and that gap is the whole reason this pair exists.
    print(
        f"[shadow_coverage] settle floor={recent['settle_coverage_fills_floor']}"
        f" ceiling={recent['settle_coverage_fills_ceiling']}"
        f" ({recent['unresolved_fills']} of {recent['fills']} fills in markets"
        f" the archive has no result for)"
    )
    # The liveness threshold's own margin, printed beside the coverage it
    # decides: every `pending` on this page is a `live` call, and a stall
    # past the grace turns those into `missed`.
    tg = report["pooled"]["tick_gap"]
    if tg is None:
        print("[shadow_coverage] tick_gap: no run has two ticks; liveness margin unmeasured")
    else:
        print(
            f"[shadow_coverage] tick_gap n={tg['n']} median={tg['median_s']}s"
            f" p95={tg['p95_s']}s max={tg['max_s']}s;"
            f" grace {LIVE_GRACE_S}s = {tg['grace_multiple_of_max']}x the worst gap,"
            f" {tg['over_grace_n']} gaps past it in"
            f" {report['pooled']['runs_with_gap_over_grace']} of {report['pooled']['runs']} runs"
            f" ({tg['stall_exposure_frac']} of wall-clock, the chance this reading"
            f" calls a live run dead)"
        )
    print(
        "| run | life_h | live | fills | missed | pending | h_to_1st | cov_fills"
        " | cov_notl | settle_lo | settle_hi | h_to_1st_settle |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in report["runs"][-12:]:
        print(
            f"| {r['run_id']} | {r['life_hours']} | {'yes' if r['live'] else ''}"
            f" | {r['fills']} | {r['missed_fills']} | {r['pending_fills']}"
            f" | {r['hours_to_first_outcome']} | {r['coverage_fills']}"
            f" | {r['coverage_notional']} | {r['settle_coverage_fills_floor']}"
            f" | {r['settle_coverage_fills_ceiling']}"
            f" | {r['hours_to_first_settleable']} |"
        )
    print(f"[shadow_coverage] written to {out}")


if __name__ == "__main__":
    main()
