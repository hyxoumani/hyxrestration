"""Phase 0 — §2.9 Qwen A/B driver: re-run Test 2-standalone and Test 2+3 on Qwen scores.

Reuses the regression / FDR / verdict / report machinery from phase0.test2_sentiment_standalone
(§2.8) and phase0.test23_wasde_sentiment (§2), swapping only the sentiment source
from FinBERT to Qwen. All other pipeline pieces — news corpus, prices, WASDE
surprises, event-panel construction, FDR correction — are identical.

Run:
    python -m phase0.test29_qwen_ab                     # full pipeline
    python -m phase0.test29_qwen_ab --skip-score        # reuse cached qwen scores

Outputs:
    phase0/data/qwen_scores.csv
    phase0/results/test2_qwen_{today}.md
    phase0/results/test23_qwen_{today}.md
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from datetime import date as date_cls

import pandas as pd

from phase0.data_loaders import load_adj_close
from phase0.events import build_event_panel, daily_ticker_sentiment
from phase0.news_loader import NEWS_CSV
from phase0.sentiment_qwen import SCORES_CSV as QWEN_SCORES_CSV
from phase0.sentiment_qwen import score_corpus as score_qwen
from phase0.surprise import compute_trend_residual
from phase0.wasde_loader import load_wasde

# Reuse §2.8 Test 2-standalone machinery
from phase0 import test2_sentiment_standalone as t2

# Reuse §2 Test 2+3 combined machinery
from phase0 import test23_wasde_sentiment as t23

TEST23_START = datetime(2021, 1, 1, tzinfo=UTC)
TEST23_END = datetime(2024, 12, 31, 23, 59, 59, tzinfo=UTC)

REGRESSION_TICKERS: tuple[str, ...] = tuple(
    t for group in t23.TICKER_CATEGORIES.values() for t in group
)
T2_TICKERS: tuple[str, ...] = tuple(t for group in t2.TICKER_CATEGORIES.values() for t in group)


def main() -> int:
    p = argparse.ArgumentParser(prog="test29_qwen_ab")
    p.add_argument(
        "--skip-score",
        action="store_true",
        help="Reuse phase0/data/qwen_scores.csv instead of running Qwen. "
        "Errors if the cache is missing.",
    )
    args = p.parse_args()

    # --- News corpus ------------------------------------------------------
    if not NEWS_CSV.exists():
        raise SystemExit(f"{NEWS_CSV} missing — run phase0.test23_real_driver first to fetch news")
    news = pd.read_csv(NEWS_CSV, parse_dates=["timestamp"])
    print(
        f"[qwen_ab] news corpus: {len(news)} rows, "
        f"{news['news_id'].nunique()} unique articles, "
        f"range {news['timestamp'].min()} → {news['timestamp'].max()}"
    )

    # --- Qwen scoring -----------------------------------------------------
    if args.skip_score:
        if not QWEN_SCORES_CSV.exists():
            raise SystemExit(f"--skip-score but {QWEN_SCORES_CSV} missing")
        scores = pd.read_csv(QWEN_SCORES_CSV)
        print(f"[qwen_ab] skip-score: loaded {len(scores)} cached Qwen scores")
    else:
        print("[qwen_ab] scoring with Qwen 2.5 7B Instruct (zero-shot) — takes ~1-2 min on 5090")
        scores = score_qwen()
        print(f"[qwen_ab] Qwen scores: {len(scores)} rows")

    if len(scores):
        print(f"[qwen_ab] label distribution: {scores['label'].value_counts().to_dict()}")

    # --- Daily ticker sentiment (same aggregation as FinBERT path) ---------
    daily_sent = daily_ticker_sentiment(news, scores)
    print(f"[qwen_ab] daily_ticker_sentiment: {len(daily_sent)} (ticker,date) pairs")

    # --- Prices (cached from Test 1) --------------------------------------
    all_needed = tuple({*REGRESSION_TICKERS, "SPY"})
    prices = load_adj_close(all_needed, start="2014-01-01", end="2025-01-01").sort_index()

    today = date_cls.today()

    # ======================================================================
    # A) Test 2-standalone on Qwen scores
    # ======================================================================
    print("\n=== A) Test 2-standalone on Qwen scores (§2.8 design) ===")
    t2_panel = t2.build_daily_panel(daily_sent, prices, T2_TICKERS)
    t2_panel = t2_panel[(t2_panel["date"] >= t2.WINDOW_START) & (t2_panel["date"] <= t2.WINDOW_END)]
    print(
        f"[qwen_ab/t2] panel: {len(t2_panel)} rows "
        f"({t2_panel['ticker'].nunique()} tickers × {t2_panel['horizon'].nunique()} horizons)"
    )

    t2_results = t2.run_all_regressions(t2_panel)
    t2.apply_fdr(t2_results)
    t2_verdict = t2.evaluate_verdict(t2_results)

    t2_report_path = t2.RESULTS_DIR / f"test2_qwen_{today.isoformat()}.md"
    t2_panel_info = {
        "Sentiment model": "Qwen 2.5 7B Instruct (zero-shot, §2.9 prompt)",
        "Window": f"{t2.WINDOW_START} → {t2.WINDOW_END}",
        "News articles": f"{news['news_id'].nunique()}",
        "Qwen scores": f"{len(scores)}",
        "Daily panel rows": f"{len(t2_panel)}",
        "Tickers": ", ".join(T2_TICKERS),
        "Horizons": ", ".join(f"{h}d" for h in t2.HORIZONS),
    }
    t2.write_report(t2_results, t2_verdict, t2_panel_info, today, t2_report_path)
    print(f"[qwen_ab/t2] verdict: {t2_verdict.verdict}  surviving={t2_verdict.n_surviving}/9")
    print(f"[qwen_ab/t2] report: {t2_report_path}")

    # ======================================================================
    # B) Test 2+3 combined on Qwen scores
    # ======================================================================
    print("\n=== B) Test 2+3 combined on Qwen scores (§2.3/§2.4 design) ===")
    wasde = load_wasde()
    surprises = compute_trend_residual(wasde).dropna(subset=["surprise"])
    window_mask = (surprises["release_date"] >= "2021-01-01") & (
        surprises["release_date"] <= "2024-12-31"
    )
    window_surprises = surprises[window_mask]
    print(
        f"[qwen_ab/t23] WASDE surprises in window: {len(window_surprises)} "
        f"({window_surprises['release_date'].nunique()} releases)"
    )

    t23_panel = build_event_panel(
        wasde_surprises=window_surprises,
        prices=prices,
        daily_sentiment=daily_sent,
        tickers=REGRESSION_TICKERS,
    )
    coverage = t23_panel["event_sentiment"].notna().mean()
    print(f"[qwen_ab/t23] event panel: {len(t23_panel)} rows  sentiment coverage={coverage:.1%}")

    t23_results = t23.run_all_regressions(t23_panel)
    t23.apply_fdr(t23_results)
    t23_verdict = t23.evaluate_verdict(t23_results)

    t23_report_path = t23.RESULTS_DIR / f"test23_qwen_{today.isoformat()}.md"
    t23.write_report(
        t23_results,
        t23_verdict,
        today,
        t23_report_path,
        caveats=[
            (
                "**Qwen 2.5 7B Instruct zero-shot** scoring per §2.9.3 prompt. "
                f"Same corpus as iter 3 ({news['news_id'].nunique()} articles), "
                f"same window (2021-2024), same regression design as §2.3."
            ),
            (
                "**CNH excluded via §7.1 depth check** — same as iter 3. "
                "See phase0/results/depth_check_2026-04-22.md."
            ),
            (
                "**A/B against FinBERT:** compare against "
                "phase0/results/test23_2026-04-22.md (same day, same data, "
                "FinBERT scores). Only the sentiment scorer varies."
            ),
        ],
    )
    print(f"[qwen_ab/t23] verdict: {t23_verdict.verdict}  surviving={t23_verdict.n_surviving}/36")
    print(f"[qwen_ab/t23] report: {t23_report_path}")

    # ----------------------------------------------------------------------
    # Joint summary + §2.9.5 interpretation
    # ----------------------------------------------------------------------
    print("\n=== Joint §2.9.5 outcome ===")
    t2_pass = t2_verdict.verdict == "pass"
    t23_pass = t23_verdict.verdict == "joint_pass"
    print(f"  Qwen Test 2-standalone: {'PASS' if t2_pass else t2_verdict.verdict.upper()}")
    print(f"  Qwen Test 2+3 combined: {'PASS' if t23_pass else t23_verdict.verdict.upper()}")
    if t2_pass and t23_pass:
        print("  → Full L01 validation. Build architecture as originally designed.")
    elif t2_pass and not t23_pass:
        print("  → §2.5 pivot is ROBUST. Sentiment works; cross-modal interaction is still null.")
    elif not t2_pass and t23_pass:
        print("  → Surprising cross-modal-only signal. Architectural discussion required.")
    else:
        print("  → Thesis ROBUSTLY FALSIFIED. Execute §2.5 pivot with max confidence.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
