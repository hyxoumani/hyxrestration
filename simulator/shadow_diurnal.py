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
  8. A PER-RUN VERDICT READ OFF THE NEWEST RUN IS THE WEAKEST READING
     THE LEDGER CONTAINS, AND IT IS THE ONE THE EYE TAKES. Measured
     2026-08-29: all six archived readings of this report were taken
     with `--run` on whatever run was live that day and all six printed
     UNDERPOWERED, while the ledger already held FOUR fully powered runs
     -- 20260713T064302 (4 days), 20260722T081852 (6), 20260803T142853
     (5), 20260810T081931 (11 days, 10 draws/hr, 55 day-pairs) -- every
     one of them reading DOES NOT REPEAT. Nobody saw them because the
     all-runs default path raised `TypeError` on the first run with no
     whole hour (`set.intersection` over an empty family) and so had
     never once completed. `power_census` is therefore published FIRST:
     it partitions the runs by `profile_status`, tallies `shape_status`
     over the POWERED runs only, holds the unscorable runs out of that
     partition rather than counting them as underpowered zeros, names
     the latest run and whether it is entitled to a claim, and flags the
     runs still writing. SHAPE is poolable across runs because rank
     correlation is computed within a run; LEVEL is not, because each
     run seeds a different book -- the census tallies no level term.
     Bound 6's open question is thereby ANSWERED on the powered subset:
     the daily shape does not recur. Not a verdict on any strategy:
     pre-registration still decides that.
  9. THE LEVEL COLUMN IS CUMULATIVE, SO A DRIFTING RUN PRINTS AN
     HOUR-OF-DAY SHAPE IT DOES NOT HAVE. `mean_equity_end_balanced`
     carries every hour before it within the day, so a run losing money
     at a steady rate falls monotonically down the clock whether or not
     any hour is special -- and the late-clock trough is then read as an
     hour-of-day effect. MEASURED 2026-08-29 on `20260810T081931`, the
     first run long enough to have a 9-day balanced panel: the raw column
     spans 453.1, the run's own drift is -375.0/day, and with that drift
     removed the hour-of-day shape spans only 293.6. `diurnal_level`
     therefore publishes the same panel DIFFERENCED -- per hour-of-day
     the mean close-to-close change, its deviation from the grand mean
     per hour, and the running sum of those deviations, which starts and
     ends at zero by construction. Deltas across a data outage are
     excluded (`contiguous`), because a delta covering four hours filed
     under one hour-of-day is exactly the value that would manufacture a
     spike. The per-hour sign test's ceiling is `LEVEL_FWER` divided by
     the hours tested: 24 hours are 24 chances, and an unadjusted 0.05
     finds a trough on noise better than half the time. That ceiling is
     0.00208, the strongest sign test a 9-day panel can produce is
     0.0039, and so `level_shape_status` reads UNDERPOWERED: 12Z is
     9 of 9 days below the leave-one-out centre at -125.7 and STILL cannot clear
     it. Ten panel days would. That is a statement about power, not a
     measured flat, and not a verdict on any strategy.
 10. AN HOUR-OF-DAY DEVIATION IS NOT A TRANSACTION COST UNTIL THE SPLIT
     SAYS SO. `d_equity = reval - entry_drag` holds per ROW and is
     exact, so it survives averaging and de-meaning with no residual:
     each hour's deviation from the grand mean is its reval deviation
     minus its drag deviation. `diurnal_level` therefore prints both
     terms beside the deviation, on the SAME rows -- the contiguous
     deltas of the balanced panel, never the profile's `mean_reval`
     column, which averages every whole hour of the run and would be
     bound 7's composition defect one axis over. MEASURED 2026-08-30 on
     `20260810T081931`: the -125.7/hr deviation at 12Z is reval -127.8
     against drag -2.11, i.e. ALL of it is a marking move on the book
     that was already standing, and 12Z is not even a busy fill hour
     (181.7 fills against 293.4 at 17Z). Across the whole clock the drag
     deviation spans 8.51 against reval's 187.3, and that span is the
     CEILING on any hour-of-day transaction-cost account of this shape --
     a 22x gap, not a close call. Being a marking
     story is what makes it interesting -- and it is also why the hour
     could not be traded by declining to trade it. The split needs no
     power because it is arithmetic, but it is not evidence the hour is
     real: the sign test of bound 9 is what decides that, and it still
     reads UNDERPOWERED. Where any contributing row has no reval
     (bound 1) the split is UNSCORABLE rather than averaged over the
     rows that do -- absent, not zero.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from math import comb
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
#: The three things a run's diurnal profile can be. `unscorable` is NOT
#: a weak `underpowered`: a run with no whole hour published no
#: hour-of-day mean at all, and counting it as underpowered would plot an
#: absent measurement as a measured zero (mistakes #32).
PROFILE_STATUSES = ("powered", "underpowered", "unscorable")
#: The three things the day-pair shape test can say. Same partition rule.
SHAPE_STATUSES = ("repeats", "does_not_repeat", "underpowered")
#: What the level PANEL can be. `no_panel` is absent, not flat.
PANEL_STATUSES = ("balanced", "ragged", "no_panel")
#: What the de-trended hour-of-day LEVEL test can say. Same partition
#: rule as everywhere else: `unscorable` is an absent measurement.
LEVEL_STATUSES = ("powered", "underpowered", "unscorable")
#: What the drag/reval split of the de-trended shape can say. It is an
#: exact identity rather than a test, so there is no `underpowered`
#: state -- either every contributing row has a reval or the split is
#: absent (bound 10).
SPLIT_STATUSES = ("scorable", "unscorable")
#: Family-wise error rate for the per-hour sign test. Divided by the
#: number of hours actually tested -- 24 hours of the clock are 24
#: chances to find a trough, and an unadjusted 0.05 finds one on noise
#: better than half the time.
LEVEL_FWER = 0.05
#: Every `*_verdict` this module publishes, with the UNIT its statuses
#: are counted in and the statuses it may take. Enumerated because the
#: comparable registries at `atlas.py` and `queuescore.py` exist and this
#: module's four publishers were not in one -- an unregistered verdict is
#: how a count gets plotted against readings that never tested it
#: (mistakes #32/#33/#35). `test_hyxlab_shadow_diurnal.py` walks the AST
#: for `*_verdict` keys and asserts this dict is exactly the population.
#: The census tallies only the `run`-unit ones: LEVEL is per-run because
#: each run seeds a different book, so it may never be pooled.
VERDICT_POPULATION: dict[str, tuple[str, tuple[str, ...]]] = {
    "profile_verdict": ("run", PROFILE_STATUSES),
    "shape_verdict": ("run", SHAPE_STATUSES),
    "level_verdict": ("hour_of_day", PANEL_STATUSES),
    "level_shape_verdict": ("panel_day", LEVEL_STATUSES),
    "level_split_verdict": ("panel_day", SPLIT_STATUSES),
}
#: A run whose last equity point is this recent is still WRITING: its
#: span will grow, so its status is a snapshot and not a settled draw.
OPEN_RUN_GRACE_MIN = 30


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
    #
    # Bound 9: a close-to-close delta is an HOUR's delta only when the two
    # buckets are ADJACENT. The daemon can be down, and the query returns
    # the surviving buckets adjacent in ROW order -- so a four-hour outage
    # hands the hour after it a delta covering four, filed under one
    # hour-of-day. `contiguous` marks the ones that are what they claim.
    for i in range(1, len(rows)):
        prev, cur = rows[i - 1], rows[i]
        d = cur["equity_end"] - prev["equity_end"]
        cur["d_equity"] = _r(d)
        cur["contiguous"] = (eq[i][0] - eq[i - 1][0]) == timedelta(hours=1)
        valid = set(cur["strategies"]) <= TIGHT_GATED
        cur["drag_model_valid"] = valid
        # reval is the residual: the move the standing book made, once
        # the new fills' entry cost is added back. Null, never wrong,
        # when the drag model does not apply (bound 1).
        cur["reval"] = _r(d + cur["entry_drag_modeled"]) if valid else None
    rows[0]["d_equity"] = None
    rows[0]["contiguous"] = False
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
    # A run can publish NO whole hours at all (the ledger holds 50 runs and
    # 14 of them are shorter than two hour boundaries). Intersecting an empty
    # family is not "every day" -- it is undefined, and `set.intersection(*[])`
    # raises. There is no panel, and the level column has nothing to be read
    # off; say that rather than crash or print a balanced-looking zero.
    panel = (
        sorted(set.intersection(*({r["day"] for r in rows} for rows in buckets.values())))
        if buckets
        else []
    )

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
    panel_status = "no_panel" if not table else "ragged" if ragged else "balanced"
    bal = [
        p["mean_equity_end_balanced"] for p in table if p["mean_equity_end_balanced"] is not None
    ]
    raw_span = _r(max(bal) - min(bal)) if bal else None
    return {
        "by_hour_of_day": table,
        **_level_decomposition(hours, panel, raw_span),
        "level_panel": {
            "days": panel,
            "ragged_hours": ragged,
            "panel_status": panel_status,
            "raw_level_span": raw_span,
            # The one sentence a reader of the level column needs.
            "level_verdict": (
                "NO PANEL (this run publishes no whole hour; nothing to read a"
                " LEVEL off, and no hour-of-day mean is defined)"
                if not table
                else f"BALANCED (every hour averages the same {len(panel)} day(s))"
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


def _sign_p(k: int, n: int) -> float:
    """Two-sided exact binomial sign p for `k` of `n` below the centre."""
    if n == 0:
        return 1.0
    m = min(k, n - k)
    return min(1.0, 2 * sum(comb(n, i) for i in range(m + 1)) / 2**n)


def _days_needed(ceiling: float) -> int:
    """Smallest per-hour draw count whose BEST sign p clears `ceiling`."""
    n = 2
    while 2.0 ** (1 - n) > ceiling:
        n += 1
    return n


def _level_split(
    buckets: dict[int, list[float]],
    terms: dict[int, list[tuple[float, float | None]]],
    table: list[dict],
) -> dict:
    """Split each hour-of-day's de-trended deviation into the two terms
    `d_equity` is made of (bound 10).

    Per row `d_equity = reval - entry_drag`, an EXACT identity, so it
    survives averaging and de-meaning: an hour's deviation from the grand
    mean is its reval deviation minus its drag deviation, with no
    residual. That makes the split readable at any power -- it is
    arithmetic on the same rows, not a second test -- which matters
    because the sign test above can sit UNDERPOWERED for days while the
    question "is this hour a marking move or a transaction cost?" is
    already answered.

    Refuses rather than guesses. `reval` is null wherever the entry-drag
    model does not apply (bound 1), and averaging the non-null subset
    would split a deviation computed over one set of rows using terms
    from a smaller one. One null row makes the whole split unscorable,
    and the count is published -- absent, not zero (mistakes #32).
    """
    rows = [t for vs in terms.values() for t in vs]
    missing = sum(1 for _, rv in rows if rv is None)
    if missing or not rows:
        for p in table:
            p["mean_drag"] = p["mean_reval"] = None
            p["demeaned_drag"] = p["demeaned_reval"] = None
            p["reval_share"] = p["carrier"] = None
        return {
            "level_split_status": "unscorable",
            "rows_without_reval": missing,
            "grand_mean_drag": None,
            "grand_mean_reval": None,
            "reval_carried_hours": None,
            "drag_deviation_span": None,
            "reval_deviation_span": None,
            "level_split_verdict": (
                f"UNSCORABLE ({missing} of {len(rows)} contributing hour(s) have"
                " no reval: the entry-drag model does not apply to their fills"
                " (bound 1), and averaging only the rows that do have one would"
                " split a deviation over rows the terms were not taken from."
                " Absent, not zero"
            ),
        }

    grand_drag = sum(d for d, _ in rows) / len(rows)
    grand_reval = sum(rv for _, rv in rows) / len(rows)
    carried = 0
    for p in table:
        ts = terms[p["hour_of_day"]]
        m_drag = sum(d for d, _ in ts) / len(ts)
        m_reval = sum(rv for _, rv in ts) / len(ts)
        d_drag, d_reval = m_drag - grand_drag, m_reval - grand_reval
        p["mean_drag"] = _r(m_drag, 2)
        p["mean_reval"] = _r(m_reval)
        p["demeaned_drag"] = _r(d_drag, 2)
        p["demeaned_reval"] = _r(d_reval)
        dev = m_reval - m_drag - (grand_reval - grand_drag)
        p["reval_share"] = round(d_reval / dev, 3) if dev else None
        p["carrier"] = "reval" if abs(d_reval) >= abs(d_drag) else "drag"
        carried += p["carrier"] == "reval"

    # The spans are the ceiling on each story: whatever the shape is, no
    # hour-of-day TRANSACTION-COST explanation of it can be larger than
    # the drag term's own span across the clock.
    d_span = _r(max(p["demeaned_drag"] for p in table) - min(p["demeaned_drag"] for p in table), 2)
    r_span = _r(max(p["demeaned_reval"] for p in table) - min(p["demeaned_reval"] for p in table))
    strongest = min(table, key=lambda p: p["sign_p"])
    share = strongest["reval_share"]
    verdict = (
        f"REVAL carries the hour-of-day shape at {carried} of {len(table)}"
        f" hours. At the strongest hour {strongest['hour_of_day']:02d}Z the"
        f" {strongest['demeaned']:+g}/hr deviation is reval"
        f" {strongest['demeaned_reval']:+g} against drag"
        f" {strongest['demeaned_drag']:+g}"
        + (f" ({share:.0%} reval)" if share is not None else "")
        + " -- a MARKING move on the standing book, not what that hour's"
        f" fills cost on the way in. Across the clock the drag deviation"
        f" spans {d_span} against reval's {r_span}, which is the CEILING on"
        " any hour-of-day transaction-cost explanation of this shape. Exact"
        " identity per row, so it needs no power; which term carries an hour"
        " does not make the hour real, and the sign test above decides that."
    )
    return {
        "level_split_status": "scorable",
        "rows_without_reval": 0,
        "grand_mean_drag": _r(grand_drag, 2),
        "grand_mean_reval": _r(grand_reval),
        "reval_carried_hours": carried,
        "drag_deviation_span": d_span,
        "reval_deviation_span": r_span,
        "level_split_verdict": verdict,
    }


def _level_decomposition(hours: list[dict], panel: list[str], raw_span: float | None) -> dict:
    """The hour-of-day LEVEL column with the run's own drift taken out
    (bound 9).

    `mean_equity_end_balanced` is a CUMULATIVE quantity: within a day it
    carries every hour before it. A run that loses money at a steady rate
    therefore prints a column that falls monotonically down the clock
    whether or not any hour of the day is special, and the eye reads the
    late-clock trough as an hour-of-day effect. The decomposition below
    is the same panel differenced: per hour-of-day, the mean of the
    close-to-close CHANGE, its deviation from the run's grand mean per
    hour, and the running sum of those deviations -- which is the level
    column with a constant drift removed and so starts and ends at zero
    by construction.

    Three things it is careful about, all of which cut against the
    finding rather than for it:

      * ONLY CONTIGUOUS deltas. A delta across an outage is not an
        hour's, and it is exactly the kind of large value that would
        manufacture a spike at whatever hour-of-day the gap ended on.
      * THE CEILING IS DIVIDED BY THE HOURS TESTED. Twenty-four hours are
        twenty-four chances; an unadjusted 0.05 per hour finds a
        "significant" hour on pure noise more often than not.
      * THE PANEL CAN BE TOO SHORT TO SAY ANYTHING AT ALL. With `n`
        draws the strongest sign test possible is `2^(1-n)`, so below
        `_days_needed(ceiling)` days NO hour can clear the ceiling even
        if every day agrees. That reads UNDERPOWERED -- distinct from a
        measured flat, and it is the state this ledger is in.

    The sign test centres each hour on the mean of the draws NOT in that
    hour: an hour tested against a centre it helped set is partly tested
    against itself, and one loud hour moves a pooled centre far enough
    that all twenty-three others come out "significantly" on the other
    side of it. `cum_demeaned` still uses the pooled grand mean, because
    that is what makes the column a decomposition that sums to zero.
    Within one day the deltas sum to that day's change and so are
    negatively coupled, but the sign test runs ACROSS days at a fixed
    hour, and days are the draws.
    """
    on_panel = set(panel)
    buckets: dict[int, list[float]] = defaultdict(list)
    # Bound 10: the same rows, carrying the two terms d_equity is made of.
    # Collected here rather than read off `by_hour_of_day` because that
    # column averages EVERY whole hour of the run and this one averages
    # the contiguous deltas on the balanced panel -- different samples,
    # and differencing one against the other is bound 7's defect again.
    terms: dict[int, list[tuple[float, float | None]]] = defaultdict(list)
    non_contiguous = 0
    for h in hours:
        if h["partial"] or h["day"] not in on_panel or h["d_equity"] is None:
            continue
        if not h["contiguous"]:
            non_contiguous += 1
            continue
        buckets[h["hour_of_day"]].append(h["d_equity"])
        terms[h["hour_of_day"]].append((h["entry_drag_modeled"], h["reval"]))

    draws = [v for vs in buckets.values() for v in vs]
    hours_tested = len(buckets)
    if not draws:
        return {
            "diurnal_level": {
                "n_panel_days": len(panel),
                "hours_tested": 0,
                "non_contiguous_deltas_excluded": non_contiguous,
                "grand_mean_per_hour": None,
                "drift_per_day": None,
                "clock_complete": False,
                "raw_level_span": raw_span,
                "detrended_span": None,
                "sign_p_ceiling": None,
                "best_achievable_sign_p": None,
                "panel_days_needed": None,
                "by_hour_of_day": [],
                "significant_hours": [],
                "level_shape_status": "unscorable",
                "level_shape_verdict": (
                    "UNSCORABLE (no contiguous hour-to-hour change lands on the"
                    " level panel; there is nothing to decompose, which is not"
                    " the same as a flat clock)"
                ),
                "level_split_status": "unscorable",
                "rows_without_reval": 0,
                "grand_mean_drag": None,
                "grand_mean_reval": None,
                "reval_carried_hours": None,
                "drag_deviation_span": None,
                "reval_deviation_span": None,
                "level_split_verdict": (
                    "UNSCORABLE (no contiguous change to split; there are no"
                    " rows, which is not a shape carried by neither term)"
                ),
            }
        }

    grand = sum(draws) / len(draws)
    others = {
        hod: sorted(v for h, vs in buckets.items() if h != hod for v in vs) for hod in buckets
    }
    ceiling = LEVEL_FWER / hours_tested
    table = []
    cum = 0.0
    for hod in sorted(buckets):
        vs = buckets[hod]
        mean = sum(vs) / len(vs)
        cum += mean - grand
        # The centre is the MEDIAN of every draw NOT in this hour. Two
        # separate reasons, both learned the hard way:
        #   * leave one out, because an hour tested against a centre it
        #     helped set is partly tested against itself; and
        #   * a median, because ONE loud hour moves a MEAN centre far
        #     enough that all twenty-three others come out significantly
        #     on the other side of it -- the outlier does not just fail
        #     to be isolated, it makes every quiet hour a finding.
        rest = others[hod]
        centre = (
            (rest[len(rest) // 2] if len(rest) % 2 else sum(rest[len(rest) // 2 - 1 :][:2]) / 2)
            if rest
            else grand
        )
        # A draw exactly ON the centre is not evidence either way, and
        # counting it as "not below" is how a run with a perfectly
        # constant drift reads 0-of-n below at every hour of the clock
        # and prints 24 significant hours. Ties are dropped and the
        # effective count is published beside the raw one.
        below = sum(1 for v in vs if v < centre)
        above = sum(1 for v in vs if v > centre)
        table.append(
            {
                "hour_of_day": hod,
                "n_days": len(vs),
                "mean_d_equity": _r(mean),
                "demeaned": _r(mean - grand),
                "cum_demeaned": _r(cum),
                "centre": _r(centre),
                "n_below_centre": below,
                "n_above_centre": above,
                "n_tied": len(vs) - below - above,
                "n_effective": below + above,
                "sign_p": round(_sign_p(below, below + above), 6),
            }
        )

    split = _level_split(buckets, terms, table)
    max_draws = max(p["n_effective"] for p in table)
    best_p = 2.0 ** (1 - max_draws)
    needed = _days_needed(ceiling)
    status = "underpowered" if best_p > ceiling else "powered"
    sig = [p for p in table if p["sign_p"] <= ceiling]
    cums = [p["cum_demeaned"] for p in table]
    detrended_span = _r(max(cums) - min(cums))
    strongest = min(table, key=lambda p: p["sign_p"])
    drift = _r(sum(p["mean_d_equity"] for p in table))

    if status == "underpowered":
        verdict = (
            f"UNDERPOWERED ({max_draws} draw(s) at the best-sampled hour; the"
            f" strongest sign test this panel can produce is p={best_p:.4f} >"
            f" {ceiling:.5f}, the {LEVEL_FWER} ceiling over {hours_tested} hours"
            f" tested, so NO hour can clear it even if every day agrees --"
            f" {needed} untied panel days are needed). The shape is still worth"
            f" reading: the raw level column spans {raw_span}, and with the"
            f" run's own {drift}/day drift removed the hour-of-day shape spans"
            f" {detrended_span}; strongest hour"
            f" {strongest['hour_of_day']:02d}Z at {strongest['n_below_centre']}"
            f"/{strongest['n_days']} days below the leave-one-out centre"
            f" (p={strongest['sign_p']:g}). Not a claim."
        )
    elif sig:
        named = ", ".join(
            f"{p['hour_of_day']:02d}Z {p['demeaned']:+g} (p={p['sign_p']:g})" for p in sig
        )
        verdict = (
            f"HOUR-OF-DAY LEVEL EFFECT at {len(sig)} of {hours_tested} hours"
            f" tested, sign p <= {ceiling:.5f}: {named}. The raw level column"
            f" spans {raw_span}; with the run's own {drift}/day drift removed"
            f" the hour-of-day shape spans {detrended_span}."
        )
    else:
        verdict = (
            f"FLAT (no hour of {hours_tested} clears sign p {ceiling:.5f} over"
            f" up to {max_draws} untied days; strongest is"
            f" {strongest['hour_of_day']:02d}Z at p={strongest['sign_p']:g}). The"
            f" raw level column's {raw_span} span is the run's own {drift}/day"
            f" drift accumulating down the clock, not an hour-of-day effect:"
            f" de-trended it spans {detrended_span}."
        )

    return {
        "diurnal_level": {
            "n_panel_days": len(panel),
            "hours_tested": hours_tested,
            "non_contiguous_deltas_excluded": non_contiguous,
            "grand_mean_per_hour": _r(grand),
            # Only a DAY when the whole clock is present; a partial clock
            # sums to a partial day and must not be read as a daily rate.
            "drift_per_day": drift,
            "clock_complete": hours_tested == 24,
            "raw_level_span": raw_span,
            "detrended_span": detrended_span,
            "sign_p_ceiling": round(ceiling, 6),
            "best_achievable_sign_p": round(best_p, 6),
            "panel_days_needed": needed,
            "by_hour_of_day": table,
            "significant_hours": [p["hour_of_day"] for p in sig],
            "level_shape_status": status,
            "level_shape_verdict": verdict,
            **split,
        }
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
        status = "underpowered"
        verdict = (
            f"UNDERPOWERED ({len(scored)} scorable day-pair(s) at"
            f" >= {MIN_SHARED_HOURS} shared hours; no shape claim)"
        )
    elif min(p["rho"] for p in scored) >= SHAPE_RHO:
        status = "repeats"
        verdict = (
            f"REPEATS (all {len(scored)} day-pairs rho >="
            f" {min(p['rho'] for p in scored)} >= {SHAPE_RHO})"
        )
    else:
        status = "does_not_repeat"
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
            # The prose sentence is for a reader; the status is what the
            # across-run census tallies. Never re-derive one from the
            # other by parsing -- they are published side by side.
            "shape_status": status,
            "n_scored_pairs": len(scored),
            "shape_verdict": verdict,
        },
    }


def power_census(runs: list[dict]) -> dict:
    """Which runs in the ledger actually answer the shape question.

    Why this exists (2026-08-29). Every archived reading of this report
    was taken with `--run` on whatever run was live that day, and every
    one of them printed UNDERPOWERED -- because the newest run is always
    the shortest. The all-runs default path, which would have shown the
    rest of the ledger, raised `TypeError` on the first run with no whole
    hour and so had never once completed. The consequence, measured: FOUR
    runs (20260713T064302, 20260722T081852, 20260803T142853,
    20260810T081931 -- up to 11 whole days and 55 day-pairs) were fully
    powered and unread, while six status passes carried "underpowered,
    wait for more days" off the two runs that were not.

    So the defect here is not across READINGS the way #32/#35 were at the
    atlas and the queue-score; it is across RUNS inside a single reading.
    The report published all 51 partitions and the eye took the last one,
    and "UNDERPOWERED" is the identical sentence for a run six hours old
    and for a ledger that has answered the question four times.

    What may and may not be pooled, stated rather than left to the reader:

      * SHAPE is poolable. Each run's `shape_status` is an independent
        draw on "does the diurnal cycle recur", computed from rank
        correlations WITHIN that run, so it carries across seedings.
      * LEVEL is not. Each run seeds a different book at a different
        time, so `mean_equity_end` is on a different scale per run. This
        census tallies no level term and must not be extended to one.

    Counts carry their units (mistakes #35): a class count moves both
    because statuses changed and because runs entered the ledger under
    them, so `runs` lists each member with the span and draw count that
    put it there. `unscorable` runs are held OUT of the profile partition
    rather than counted as underpowered zeros (mistakes #32).
    """
    scorable = [r for r in runs if r["validity"]["profile_status"] != "unscorable"]
    unscorable = [r for r in runs if r["validity"]["profile_status"] == "unscorable"]

    def _unit(r: dict) -> dict:
        return {
            "run_id": r["run_id"],
            "n_days": r["n_days"],
            "min_draws_per_hour": r["min_draws_per_hour"],
            "n_scored_pairs": r["shape_agreement"]["n_scored_pairs"],
            "shape_status": r["shape_agreement"]["shape_status"],
            "open": r["open"],
        }

    powered = [r for r in scorable if r["validity"]["profile_status"] == "powered"]
    latest = runs[-1] if runs else None
    return {
        "rule": (
            "SHAPE is poolable across runs and LEVEL is not; a run with no"
            " whole hour is absent from the profile partition, not an"
            " underpowered zero in it; every count is read against the spans"
            " listed beside it, never on its own"
        ),
        "runs_published": len(runs),
        "unscorable": {
            "n": len(unscorable),
            "run_ids": [r["run_id"] for r in unscorable],
            "why": "no whole hour: nothing to be powered or underpowered about",
        },
        "profile": {
            "scorable": len(scorable),
            "counts": {
                st: sum(1 for r in scorable if r["validity"]["profile_status"] == st)
                for st in PROFILE_STATUSES
                if st != "unscorable"
            },
            "powered_runs": [_unit(r) for r in powered],
        },
        # The answer the ledger already holds, restricted to the runs that
        # are entitled to give one. A shape status from an underpowered
        # profile is not evidence and is not counted here.
        "shape_among_powered": {
            "n": len(powered),
            "counts": {
                st: sum(1 for r in powered if r["shape_agreement"]["shape_status"] == st)
                for st in SHAPE_STATUSES
            },
            "total_scored_pairs": sum(r["shape_agreement"]["n_scored_pairs"] for r in powered),
            "days_span": (
                [min(r["n_days"] for r in powered), max(r["n_days"] for r in powered)]
                if powered
                else None
            ),
        },
        "open_runs": [r["run_id"] for r in runs if r["open"]],
        # Published because reading the newest run is exactly how the four
        # powered runs went unseen for nine days.
        "latest_run": (
            {
                "run_id": latest["run_id"],
                "profile_status": latest["validity"]["profile_status"],
                "shape_status": latest["shape_agreement"]["shape_status"],
                "open": latest["open"],
                "is_powered": latest["validity"]["profile_status"] == "powered",
            }
            if latest is not None
            else None
        ),
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
    now = datetime.now(UTC)
    last_ts = {
        r[0]: r[1]
        for r in ledger.execute(
            "select run_id, max(ts) from shadow_equity"
            + (" where run_id = ?" if run_id else "")
            + " group by 1",
            [run_id] if run_id else [],
        ).fetchall()
    }
    report: dict = {
        "generated_at": f"{now:%Y-%m-%dT%H:%M:%SZ}",
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
        # A run with no whole hour is UNSCORABLE, not underpowered: the
        # `min(..., default=0)` below would otherwise report "weakest hour
        # has 0 draws", which reads as a measured zero for a measurement
        # that was never taken (mistakes #32).
        profile_status = (
            "unscorable"
            if not prof["by_hour_of_day"]
            else "underpowered"
            if min_draws < MIN_DAYS
            else "powered"
        )
        dense_prof = [p for p in prof["by_hour_of_day"] if p["mean_range"] is not None]
        loudest = max(dense_prof, key=lambda p: p["mean_range"], default=None)
        quietest = min(dense_prof, key=lambda p: p["mean_range"], default=None)
        report["runs"].append(
            {
                "run_id": rid,
                # An OPEN run is still writing: its span will grow, so its
                # status is a snapshot, not a settled draw. Read off the
                # ledger rather than assumed from run_id ordering, because
                # the newest run_id is not necessarily the live one.
                "open": (now - last_ts[rid].replace(tzinfo=UTC)).total_seconds()
                < OPEN_RUN_GRACE_MIN * 60,
                "last_equity_at": f"{last_ts[rid]:%Y-%m-%dT%H:%M:%SZ}",
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
                    "min_days": MIN_DAYS,
                    "profile_status": profile_status,
                    "profile_verdict": (
                        "UNSCORABLE (this run publishes no whole hour; it has no"
                        " hour-of-day mean to be powered or underpowered about)"
                        if profile_status == "unscorable"
                        else f"UNDERPOWERED (weakest hour has {min_draws} <"
                        f" {MIN_DAYS} draws; run spans {n_days} whole days)"
                        if profile_status == "underpowered"
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
    report["power_census"] = power_census(report["runs"])
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

    # The census FIRST. The failure this report had was that the eye went
    # to one run's verdict, and a per-run verdict printed alone is the
    # same sentence for a six-hour run and for a ledger that has answered
    # the question four times.
    c = report["power_census"]
    pw = c["shape_among_powered"]
    print(
        f"[shadow_diurnal] census — {c['runs_published']} run(s) published:"
        f" {c['profile']['counts']['powered']} powered,"
        f" {c['profile']['counts']['underpowered']} underpowered,"
        f" {c['unscorable']['n']} unscorable (no whole hour, NOT a zero)"
    )
    if pw["n"]:
        tally = ", ".join(f"{k} {v}" for k, v in pw["counts"].items() if v)
        print(
            f"[shadow_diurnal] shape over the {pw['n']} POWERED run(s)"
            f" ({pw['days_span'][0]}-{pw['days_span'][1]} whole days,"
            f" {pw['total_scored_pairs']} scored day-pairs): {tally}"
        )
        for u in c["profile"]["powered_runs"]:
            print(
                f"    {u['run_id']}: {u['shape_status']} over {u['n_days']} days,"
                f" {u['min_draws_per_hour']} draws/hr, {u['n_scored_pairs']} pairs"
            )
    lr = c["latest_run"]
    if lr is not None:
        print(
            f"[shadow_diurnal] latest run {lr['run_id']} is"
            f" {lr['profile_status']}{' and OPEN' if lr['open'] else ''} — it is"
            f" {'' if lr['is_powered'] else 'NOT '}entitled to a shape claim;"
            " LEVEL is not poolable across runs, SHAPE is.\n"
        )

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
        # The level column above is CUMULATIVE, so a run that loses money
        # at a steady rate prints a monotone fall down the clock whether
        # or not any hour is special. Print the same panel DIFFERENCED
        # right underneath it, so the drift and the shape are never read
        # off one number (bound 9).
        dl = best["diurnal_level"]
        print(f"\n[shadow_diurnal] de-trended level — {dl['level_shape_verdict']}")
        # Bound 10: the same deviation split into the two terms it is
        # made of, in the same row, because "12Z is down 125" and "12Z's
        # fills cost 125" are different findings and only one of them is
        # a transaction-cost story.
        print(f"[shadow_diurnal] split — {dl['level_split_verdict']}")
        if dl["by_hour_of_day"]:
            print(
                "| UTC | mean_d_equity | demeaned | d_reval | d_drag | carrier"
                " | cum_demeaned | days | below | sign_p |"
            )
            print("|---" * 10 + "|")
            for q in dl["by_hour_of_day"]:
                print(
                    f"| {q['hour_of_day']:02d}Z | {q['mean_d_equity']} | {q['demeaned']}"
                    f" | {q['demeaned_reval']} | {q['demeaned_drag']} | {q['carrier'] or '—'}"
                    f" | {q['cum_demeaned']} | {q['n_days']}"
                    f" | {q['n_below_centre']}/{q['n_effective']} | {q['sign_p']:g} |"
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
