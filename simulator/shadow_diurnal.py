"""Shadow equity by hour: the sampling convention, and the diurnal shape.

    python -m simulator.shadow_diurnal [--ledger data/hyxshadow.duckdb]
                                       [--run RUN_ID] [--out reports/shadow_diurnal]

Why this exists (2026-08-23). `simulator.shadow_attribution` decomposed
a run's WHOLE-LIFE equity into realized + open carry and settled the
"settle-and-slide" question at that resolution. It left the successor
question open: within a day, when does the open book get marked up and
down, and is the repeating afternoon trough real?

Six status passes answered that by quoting numbers like "a NEW RUN LOW
-301.3 (17Z)" and "08-21 traced the same 15-17Z dive to -296.9". Those
are intra-hour MINIMA, read off a curve sampled ~177 times an hour, and
compared against each other across hours. That comparison is not valid,
because min-sampling manufactures depth in proportion to intra-hour
mark VOLATILITY -- and on this ledger volatility is itself strongly
diurnal. Run `20260821T015256`, mean intra-hour range by UTC hour:

    00-10Z   29-64      (quiet overnight book)
    11Z     131.6
    16Z     158.8
    17Z     253.2
    20Z     301.8       <- the LOUDEST hour of the day
    22Z     216.3

So 20Z has the largest range of any hour. Sampled at its minimum it
reads -153.2; sampled at its close it reads +70.0. Nothing happened at
20Z except that the marks got noisy. The reason 16-17Z read as "the
trough" is that the loud window overlaps the day's declining leg, and
the minimum then exaggerates the low by 50-225 points. The honest daily
shape is the hour-END series: a single smooth cycle, high ~+72 at 03Z,
low -247 at 16Z, recovering to +150 by 22Z -- one daily oscillation,
not a "15-17Z dive".

This module therefore publishes hour-end as the level series and prints
the intra-hour range NEXT TO IT in the same row, so the confound is
visible rather than latent. `min_gap` (hour_end - hour_min) is the depth
a minimum-sampled reading would have invented for that hour.

The second column that matters is the split of each hour's equity move:

    d_equity = -entry_drag + reval

`entry_drag` is what the hour's NEW fills cost on the way in: a taker
pays the ask and is immediately marked at the mid, so each contract
books (ask - mid) + fee before the market moves at all. `reval` is the
residual -- the revaluation of the book that was already standing. On
this ledger drag runs 8-24/hr against reval swings of +-300, which is
the whole reason the daily shape is a marking story and not a
transaction-cost story.

VALIDITY BOUNDS, in the `validity` block rather than left to memory:

  1. `entry_drag` is a MODEL, not a measurement: the ledger stores no
     mid at fill time, so the mid is taken as ask - half_tick. That is
     true BY CONSTRUCTION for a one-tick-spread-gated strategy (the
     probe fills this ledger and gates on exactly that) and false for
     anything else. `drag_model_valid` is false when any fill comes
     from a strategy not in TIGHT_GATED, and then `reval` is null
     rather than wrong.
  2. The first and last hour buckets of a run are PARTIAL -- their
     "hour end" is not an hour end and their range is under-sampled.
     They are excluded from the diurnal profile and flagged `partial`.
  3. An hour with few equity points has an unreliable min and range.
     `pts` is reported per hour and hours under `MIN_PTS_PER_HOUR` are
     excluded from the profile's range statistics.
  4. Hour-of-day means need days to average. `n_days` is reported per
     hour, and a profile built from under `MIN_DAYS` days reads
     UNDERPOWERED -- this run has 2-3 days, so it IS underpowered and
     says so.
  5. `reval` absorbs settlement as well as marking. Settlements are
     counted per hour so a contaminated hour is visible; on this run
     the big reval hours carry ZERO settlements, which is what makes
     them a marking result.
  6. A MEAN OVER DAYS CANNOT SAY WHETHER A SHAPE REPEATS. The profile
     above answers "what is the average hour-of-day level"; the
     question it invites -- "does the +72/-247/+150 cycle recur
     tomorrow" -- is a different one, and averaging is exactly the
     operation that destroys the evidence for it. Three days that each
     oscillate on their own schedule and three days that trace the same
     curve produce the same mean. `by_day` therefore publishes each
     day's hour-end series UNAVERAGED, plus pairwise Spearman rho
     between days over the hours they share. Rank correlation is the
     right statistic because a day's equity offset and amplitude are
     nuisance parameters -- only the ORDER of hours within the day is
     the claim. `shape_verdict` reads REPEATS only when every pair
     clears `SHAPE_RHO` on at least `MIN_SHARED_HOURS` shared hours;
     with fewer than two pairs it reads UNDERPOWERED and no shape claim
     may be made from this report.
  7. AN HOUR-OF-DAY MEAN IS A MEAN OVER WHATEVER DAYS THAT HOUR HAD,
     AND RUNS DO NOT START OR END ON THE HOUR-OF-DAY BOUNDARY. A run
     from 20Z Sunday to 19Z Wednesday gives 19Z three readings and 20Z
     two, so the two cells average DIFFERENT DAYS. That would be a
     rounding detail if equity level were stationary; it is not --
     across run `20260823T201714` the daily level slid from -50 to
     -1800, so dropping the worst day from one hour moves that hour's
     mean by hundreds. MEASURED 2026-08-26: mean_end steps +433.6 from
     19Z to 20Z, which reads as a nightly recovery; on the two days
     BOTH hours share it is -14.6. The step was 97% composition.
     `level_panel` therefore names the days common to every published
     hour and `mean_equity_end_balanced` averages only those, so the
     level column can be read DOWN. `mean_equity_end` is kept beside
     it because it is the wider sample for reading any single hour --
     it is comparisons ACROSS hours that it cannot support.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from hyxlab.store import connect_retry

SHADOW_DB = "data/hyxshadow.duckdb"
#: Strategies whose fills are known to be taken under a one-tick spread
#: gate, so that mid == ask - half a tick holds by construction. Only
#: these make the entry-drag model valid; see validity bound 1.
TIGHT_GATED = frozenset({"probe"})
HALF_TICK = 0.005
#: An hour sampled less than this cannot support a min/range reading.
#: The shadow daemon persists one point per poll (~177/hr observed), so
#: this is a very loose floor that only catches genuinely starved hours.
MIN_PTS_PER_HOUR = 20
#: Fewer days than this and an hour-of-day mean is one or two draws.
MIN_DAYS = 3
#: Two days must overlap by at least this many whole hours before their
#: rank correlation says anything about a DAILY shape -- a rho computed
#: over six shared hours is a statement about a morning, not a cycle.
MIN_SHARED_HOURS = 12
#: Pairwise Spearman floor for "the shape repeats". At 12+ shared hours
#: rho >= 0.7 is well outside what independent day-curves produce, and
#: the bar is deliberately one a noisy-but-real cycle can still clear;
#: it is a screen against "the mean is an average of unlike days", not
#: a significance test.
SHAPE_RHO = 0.7


def _r(x: float | None, n: int = 1) -> float | None:
    return None if x is None else round(x, n)


def _hours(conn: duckdb.DuckDBPyConnection, run_id: str) -> list[dict]:
    """One row per clock hour of the run: level, spread, flow, drag."""
    eq = conn.execute(
        """select date_trunc('hour', ts) as h, count(*) as pts,
                  last(equity order by ts) as e_end,
                  min(equity) as e_min, max(equity) as e_max
           from shadow_equity where run_id = ? group by 1 order by 1""",
        [run_id],
    ).fetchall()
    if not eq:
        return []

    fills = {
        h: (n, drag, strats)
        for h, n, drag, strats in conn.execute(
            """select date_trunc('hour', ts), count(*), sum(qty * ? + fee),
                      list(distinct strategy)
               from shadow_fills where run_id = ? group by 1""",
            [HALF_TICK, run_id],
        ).fetchall()
    }
    setts = dict(
        conn.execute(
            """select date_trunc('hour', ts), count(*)
               from shadow_settlements where run_id = ? group by 1""",
            [run_id],
        ).fetchall()
    )

    rows = []
    for i, (h, pts, e_end, e_min, e_max) in enumerate(eq):
        n_fills, drag, strats = fills.get(h, (0, 0.0, []))
        rows.append(
            {
                "hour": f"{h:%Y-%m-%d %H}Z",
                "hour_of_day": h.hour,
                "day": f"{h:%Y-%m-%d}",
                "pts": pts,
                "equity_end": _r(e_end),
                "equity_min": _r(e_min),
                "equity_max": _r(e_max),
                "range": _r(e_max - e_min),
                # What a minimum-sampled reading of this hour would have
                # invented, relative to its close. This is the number the
                # status narration was quoting.
                "min_gap": _r(e_end - e_min),
                "n_fills": n_fills,
                "n_settlements": setts.get(h, 0),
                "entry_drag_modeled": _r(drag or 0.0, 2),
                "strategies": sorted(strats or []),
                # Bound 2: the run's first and last buckets are partial.
                "partial": i == 0 or i == len(eq) - 1,
            }
        )

    # d_equity is close-to-close, so it needs the PRIOR hour. The first
    # hour has none and is partial anyway.
    for prev, cur in zip(rows, rows[1:], strict=False):
        d = cur["equity_end"] - prev["equity_end"]
        cur["d_equity"] = _r(d)
        valid = set(cur["strategies"]) <= TIGHT_GATED
        cur["drag_model_valid"] = valid
        # reval is the residual: the move the standing book made, once
        # the new fills' entry cost is added back. Null, never wrong,
        # when the drag model does not apply (bound 1).
        cur["reval"] = _r(d + cur["entry_drag_modeled"]) if valid else None
    rows[0]["d_equity"] = None
    rows[0]["reval"] = None
    rows[0]["drag_model_valid"] = set(rows[0]["strategies"]) <= TIGHT_GATED
    return rows


def _profile(hours: list[dict]) -> dict:
    """Hour-of-day means over whole hours only (bounds 2, 3, 4, 7)."""
    buckets: dict[int, list[dict]] = defaultdict(list)
    for h in hours:
        if not h["partial"]:
            buckets[h["hour_of_day"]].append(h)

    def _mean(vals: list[float]) -> float | None:
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else None

    # Bound 7: the days present in EVERY published hour. A run that ends
    # at 19Z gives 20Z one fewer day than 19Z, and equity LEVEL trends
    # across days by hundreds, so a mean over "whatever days this hour
    # had" is not comparable to the hour beside it.
    panel = sorted(set.intersection(*({r["day"] for r in rows} for rows in buckets.values())))

    table = []
    for hod in sorted(buckets):
        rows = buckets[hod]
        dense = [r for r in rows if r["pts"] >= MIN_PTS_PER_HOUR]
        on_panel = [r for r in rows if r["day"] in panel]
        table.append(
            {
                "hour_of_day": hod,
                "n_days": len({r["day"] for r in rows}),
                "days": sorted({r["day"] for r in rows}),
                # Bound 7: same days in every row, so the COLUMN can be
                # read down. Null only when the panel is empty.
                "balanced": {r["day"] for r in rows} == set(panel),
                "mean_equity_end_balanced": _r(_mean([r["equity_end"] for r in on_panel])),
                "mean_equity_end": _r(_mean([r["equity_end"] for r in rows])),
                "mean_equity_min": _r(_mean([r["equity_min"] for r in rows])),
                # Bound 3: range/min_gap only over densely sampled hours.
                "n_days_dense": len({r["day"] for r in dense}),
                "mean_range": _r(_mean([r["range"] for r in dense])),
                "mean_min_gap": _r(_mean([r["min_gap"] for r in dense])),
                "mean_reval": _r(_mean([r["reval"] for r in rows])),
                "mean_drag": _r(_mean([r["entry_drag_modeled"] for r in rows]), 2),
                "mean_fills": _r(_mean([float(r["n_fills"]) for r in rows])),
                "settlement_hours": sum(1 for r in rows if r["n_settlements"]),
            }
        )
    ragged = [p["hour_of_day"] for p in table if not p["balanced"]]
    return {
        "by_hour_of_day": table,
        "level_panel": {
            "days": panel,
            "ragged_hours": ragged,
            # The one sentence a reader of the level column needs.
            "level_verdict": (
                f"BALANCED (every hour averages the same {len(panel)} day(s))"
                if not ragged
                else (
                    f"RAGGED ({len(ragged)} hour(s) average a different day set:"
                    f" {', '.join(f'{h:02d}Z' for h in ragged)}) — read LEVEL off"
                    f" mean_end_bal, which is the {len(panel)}-day panel, never off"
                    " mean_end, where a step between hours can be a change of days"
                )
            ),
        },
    }


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    """Rank correlation, ties averaged. None when a side is constant."""
    n = len(xs)
    if n < 2:
        return None

    def _ranks(vs: list[float]) -> list[float]:
        order = sorted(range(n), key=lambda i: vs[i])
        out = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and vs[order[j + 1]] == vs[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                out[order[k]] = avg
            i = j + 1
        return out

    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    dx = sum((a - mx) ** 2 for a in rx)
    dy = sum((b - my) ** 2 for b in ry)
    if dx <= 0 or dy <= 0:
        return None
    return num / (dx * dy) ** 0.5


def _by_day(hours: list[dict]) -> dict:
    """Each day's hour-end series unaveraged, and whether they agree.

    Bound 6: this is the block that answers "does the cycle repeat",
    which `by_hour_of_day` structurally cannot.
    """
    days: dict[str, dict[int, float]] = defaultdict(dict)
    for h in hours:
        if not h["partial"]:
            days[h["day"]][h["hour_of_day"]] = h["equity_end"]

    series = []
    for day in sorted(days):
        curve = days[day]
        hods = sorted(curve)
        peak = max(hods, key=lambda k: curve[k])
        trough = min(hods, key=lambda k: curve[k])
        series.append(
            {
                "day": day,
                "n_whole_hours": len(hods),
                "hour_end": {f"{k:02d}": _r(curve[k]) for k in hods},
                "peak_hour": peak,
                "peak": _r(curve[peak]),
                "trough_hour": trough,
                "trough": _r(curve[trough]),
                "amplitude": _r(curve[peak] - curve[trough]),
            }
        )

    pairs = []
    for i in range(len(series)):
        for j in range(i + 1, len(series)):
            a, b = series[i], series[j]
            shared = sorted(set(days[a["day"]]) & set(days[b["day"]]))
            rho = (
                _spearman([days[a["day"]][k] for k in shared], [days[b["day"]][k] for k in shared])
                if len(shared) >= MIN_SHARED_HOURS
                else None
            )
            pairs.append(
                {
                    "days": [a["day"], b["day"]],
                    "n_shared_hours": len(shared),
                    # None means the pair is UNSCORED (too little overlap),
                    # which is not the same as a pair that disagreed.
                    "rho": _r(rho, 3),
                }
            )

    scored = [p for p in pairs if p["rho"] is not None]
    if len(scored) < 2:
        verdict = (
            f"UNDERPOWERED ({len(scored)} scorable day-pair(s) at"
            f" >= {MIN_SHARED_HOURS} shared hours; no shape claim)"
        )
    elif min(p["rho"] for p in scored) >= SHAPE_RHO:
        verdict = (
            f"REPEATS (all {len(scored)} day-pairs rho >="
            f" {min(p['rho'] for p in scored)} >= {SHAPE_RHO})"
        )
    else:
        verdict = (
            f"DOES NOT REPEAT (weakest of {len(scored)} day-pairs rho"
            f" {min(p['rho'] for p in scored)} < {SHAPE_RHO})"
        )
    return {
        "by_day": series,
        "shape_agreement": {
            "pairs": pairs,
            "min_shared_hours": MIN_SHARED_HOURS,
            "shape_rho_floor": SHAPE_RHO,
            "shape_verdict": verdict,
        },
    }


def build_diurnal(ledger: duckdb.DuckDBPyConnection, run_id: str | None = None) -> dict:
    """Hourly equity level, spread and move-split for each shadow run."""
    runs = [
        r[0]
        for r in ledger.execute(
            "select distinct run_id from shadow_equity"
            + (" where run_id = ?" if run_id else "")
            + " order by run_id",
            [run_id] if run_id else [],
        ).fetchall()
    ]
    report: dict = {
        "generated_at": f"{datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ}",
        "half_tick": HALF_TICK,
        "tight_gated_strategies": sorted(TIGHT_GATED),
        "runs": [],
    }
    for rid in runs:
        hours = _hours(ledger, rid)
        prof = _profile(hours)
        daily = _by_day(hours)
        whole = [h for h in hours if not h["partial"]]
        # Days SPANNED is not draws per hour: a run covering three
        # calendar days can still give most hours-of-day only two
        # readings. The weakest published hour is what bounds the
        # profile, so power is judged on the minimum (bound 4).
        n_days = len({h["day"] for h in whole})
        min_draws = min((p["n_days"] for p in prof["by_hour_of_day"]), default=0)
        drag_valid = all(h["drag_model_valid"] for h in hours)
        # The loudest hour of the day is the headline of this report --
        # it is the one that shows min-sampling is not measuring level.
        dense_prof = [p for p in prof["by_hour_of_day"] if p["mean_range"] is not None]
        loudest = max(dense_prof, key=lambda p: p["mean_range"], default=None)
        quietest = min(dense_prof, key=lambda p: p["mean_range"], default=None)
        report["runs"].append(
            {
                "run_id": rid,
                "n_hours": len(hours),
                "n_whole_hours": len(whole),
                "n_days": n_days,
                "min_draws_per_hour": min_draws,
                "validity": {
                    "drag_model_valid": drag_valid,
                    "strategies": sorted({s for h in hours for s in h["strategies"]}),
                    "partial_hours_excluded": sum(1 for h in hours if h["partial"]),
                    "sparse_hours": sum(1 for h in hours if h["pts"] < MIN_PTS_PER_HOUR),
                    "min_pts_per_hour": MIN_PTS_PER_HOUR,
                    "profile_verdict": (
                        f"UNDERPOWERED (weakest hour has {min_draws} <"
                        f" {MIN_DAYS} draws; run spans {n_days} whole days)"
                        if min_draws < MIN_DAYS
                        else f"{min_draws}+ draws per hour over {n_days} whole days"
                    ),
                },
                "range_extremes": {
                    "loudest_hour_of_day": loudest["hour_of_day"] if loudest else None,
                    "loudest_mean_range": loudest["mean_range"] if loudest else None,
                    "quietest_hour_of_day": quietest["hour_of_day"] if quietest else None,
                    "quietest_mean_range": quietest["mean_range"] if quietest else None,
                },
                "hours": hours,
                **prof,
                **daily,
            }
        )
    return report


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ledger", default=SHADOW_DB)
    ap.add_argument("--run", default=None, help="run_id (default: every run)")
    ap.add_argument("--out", default="reports/shadow_diurnal")
    args = ap.parse_args(argv)

    ledger = connect_retry(args.ledger, read_only=True)
    try:
        report = build_diurnal(ledger, args.run)
    finally:
        ledger.close()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{datetime.now(UTC):%Y%m%dT%H%M%S}.json"
    out.write_text(json.dumps(report, indent=1) + "\n")

    # Print the profile for the longest-lived run: an hour-of-day mean
    # over one day is not a profile, and printing it invites it to be
    # read as one.
    best = max(report["runs"], key=lambda r: r["n_whole_hours"], default=None)
    if best and best["by_hour_of_day"]:
        v = best["validity"]
        print(
            f"[shadow_diurnal] {best['run_id']} — {best['n_whole_hours']} whole hours,"
            f" profile {v['profile_verdict']}, drag model"
            f" {'VALID' if v['drag_model_valid'] else 'INVALID'} ({', '.join(v['strategies'])})"
        )
        print(f"[shadow_diurnal] level panel — {best['level_panel']['level_verdict']}")
        print(
            "| UTC | mean_end_bal | mean_end | mean_min | min_gap | range"
            " | reval | drag | fills | days |"
        )
        print("|---|---|---|---|---|---|---|---|---|---|")
        for p in best["by_hour_of_day"]:
            print(
                f"| {p['hour_of_day']:02d}Z | {p['mean_equity_end_balanced']}"
                f" | {p['mean_equity_end']}{'' if p['balanced'] else ' *'}"
                f" | {p['mean_equity_min']}"
                f" | {p['mean_min_gap']} | {p['mean_range']} | {p['mean_reval']}"
                f" | {p['mean_drag']} | {p['mean_fills']} | {p['n_days']} |"
            )
        r = best["range_extremes"]
        print(
            f"\n[shadow_diurnal] loudest hour {r['loudest_hour_of_day']:02d}Z"
            f" (mean range {r['loudest_mean_range']}) vs quietest"
            f" {r['quietest_hour_of_day']:02d}Z ({r['quietest_mean_range']}) —"
            " read the LEVEL off mean_end_bal, never off mean_min and never"
            " off a ragged mean_end."
        )

        # Bound 6: the same hour-ends UNAVERAGED, because the column
        # above cannot distinguish a repeating cycle from three unlike
        # days that happen to average into one.
        days = best["by_day"]
        agree = best["shape_agreement"]
        print(f"\n[shadow_diurnal] hour-end by day — shape {agree['shape_verdict']}")
        print("| UTC | " + " | ".join(d["day"] for d in days) + " |")
        print("|---" * (len(days) + 1) + "|")
        for hod in range(24):
            cells = [d["hour_end"].get(f"{hod:02d}") for d in days]
            if all(c is None for c in cells):
                continue
            print(
                f"| {hod:02d}Z | " + " | ".join("—" if c is None else f"{c}" for c in cells) + " |"
            )
        for d in days:
            print(
                f"    {d['day']}: peak {d['peak']} at {d['peak_hour']:02d}Z,"
                f" trough {d['trough']} at {d['trough_hour']:02d}Z,"
                f" amplitude {d['amplitude']} over {d['n_whole_hours']} whole hours"
            )
        for pr in agree["pairs"]:
            rho = "UNSCORED" if pr["rho"] is None else f"rho {pr['rho']}"
            print(
                f"    {pr['days'][0]} vs {pr['days'][1]}: {rho}"
                f" over {pr['n_shared_hours']} shared hours"
            )
    print(f"\n[shadow_diurnal] written to {out}")


if __name__ == "__main__":
    main()
