"""WASDE release-value loader.

Schema (the only table Test 2+3 needs from WASDE):

    release_date   DATE       — the day the report was published
    crop           TEXT       — corn | soybeans | wheat
    line_item      TEXT       — production | ending_stocks | yield
    value_reported FLOAT      — USDA's published value

Iteration 1 reads from `phase0/data/wasde_releases.csv` if present. The real
scrape (USDA OCE + Cornell Mann Library PDFs) is iteration 2 and populates
that CSV out-of-band. Until then, a synthetic-data helper supports
end-to-end pipeline testing.

Consensus estimates are NOT in this table — they're derived later in
surprise.py (trend-residual proxy) or scraped from Farmdoc in stage 2.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from phase0.data_loaders import DATA_DIR

CROPS: tuple[str, ...] = ("corn", "soybeans", "wheat")
LINE_ITEMS: tuple[str, ...] = ("production", "ending_stocks", "yield")
WASDE_CSV = DATA_DIR / "wasde_releases.csv"


def load_wasde(path: Path = WASDE_CSV) -> pd.DataFrame:
    """Return the WASDE release table. Raises FileNotFoundError if not populated."""
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Populate it via phase0/wasde_loader.py scrape "
            "(iteration 2) or generate_synthetic_wasde() for pipeline testing."
        )
    df = pd.read_csv(path, parse_dates=["release_date"])
    _validate(df)
    return df.sort_values(["release_date", "crop", "line_item"]).reset_index(drop=True)


def _validate(df: pd.DataFrame) -> None:
    expected = {"release_date", "crop", "line_item", "value_reported"}
    missing = expected - set(df.columns)
    if missing:
        raise ValueError(f"wasde_releases.csv missing columns: {missing}")
    bad_crops = set(df["crop"].unique()) - set(CROPS)
    if bad_crops:
        raise ValueError(f"unrecognized crops: {bad_crops}")
    bad_lines = set(df["line_item"].unique()) - set(LINE_ITEMS)
    if bad_lines:
        raise ValueError(f"unrecognized line_items: {bad_lines}")


# ---------------------------------------------------------------- synthetic

# Baseline values roughly in the right ballpark for calendar-2020s WASDE numbers,
# used only for pipeline validation. Not a forecast and not for analysis.
_SYNTHETIC_BASELINES: dict[tuple[str, str], float] = {
    ("corn", "production"): 14_500.0,  # million bushels
    ("corn", "ending_stocks"): 1_800.0,
    ("corn", "yield"): 175.0,  # bushels / acre
    ("soybeans", "production"): 4_400.0,
    ("soybeans", "ending_stocks"): 300.0,
    ("soybeans", "yield"): 51.0,
    ("wheat", "production"): 1_900.0,
    ("wheat", "ending_stocks"): 700.0,
    ("wheat", "yield"): 48.0,
}


def generate_synthetic_wasde(
    start: str = "2014-01-10",
    end: str = "2024-12-31",
    seed: int = 42,
) -> pd.DataFrame:
    """Generate a synthetic WASDE panel with monthly release dates and realistic noise.

    Used only for pipeline smoke tests — not a forecast. Values drift with a
    small AR(1) component so the trend-residual surprise has something to
    measure and occasional outliers exist.
    """
    rng = np.random.default_rng(seed)
    # Month-start second week-ish
    dates = pd.date_range(start=start, end=end, freq="MS") + pd.Timedelta(days=9)
    dates = dates[(dates >= start) & (dates <= end)]

    rows: list[dict[str, object]] = []
    for crop in CROPS:
        for line in LINE_ITEMS:
            baseline = _SYNTHETIC_BASELINES[(crop, line)]
            # AR(1) random walk around the baseline
            ar_coef = 0.7
            sigma = baseline * 0.02  # ~2% typical revision noise
            level = baseline
            for d in dates:
                level = ar_coef * level + (1 - ar_coef) * baseline + rng.normal(0, sigma)
                # Occasional outlier (adverse weather / surprise)
                if rng.random() < 0.05:
                    level += rng.choice([-1, 1]) * sigma * 3
                rows.append(
                    {
                        "release_date": d,
                        "crop": crop,
                        "line_item": line,
                        "value_reported": float(level),
                    }
                )

    return pd.DataFrame(rows)


def write_synthetic(path: Path = WASDE_CSV) -> Path:
    """Write a synthetic WASDE CSV for pipeline testing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    df = generate_synthetic_wasde()
    df.to_csv(path, index=False)
    return path


if __name__ == "__main__":
    p = write_synthetic()
    print(f"synthetic WASDE written to {p} ({len(load_wasde(p))} rows)")
