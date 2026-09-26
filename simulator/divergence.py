"""Shadow-vs-replay divergence report: the fill-model calibration haircut.

    python -m simulator.divergence [--run RUN_ID] [--anchor ISO_TS]

Defaults to the newest FINISHED shadow run that produced fills, so
that re-running the report measures newly accumulated evidence
(see `latest_complete_run`).

Replays the exact stream-archive window a shadow run traded — same
seeding procedure, same strategy, same latency model — and compares the
two fill streams. Shadow decided while the future didn't exist; the
replay decides over the identical recording. Any disagreement is
therefore infrastructure, not market: late-arriving archive rows,
coverage gaps unknown to the live run (e.g. retro-marked flush
failures), or fill-model asymmetries. The signed price delta per
contract is the haircut to apply to every backtest number.

Equity is deliberately NOT compared in v1: replay sees today's
settlement metadata for markets that were unresolved while shadow ran,
so P&L differences would conflate resolution knowledge with fill
quality. Fills are the honest common currency.

Output: printed summary + reports/shadow_divergence/{run_id}.json.
"""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median

from hyxlab.importclosure import closure_sha
from hyxlab.reportdir import shared_reports
from hyxlab.scratch import duck_scratch_dir
from hyxlab.shadowruns import latest_complete_run
from hyxlab.store import attach_wait_block, connect_retry, open_retry, reset_attach_waits
from simulator.bookreplay import (
    BOOK_GAPS,
    export_events,
    replay_snapshots,
    stream_exported,
)
from simulator.registry import STRATEGIES
from simulator.shadow import SHADOW_DB, STREAM_DB
from simulator.sim import Simulator

MATCH_TOLERANCE = timedelta(seconds=60)
NEAREST_WINDOW = timedelta(seconds=2)
_QTY_EPS = 1e-9
# The stream archive is held read-write by `collector.streamd`, which
# never stops. `connect_retry`'s default 15 x 2.0s = 30s budget is for a
# brief reader: measured against the daemon, FOUR OF FIVE attempts died
# on a clean IOException after 30s even though the lock samples free 87%
# of the time. These give ~10.5 min of growing-interval attempts, which
# also detunes the retry from the daemon's flush period.
STREAM_ATTACH = {"retries": 30, "delay": 1.0, "backoff": 1.4, "max_delay": 30.0}


def replay_run(
    run_id: str,
    anchor: datetime,
    end: datetime,
    latency: float,
    strategy_names: list[str],
    stream_db: str = STREAM_DB,
    archive_db: str = "data/hyxlab.duckdb",
    return_gaps: bool = False,
) -> list:
    """Reproduce a shadow run offline; returns the replay's fills.

    With `return_gaps=True` returns `(fills, gaps)` so the caller can
    classify unmatched fills against the same coverage breaks the replay
    traded through.
    """
    # Every read of the stream archive happens inside this block, and the
    # replay happens OUTSIDE it. See the copy-out note in
    # `simulator.bookreplay`: a streaming cursor cannot release the file it
    # reads, so replaying through one held `hyxstream.duckdb` for 45m45s on
    # 2026-09-25 and cost `collector.streamd` 387,856 rows to the sidecar.
    # Measured cost of the copy that replaces it: 6.2 s and 0.89 GB.
    scratch = Path(duck_scratch_dir(stream_db) or ".") / f"divergence-{run_id}"
    try:
        with connect_retry(stream_db, **STREAM_ATTACH) as conn:
            floor = conn.execute(
                f"SELECT max(ended_at) FROM stream_gaps WHERE ended_at <= ? AND {BOOK_GAPS}",
                [anchor],
            ).fetchone()[0]
            # Metadata for the markets THIS REPLAY CAN TOUCH, and no others
            # — derived exactly as `run_l2` derives it (EXP-1378, EXP-1379).
            # `store.markets()` unfiltered is 1.87M MarketInfo objects, 1.32
            # GiB resident, sized by the ARCHIVE rather than by the window
            # the operator asked for, and this report is the one-shot that
            # holds it LONGEST: a 10.5-day replay of a shadow run. The id
            # set is a fact the stream archive owns, so it is derived here;
            # the `hyxlab.duckdb` attach that consumes it runs after this
            # block, because the two archives no longer have to be held at
            # the same time.
            # The floor is INCLUSIVE for the same reason the seed walk below
            # is (`lo_inclusive`): the reconnect image at a gap end is a real
            # market of this replay. `end`, not `anchor`, is the upper bound
            # — the traded window runs past the seed.
            ids = [
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT market_id FROM book_events"
                    " WHERE venue='kalshi' AND recv_ts >= ? AND recv_ts <= ?",
                    [floor or datetime.min, end],
                ).fetchall()
            ]
            # Gap rows are read BEFORE the release, not after: they are the
            # last thing this report needs from the stream archive, and
            # reading them later would re-open it (including gap rows the
            # live run never saw, e.g. flush_failure_backfill).
            gaps = conn.execute(
                f"SELECT started_at, ended_at FROM stream_gaps"
                f" WHERE ended_at > ? AND started_at <= ? AND {BOOK_GAPS}"
                f" ORDER BY started_at",
                [anchor, end],
            ).fetchall()
            # `lo_inclusive`: the floor is a gap's `ended_at`, and a seq_reset
            # gap ends AT the reconnect image that re-seeds books — excluding
            # it made this report replay a hole the daemon never had.
            seed_dir = export_events(
                conn, floor or datetime.min, anchor, scratch / "seed", lo_inclusive=True
            )
            window_dir = export_events(conn, anchor, end, scratch / "window")
        # --- the stream archive is released HERE, and streamd can flush ---

        # `open_retry`, not a bare `Store`: this attaches the LIVE
        # archive, whose write lock is taken by the 5-minute collector
        # and held for ~7h by the poly sweep. A bare read-only attach
        # dies instantly on the collision -- which is exactly how this
        # report failed on 2026-08-25 08:58Z, against poly_sweep at
        # 4h43m. `run_l2` (the module that copied this function's
        # seeding discipline) already used the retrying helper; this
        # site never got it. The shadow daemon takes the third valid
        # option and DEGRADES (`except duckdb.Error: return None`),
        # which a one-shot report cannot do.
        store = open_retry(archive_db, read_only=True)
        try:
            markets = store.markets(venue="kalshi", market_ids=ids)
        finally:
            store.close()
        sim = Simulator(markets, [STRATEGIES[n]() for n in strategy_names], latency=latency)

        from simulator.bookreplay import BookReplayer

        replayer = BookReplayer()
        # Seed books exactly as shadow does: replay history since the
        # last coverage break WITHOUT stepping the sim.
        for _ in replay_snapshots(stream_exported(seed_dir), replayer=replayer):
            pass
        # Trade the window with full gap honesty.
        for snap in replay_snapshots(stream_exported(window_dir), gaps=gaps, replayer=replayer):
            sim.step(snap)
            # Same bound the shadow daemon applies (simulator/shadow.py),
            # for the same reason, at the site that never got it: the sim
            # appends one equity point PER SNAPSHOT (~2.3M/day at stream
            # rates), and this replay covers whatever the longest run was
            # -- ~24M points over the 10.5-day default target. Divergence
            # compares FILLS and never calls `finalize()`, so it never
            # reads the curve at all; max_drawdown is a running stat, so
            # trimming cannot change any number this report prints.
            del sim.result.equity_curve[:-1]
    finally:
        # Not a cache: an export outlives nothing. `hyxlab.scratch`'s
        # reaper is the backstop for the exits that run no `finally` at
        # all (OOM kill, SIGKILL).
        shutil.rmtree(scratch, ignore_errors=True)
    fills = [f for f in sim.result.fills if f.ts <= end]
    return (fills, gaps) if return_gaps else fills


def compare(
    shadow_fills: list[tuple],
    replay_fills: list,
    window: timedelta = NEAREST_WINDOW,
    *,
    anchor: datetime | None = None,
    end: datetime | None = None,
    gaps: list | None = None,
) -> dict:
    """Tiered per-(market, side) matching; price deltas per tier.

    Tier priority: exact (the v1 matcher, byte-for-byte unchanged:
    equal qty within 60s, greedy in time order) claims fills FIRST, so
    a clean window reports identically to v1 — the relaxed tiers only
    ever see exact's leftovers. Split-aware runs before nearest because
    the nearest key is qty-free and would otherwise consume one leg of
    a partial-fill group. Split does not invent agreement: it demands
    partials at the same price summing exactly to the single fill.
    NEAREST DOES, ON QUANTITY, AND CAN DO NOTHING ELSE: every pair it
    is able to make disagrees on qty, because a replay fill still in
    `r_left` was visible to every shadow fill that became a leftover
    and the only predicate that can have refused it inside the nearest
    window is `r[1] == s[1]` (see `nearest_qty_delta`). Both relaxed
    tiers are confined to `window` (default 2s) and counted separately
    in the report so a calibration read never silently mixes tiers.

    shadow_fills rows: (market_id, side, qty, price, fee, maker, ts).

    When `anchor`/`end`/`gaps` are supplied the leftover (unmatched)
    fills — the ones no tier could pair — are classified by cause so the
    "boundary/coverage, not price disagreement" reading is verified, not
    inferred from the count gap: `boundary` (within the 60s exact
    tolerance of the window edge, so a true counterpart falls just
    outside the compared window), `gap` (inside a coverage break ±window,
    where the two streams re-seed differently), `reseed_twin` (an exact
    (market, side, qty, price) counterpart exists elsewhere in the
    OPPOSITE stream, just time-shifted beyond the match window — the
    start-of-run seed-settling signature, where both streams produce the
    identical fill at offset moments because their seeded books have not
    yet converged), else `unexplained` — a fill one stream produced and
    the other never did in any form, which is the only place a hidden
    fill-model discrepancy could hide. The twin test is existence-only
    (it does not net counts), so `reseed_twin` asserts an identical fill
    exists opposite, not that fill counts balance exactly. Samples of any
    unexplained fills are emitted so a nonzero count is never silent.

    `nearest_unpaired_dt` sizes the WINDOW itself. The shipped
    `nearest_dt_abs_mean_s` averages the pairs the window admitted, so
    it is bounded by the window edge by construction and cannot report a
    pairing the window missed; the census measures, for each unpaired
    fill, the |dt| to its nearest same-(market, side, price) counterpart
    among the opposite stream's OTHER unpaired fills with NO window
    bound, plus the count of unpaired fills that have no such
    counterpart at any dt.

    `nearest_qty_delta` publishes the size gap the nearest tier absorbs.
    It is the tier's ONLY divergence: its price delta is 0 by
    construction and its |dt| is bounded by the window, so without this
    census a nearest match is indistinguishable in the report from an
    exact one while representing a strictly worse agreement.
    """
    from collections import defaultdict

    s_by, r_by = defaultdict(list), defaultdict(list)
    for m, side, qty, price, fee, maker, ts in shadow_fills:
        s_by[(m, side)].append([ts, qty, price, fee, maker])
    for f in replay_fills:
        r_by[(f.market_id, f.side)].append([f.ts, f.qty, f.price, f.fee, f.maker])

    # Tier 1 — exact (v1 semantics, unchanged): equal qty within 60s,
    # greedy over time-sorted fills. Leftovers feed the relaxed tiers.
    matched, deltas = 0, []
    s_left, r_left = defaultdict(list), defaultdict(list)
    for key in sorted(s_by.keys() | r_by.keys()):
        r_list = sorted(r_by.get(key, []))
        for s in sorted(s_by.get(key, [])):
            best = None
            for i, r in enumerate(r_list):
                if abs(r[0] - s[0]) <= MATCH_TOLERANCE and r[1] == s[1]:
                    best = i
                    break
            if best is not None:
                r = r_list.pop(best)
                matched += 1
                deltas.append(r[2] - s[2])  # replay price - shadow price
            else:
                s_left[key].append(s)
        r_left[key] = r_list

    # Tier split — N partials at one price on one side summing exactly
    # to a single leftover fill on the other, all within `window` of it.
    def _claim_group(single, cands):
        """Earliest contiguous time-sorted run (>=2 fills, same price,
        inside `window` of `single`) whose qtys sum to single's qty."""
        elig = sorted(c for c in cands if c[2] == single[2] and abs(c[0] - single[0]) <= window)
        for i in range(len(elig)):
            total = 0.0
            for j in range(i, len(elig)):
                total += elig[j][1]
                if total > single[1] + _QTY_EPS:
                    break
                if abs(total - single[1]) <= _QTY_EPS and j > i:
                    return elig[i : j + 1]
        return None

    split_groups, split_s_fills, split_r_fills, split_deltas = 0, 0, 0, []
    for key in sorted(s_left.keys() | r_left.keys()):
        for singles, parts, sign in (
            (s_left[key], r_left[key], +1),  # 1 shadow <- N replay
            (r_left[key], s_left[key], -1),  # 1 replay <- N shadow
        ):
            for single in list(singles):
                grp = _claim_group(single, parts)
                if grp is None:
                    continue
                singles.remove(single)
                for g in grp:
                    parts.remove(g)
                split_groups += 1
                split_s_fills += 1 if sign > 0 else len(grp)
                split_r_fills += len(grp) if sign > 0 else 1
                split_deltas.append(sign * (grp[0][2] - single[2]))  # 0 by construction

    # Tier nearest — one-to-one on the remainder: same (market, side,
    # price), smallest |dt| within `window` first; ties by earliest ts.
    nearest_pairs = []
    for key in sorted(s_left.keys() & r_left.keys()):
        cands = sorted(
            (abs(r[0] - s[0]), s[0], r[0], si, ri)
            for si, s in enumerate(s_left[key])
            for ri, r in enumerate(r_left[key])
            if r[2] == s[2] and abs(r[0] - s[0]) <= window
        )
        used_s, used_r = set(), set()
        for dt, _sts, _rts, si, ri in cands:
            if si in used_s or ri in used_r:
                continue
            used_s.add(si)
            used_r.add(ri)
            nearest_pairs.append((dt, s_left[key][si], r_left[key][ri]))
    n_nearest = len(nearest_pairs)
    nearest_deltas = [r[2] - s[2] for _, s, r in nearest_pairs]  # 0 by construction
    # The tier's only real divergence. Its price delta is 0 by selection and
    # its |dt| is bounded by `window`, so a nearest match otherwise reads in
    # the report exactly like an exact one -- while standing for a strictly
    # weaker agreement. And it is a SIZE disagreement every time: the exact
    # tier holds one candidate list per key across its whole greedy pass and
    # only ever POPS matches from it, so every replay fill still in `r_left`
    # was visible to every shadow fill that became a leftover. Inside the
    # nearest window (2s, a subset of exact's 60s MATCH_TOLERANCE) the one
    # predicate that can have refused the pair is `r[1] == s[1]`. So
    # `equal_qty` is 0 whenever `window <= MATCH_TOLERANCE`; a hand-widened
    # `--nearest-window` past 60s lapses the invariant, and that is what the
    # field is for (mistakes #68).
    nearest_qty_deltas = sorted(r[1] - s[1] for _, s, r in nearest_pairs)  # replay - shadow
    all_deltas = deltas + split_deltas + nearest_deltas

    def _abs_mean(vals):
        return round(sum(abs(v) for v in vals) / len(vals), 6) if vals else None

    # qty-weighted overlap: bucket each side's quantity by minute and
    # credit min(shadow, replay) per bucket — split fills (5 vs 3+2)
    # count as matched quantity instead of unmatched orders (v2).
    def _qty_buckets(by):
        out: dict[tuple, float] = {}
        for key, fills in by.items():
            for ts, qty, *_ in fills:
                b = (key, ts.replace(second=0, microsecond=0))
                out[b] = out.get(b, 0.0) + qty
        return out

    sq, rq = _qty_buckets(s_by), _qty_buckets(r_by)
    matched_qty = sum(min(v, rq.get(k, 0.0)) for k, v in sq.items())
    qty_s, qty_r = sum(sq.values()), sum(rq.values())

    n_s = sum(len(v) for v in s_by.values())
    n_r = sum(len(v) for v in r_by.values())
    n_all_s = matched + n_nearest + split_s_fills
    n_all_r = matched + n_nearest + split_r_fills

    # True leftovers: exact removes matches from s_by (only misses reach
    # s_left) and pops replay hits out of r_left; split mutates both
    # lists in place; nearest matches by identity without removing, so
    # subtract those. What remains is what no tier could pair.
    near_s = {id(s) for _, s, _ in nearest_pairs}
    near_r = {id(r) for _, _, r in nearest_pairs}
    unmatched_s = [(k, f) for k, fills in s_left.items() for f in fills if id(f) not in near_s]
    unmatched_r = [(k, f) for k, fills in r_left.items() for f in fills if id(f) not in near_r]

    # Economics multisets (existence-only): a leftover whose exact
    # (market, side, qty, price) also occurs in the opposite stream has a
    # timing-shifted twin — the seed-settling signature — not a fill the
    # other stream never produced.
    shadow_econ = {(m, side, qty, price) for m, side, qty, price, *_ in shadow_fills}
    replay_econ = {(f.market_id, f.side, f.qty, f.price) for f in replay_fills}

    def _cause(ts, key, opposite_econ):
        if anchor is not None and ts - anchor <= MATCH_TOLERANCE:
            return "boundary"
        if end is not None and end - ts <= MATCH_TOLERANCE:
            return "boundary"
        for g0, g1 in gaps or ():
            if g0 - window <= ts <= g1 + window:
                return "gap"
        if key in opposite_econ:
            return "reseed_twin"
        return "unexplained"

    def _breakdown(unmatched, opposite_econ):
        counts = {"boundary": 0, "gap": 0, "reseed_twin": 0, "unexplained": 0}
        samples = []
        for (m, side), f in unmatched:
            cause = _cause(f[0], (m, side, f[1], f[2]), opposite_econ)
            counts[cause] += 1
            if cause == "unexplained" and len(samples) < 20:
                samples.append(
                    {"market": m, "side": side, "ts": str(f[0]), "qty": f[1], "price": f[2]}
                )
        return counts, samples

    unmatched_s_by_cause, unmatched_s_samples = _breakdown(unmatched_s, replay_econ)
    unmatched_r_by_cause, unmatched_r_samples = _breakdown(unmatched_r, shadow_econ)

    # The nearest window's own evidence, UNCENSORED. `nearest_dt_abs_mean_s`
    # is computed over the pairs the window already admitted -- the candidate
    # list is filtered on `|dt| <= window` -- so it cannot report a pairing the
    # window missed: its maximum is the window edge by construction, whatever
    # the data does (mistakes #67). The census below asks the same question of
    # the fills NO tier paired: how far away is each one's nearest
    # same-(market, side, price) counterpart in the opposite stream, with no
    # window bound at all. Unit is the unpaired FILL, not the pair, so the two
    # directions are pooled and a mutually-nearest couple contributes twice --
    # the question is "how many fills did the window fail to reach", not "how
    # many pairs exist". A mass sitting just above `window` means the window is
    # tight and the leftovers are timing, not disagreement; a mass far out is
    # the re-seed signature `reseed_twin` already counts by existence alone.
    # The counterpart is sought among the opposite stream's UNPAIRED fills
    # only, which is narrower than `reseed_twin`'s existence test (that one
    # searches every fill, matched ones included). Deliberate: this census
    # asks what a WIDER window could have paired, and a counterpart already
    # claimed by another fill was never available to pair. So
    # `no_counterpart` legitimately exceeds `unexplained` -- measured 79 vs
    # 36 on run 20260803T142853.
    def _unpaired_dt(unmatched, opposite):
        by_key = defaultdict(list)
        for (m, side), f in opposite:
            by_key[(m, side, f[2])].append(f[0])
        out = []
        for (m, side), f in unmatched:
            cands = by_key.get((m, side, f[2]))
            if cands:
                out.append(min(abs((c - f[0]).total_seconds()) for c in cands))
        return out

    unpaired_dt = _unpaired_dt(unmatched_s, unmatched_r) + _unpaired_dt(unmatched_r, unmatched_s)
    _w = window.total_seconds()
    _cuts = sorted({_w, 10 * _w, 60.0, 600.0})
    nearest_unpaired_dt = {
        "population": (
            "unpaired fills (either stream) that have a same-(market, side,"
            " price) counterpart among the opposite stream's UNPAIRED fills at"
            " ANY dt -- no window bound; one entry per unpaired fill"
        ),
        "n": len(unpaired_dt),
        "no_counterpart": len(unmatched_s) + len(unmatched_r) - len(unpaired_dt),
        "min_s": round(min(unpaired_dt), 6) if unpaired_dt else None,
        "median_s": round(median(unpaired_dt), 6) if unpaired_dt else None,
        "max_s": round(max(unpaired_dt), 6) if unpaired_dt else None,
        "over_s": {f"{c:g}": sum(1 for d in unpaired_dt if d > c) for c in _cuts},
    }

    deltas.sort()
    return {
        "matching_note": (
            "order-level tiers: exact (v1 floor: equal qty in a 60s"
            " window) alone decides matched/match_rate_*/price_delta_*;"
            " split (same-price partials summing exactly) and nearest"
            " (same price, smallest |dt|, ALWAYS a qty disagreement --"
            " see nearest_qty_delta) claim only exact's leftovers"
            f" within {window.total_seconds()}s and are counted"
            " separately (v2); qty_match_* buckets quantity per minute"
            " and credits overlap, so split fills count"
        ),
        "matched_nearest": n_nearest,
        "matched_split_groups": split_groups,
        "matched_split_shadow_fills": split_s_fills,
        "matched_split_replay_fills": split_r_fills,
        "matched_all_vs_shadow": n_all_s,
        "matched_all_vs_replay": n_all_r,
        "match_rate_all_vs_shadow": round(n_all_s / n_s, 4) if n_s else None,
        "match_rate_all_vs_replay": round(n_all_r / n_r, 4) if n_r else None,
        "nearest_window_s": window.total_seconds(),
        # Conditioned view, kept under its shipped name so archived
        # readings stay comparable: these pairs were selected by
        # `|dt| <= window`, so this mean says nothing about whether the
        # window is wide enough. `nearest_unpaired_dt` is the population
        # that can answer that.
        "nearest_dt_abs_mean_s": (
            round(sum(dt.total_seconds() for dt, _, _ in nearest_pairs) / n_nearest, 6)
            if nearest_pairs
            else None
        ),
        "nearest_unpaired_dt": nearest_unpaired_dt,
        "nearest_qty_delta": {
            "population": (
                "replay qty - shadow qty over the pairs the nearest tier"
                " claimed; nonzero for every pair while the nearest window"
                " stays within the exact tier's 60s tolerance, because qty is"
                " the only predicate that can have refused them there"
            ),
            "n": n_nearest,
            "equal_qty": sum(1 for d in nearest_qty_deltas if abs(d) <= _QTY_EPS),
            "abs_mean": _abs_mean(nearest_qty_deltas),
            "mean": (round(sum(nearest_qty_deltas) / n_nearest, 6) if nearest_qty_deltas else None),
            "min": round(nearest_qty_deltas[0], 6) if nearest_qty_deltas else None,
            "median": round(median(nearest_qty_deltas), 6) if nearest_qty_deltas else None,
            "max": round(nearest_qty_deltas[-1], 6) if nearest_qty_deltas else None,
        },
        "price_delta_abs_mean_nearest": _abs_mean(nearest_deltas),
        "price_delta_abs_mean_split": _abs_mean(split_deltas),
        "price_delta_mean_all": round(sum(all_deltas) / len(all_deltas), 6) if all_deltas else None,
        "price_delta_abs_mean_all": _abs_mean(all_deltas),
        "qty_match_rate_vs_shadow": round(matched_qty / qty_s, 4) if qty_s else None,
        "qty_match_rate_vs_replay": round(matched_qty / qty_r, 4) if qty_r else None,
        "shadow_fills": n_s,
        "replay_fills": n_r,
        "matched": matched,
        "unmatched_shadow": len(unmatched_s),
        "unmatched_replay": len(unmatched_r),
        "unmatched_shadow_by_cause": unmatched_s_by_cause,
        "unmatched_replay_by_cause": unmatched_r_by_cause,
        "unmatched_unexplained_samples": unmatched_s_samples + unmatched_r_samples,
        "match_rate_vs_shadow": round(matched / n_s, 4) if n_s else None,
        "match_rate_vs_replay": round(matched / n_r, 4) if n_r else None,
        "price_delta_mean": round(sum(deltas) / len(deltas), 6) if deltas else None,
        # `statistics.median`, not `deltas[len(deltas) // 2]`: on an even
        # `matched` the latter is the upper straddler, i.e. a signed
        # fill-model bias reported HIGH by half the straddler gap. The
        # whole record reads 0.0 here so nothing archived moves, but this
        # is the number that would carry a real haircut's sign.
        "price_delta_median": round(median(deltas), 6) if deltas else None,
        "price_delta_abs_mean": (
            round(sum(abs(d) for d in deltas) / len(deltas), 6) if deltas else None
        ),
        "shadow_gross_cash": round(sum(q * p for _, _, q, p, *_ in _rows(s_by)), 2),
        "replay_gross_cash": round(sum(q * p for _, _, q, p, *_ in _rows(r_by)), 2),
        "shadow_fees": round(sum(r[3] for r in _flat(s_by)), 2),
        "replay_fees": round(sum(r[3] for r in _flat(r_by)), 2),
    }


def _flat(by):
    for v in by.values():
        yield from v


def _rows(by):
    for (m, side), v in by.items():
        for _ts, qty, price, fee, maker in v:
            yield m, side, qty, price, fee, maker


#: Re-exported from `hyxlab.shadowruns` so `collector.qa` can ask the same
#: question without importing a simulator (tests/test_boundaries.py).
__all__ = ["compare", "latest_complete_run", "replay_run"]


#: Root of the import closure whose bytes identify "the code that made this
#: report". The root is this module and the closure is 28 repo files wide,
#: which is the point: `attach_wait` (#70/#71) lives in `hyxlab/store.py`.
REPORT_CODE_ROOT = "simulator.divergence"


def report_code() -> dict[str, object]:
    """Stamp identifying the code this run executes; never raises.

    A failure stamps `sha: None` WITH the error rather than omitting the
    field, because the consumer is a staleness test and "I could not tell"
    has to be distinguishable from "unchanged" — the #74 direction: an
    unknown must not read as the cheap answer.
    """
    try:
        return closure_sha(REPORT_CODE_ROOT)
    except (ValueError, OSError, SyntaxError) as exc:  # pragma: no cover - defensive
        return {"root": REPORT_CODE_ROOT, "sha": None, "files": None, "error": str(exc)}


def reported_code_sha(path: Path) -> str | None:
    """The `report_code.sha` of an on-disk report, or None if it has none.

    None covers both a report written before this field existed (every one
    of the 8 in the archive on 2026-09-22) and an unreadable/!JSON file.
    Both mean "cannot prove this was made by today's code".
    """
    try:
        stamp = json.loads(path.read_text()).get("report_code")
    except (OSError, ValueError):
        return None
    return stamp.get("sha") if isinstance(stamp, dict) else None


def main() -> None:
    ap = argparse.ArgumentParser(description="shadow-vs-replay divergence report")
    ap.add_argument(
        "--run",
        default=None,
        help="run_id (default: the newest FINISHED run that produced fills)",
    )
    ap.add_argument("--anchor", default=None, help="ISO ts override for the trading anchor")
    ap.add_argument("--shadow-db", default=SHADOW_DB)
    ap.add_argument("--stream-db", default=STREAM_DB)
    ap.add_argument("--archive-db", default="data/hyxlab.duckdb")
    # Rooted, not cwd-relative: the daily unit runs from the stable
    # worktree and `collector.qa` reads the result from there too, while
    # a by-hand run in the dev tree must land in the same place or
    # --if-new re-derives a 29-minute replay (hyxlab.reportdir).
    ap.add_argument("--out", default=str(shared_reports("shadow_divergence")))
    ap.add_argument(
        "--if-new",
        action="store_true",
        help="exit 0 without replaying when the selected run is already reported"
        " (what the daily timer runs: the subject only changes when the shadow"
        " daemon restarts, so most days there is nothing new to measure)",
    )
    ap.add_argument(
        "--nearest-window",
        type=float,
        default=NEAREST_WINDOW.total_seconds(),
        help="seconds of |dt| tolerance for the nearest/split tiers (default 2)",
    )
    args = ap.parse_args()

    reset_attach_waits()
    # NOTE the budget this one uses: `connect_retry`'s DEFAULT, 15 x 2.0s
    # flat, which that helper's own docstring calls inadequate against a 24/7
    # writer -- and `hyxshadow.duckdb` is owned by exactly such a daemon. It
    # is deliberately NOT re-sized here on the strength of that argument
    # (re-sizing off prose is how mistakes #70 happened); `attach_wait`
    # publishes its measured `budget_frac` per run, and a reading near 1.0 is
    # the evidence that would justify a change.
    with connect_retry(args.shadow_db) as conn:
        run_id = args.run or latest_complete_run(conn)
        if run_id is None:
            raise SystemExit(
                "no completed shadow run with fills to report on"
                " (the only run with fills may still be live; pass --run to force)"
            )
        # Checked while the connection is open but BEFORE the replay: the
        # expensive half is the replay, and skipping it is the whole point
        # of the flag. Exit 0 -- "nothing new to measure" is the normal
        # state of a daily timer whose subject only advances on a daemon
        # restart, and a nonzero exit there would train the operator to
        # ignore this unit's failures.
        existing = Path(args.out) / f"{run_id}.json"
        if args.if_new and existing.exists():
            # THE SUBJECT IS (RUN, CODE), NOT THE RUN (mistakes #76). Keyed
            # on run_id alone this branch printed "nothing to do" on ten
            # consecutive days while four passes shipped five new fields
            # into this report's closure; the field the 09-21 pass promoted
            # to get a production reading could never have got one, because
            # the only thing that advances run_id is a shadow-daemon restart
            # and that restart is itself deferred by the panel guard.
            #
            # The sha is over the tree this process runs FROM, so a by-hand
            # dev-tree run stamps dev's code into the shared report and the
            # stable unit then re-derives until the promote lands. That is
            # the safe direction: it costs one replay, measured at 9m06s /
            # 1.9G peak (journal, 09-12), and it is paid at most once per
            # change rather than once per day.
            here = report_code()
            there = reported_code_sha(existing)
            if here["sha"] is not None and here["sha"] == there:
                print(f"[divergence] run {run_id} already reported in {existing} — nothing to do")
                return
            print(
                f"[divergence] run {run_id} is reported in {existing} but by DIFFERENT"
                f" code (report {there} != {REPORT_CODE_ROOT} closure {here['sha']})"
                " — re-deriving"
            )
        started_at, latency, strategies, anchor = conn.execute(
            "SELECT started_at, latency_s, strategies, anchor FROM shadow_runs WHERE run_id=?",
            [run_id],
        ).fetchone()
        end = conn.execute("SELECT max(ts) FROM shadow_equity WHERE run_id=?", [run_id]).fetchone()[
            0
        ]
        shadow_fills = conn.execute(
            "SELECT market_id, side, qty, price, fee, maker, ts FROM shadow_fills"
            " WHERE run_id=? ORDER BY ts",
            [run_id],
        ).fetchall()

    if args.anchor:
        anchor = datetime.fromisoformat(args.anchor)
    if anchor is None:
        raise SystemExit(f"run {run_id} has no recorded anchor; pass --anchor (see journal)")

    print(f"[divergence] run {run_id} anchor={anchor} end={end} latency={latency}s")
    replay_fills, gaps = replay_run(
        run_id,
        anchor,
        end,
        latency,
        strategies.split(","),
        stream_db=args.stream_db,
        archive_db=args.archive_db,
        return_gaps=True,
    )
    report = {
        "run_id": run_id,
        "anchor": str(anchor),
        "end": str(end),
        "latency_s": latency,
        "strategies": strategies,
        "generated_at": str(datetime.now(UTC).replace(tzinfo=None)),
        # WHICH CODE PRODUCED THIS (mistakes #76). `--if-new` keys on it as
        # well as on run_id, because the report is a function of BOTH.
        "report_code": report_code(),
        # Three attaches reach this report (shadow ledger, stream archive,
        # market archive), on three different budgets, all of them justified
        # by prose no artifact could contradict until now (mistakes #70).
        "attach_wait": attach_wait_block(),
        **compare(
            shadow_fills,
            replay_fills,
            window=timedelta(seconds=args.nearest_window),
            anchor=anchor,
            end=end,
            gaps=gaps,
        ),
    }
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{run_id}.json"
    out.write_text(json.dumps(report, indent=2, default=str) + "\n")
    for k, v in report.items():
        print(f"  {k}: {v}")
    print(f"[divergence] written to {out}")


if __name__ == "__main__":
    main()
