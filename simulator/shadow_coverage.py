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
**100.0% for every run since 2026-07-31 08:20** (4,799 of 4,799 pooled
over the last five runs).

So the shadow track has stopped observing outcomes altogether. Two
things follow, and both are validity bounds on readings taken elsewhere:

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

This is a bound on OTHER readings, not a verdict on any strategy.
"""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from hyxlab.store import connect_retry

# Runs pooled into the `recent` block. Five is the span over which the
# live ledger first read 100% unobserved, so it is the window that shows
# the current regime rather than the historical average that dilutes it.
RECENT_RUNS = 5


def _ratio(observed: float, unobserved: float) -> float | None:
    """Coverage = observed / (observed + unobserved), or None when the
    run has no dated fills at all. None, not 0.0 or 1.0 — a run with
    nothing to observe has no coverage, and defaulting either way would
    read as a finding."""
    total = observed + unobserved
    if total <= 0:
        return None
    return round(observed / total, 4)


def build_coverage(ledger, markets_conn, recent_runs: int = RECENT_RUNS) -> dict:
    """Per-run and pooled outcome coverage.

    `ledger` is a connection to the shadow ledger; `markets_conn` a
    connection carrying a `markets` table with `market_id`/`close_time`.
    They are separate databases in production and separate arguments
    here so the report can be tested without an ATTACH.
    """
    closes = {
        mid: ct
        for mid, ct in markets_conn.execute(
            "SELECT market_id, close_time FROM markets"
        ).fetchall()
    }

    # Run end is the last EQUITY tick, not the last fill: a run keeps
    # marking after it stops trading, and using the last fill would
    # shorten the observation window and overstate the problem.
    ends = dict(
        ledger.execute(
            "SELECT run_id, max(ts) FROM shadow_equity GROUP BY run_id"
        ).fetchall()
    )

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

        obs_n = unobs_n = undated_n = 0
        obs_v = unobs_v = undated_v = 0.0
        for market_id, qty, price in fills:
            notional = abs(qty or 0.0) * (price or 0.0)
            close = closes.get(market_id)
            if close is None:
                undated_n += 1
                undated_v += notional
            elif end is not None and close <= end:
                obs_n += 1
                obs_v += notional
            else:
                unobs_n += 1
                unobs_v += notional

        life_h = None
        if end is not None and started_at is not None:
            life_h = round((end - started_at).total_seconds() / 3600.0, 2)

        runs.append(
            {
                "run_id": run_id,
                "started_at": started_at.isoformat() if started_at else None,
                "ended_at": end.isoformat() if end else None,
                "life_hours": life_h,
                "fills": len(fills),
                "observed_fills": obs_n,
                "unobserved_fills": unobs_n,
                "undated_fills": undated_n,
                "coverage_fills": _ratio(obs_n, unobs_n),
                "observed_notional": round(obs_v, 2),
                "unobserved_notional": round(unobs_v, 2),
                "undated_notional": round(undated_v, 2),
                "coverage_notional": _ratio(obs_v, unobs_v),
            }
        )

    def _pool(subset: list[dict]) -> dict:
        obs_n = sum(r["observed_fills"] for r in subset)
        unobs_n = sum(r["unobserved_fills"] for r in subset)
        obs_v = sum(r["observed_notional"] for r in subset)
        unobs_v = sum(r["unobserved_notional"] for r in subset)
        return {
            "runs": len(subset),
            "fills": sum(r["fills"] for r in subset),
            "observed_fills": obs_n,
            "unobserved_fills": unobs_n,
            "undated_fills": sum(r["undated_fills"] for r in subset),
            "coverage_fills": _ratio(obs_n, unobs_n),
            "observed_notional": round(obs_v, 2),
            "unobserved_notional": round(unobs_v, 2),
            "coverage_notional": _ratio(obs_v, unobs_v),
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
        f" recent {recent['runs']}: coverage_fills={recent['coverage_fills']}"
        f" coverage_notional={recent['coverage_notional']}"
        f" ({recent['unobserved_fills']}/{recent['fills']} fills unobserved)"
    )
    print("| run | life_h | fills | unobserved | cov_fills | cov_notional |")
    print("|---|---|---|---|---|---|")
    for r in report["runs"][-12:]:
        print(
            f"| {r['run_id']} | {r['life_hours']} | {r['fills']}"
            f" | {r['unobserved_fills']} | {r['coverage_fills']}"
            f" | {r['coverage_notional']} |"
        )
    print(f"[shadow_coverage] written to {out}")


if __name__ == "__main__":
    main()
