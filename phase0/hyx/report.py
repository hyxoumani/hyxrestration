"""Per-run reports: MD (human-readable) + CSV (machine-readable).

One pair of files per slice per day: reports/slice{N}/YYYY-MM-DD.{md,csv}.
Rerunning the same slice on the same day overwrites the prior report — the
authoritative state lives in DuckDB.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass(frozen=True)
class TickerReport:
    ticker: str
    ohlcv_rows_ingested: int
    news_rows_ingested: int
    latest_close: float | None
    latest_bar_date: date | None
    sentiment_pos: int  # count of positive-labeled headlines today
    sentiment_neg: int
    sentiment_neu: int
    top_headlines: list[tuple[str, str]]  # (label, headline) — already sorted by interestingness


def write_report(
    reports_dir: Path,
    slice_num: int,
    run_date: date,
    tickers: list[TickerReport],
    notes: list[str] | None = None,
) -> tuple[Path, Path]:
    """Write MD + CSV pair. Returns (md_path, csv_path)."""
    out_dir = reports_dir / f"slice{slice_num}"
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"{run_date.isoformat()}.md"
    csv_path = out_dir / f"{run_date.isoformat()}.csv"

    _write_csv(csv_path, tickers)
    _write_md(md_path, slice_num, run_date, tickers, notes or [])
    return md_path, csv_path


def _write_csv(path: Path, tickers: list[TickerReport]) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "ticker",
                "ohlcv_ingested",
                "news_ingested",
                "latest_close",
                "latest_bar_date",
                "sentiment_pos",
                "sentiment_neg",
                "sentiment_neu",
            ]
        )
        for t in tickers:
            w.writerow(
                [
                    t.ticker,
                    t.ohlcv_rows_ingested,
                    t.news_rows_ingested,
                    f"{t.latest_close:.4f}" if t.latest_close is not None else "",
                    t.latest_bar_date.isoformat() if t.latest_bar_date else "",
                    t.sentiment_pos,
                    t.sentiment_neg,
                    t.sentiment_neu,
                ]
            )


def _write_md(
    path: Path,
    slice_num: int,
    run_date: date,
    tickers: list[TickerReport],
    notes: list[str],
) -> None:
    lines: list[str] = []
    lines.append(f"# Slice {slice_num} — {run_date.isoformat()}")
    lines.append("")
    if notes:
        lines.append("## Notes")
        lines.extend(f"- {n}" for n in notes)
        lines.append("")

    lines.append("## Tickers")
    lines.append("")
    lines.append("| ticker | ohlcv new | news new | latest close | latest bar | pos / neg / neu |")
    lines.append("|---|---:|---:|---:|---|---|")
    for t in tickers:
        close = f"{t.latest_close:.2f}" if t.latest_close is not None else "—"
        bar = t.latest_bar_date.isoformat() if t.latest_bar_date else "—"
        sent = f"{t.sentiment_pos} / {t.sentiment_neg} / {t.sentiment_neu}"
        lines.append(
            f"| {t.ticker} | {t.ohlcv_rows_ingested} | {t.news_rows_ingested} | {close} | {bar} | {sent} |"
        )
    lines.append("")

    for t in tickers:
        if not t.top_headlines:
            continue
        lines.append(f"### {t.ticker} headlines")
        lines.append("")
        for label, headline in t.top_headlines:
            lines.append(f"- **[{label}]** {headline}")
        lines.append("")

    path.write_text("\n".join(lines))
