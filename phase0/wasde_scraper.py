"""Scrape historical WASDE reports from USDA/NAL into phase0/data/wasde_releases.csv.

Source: https://esmis.nal.usda.gov/publication/world-agricultural-supply-and-demand-estimates
(Cornell Mann Library archive, now hosted by the National Agricultural Library.)

Pipeline per release:
    1. Paginate the listing to enumerate release dates.
    2. For each date, fetch its release page and extract the .xls download URL.
    3. Cache the XLS to phase0/data/wasde_xls/YYYY-MM-DD.xls.
    4. Parse the XLS — consistent structure across 2014-2024:
         Page 11: U.S. Wheat Supply and Use
         Page 12: U.S. Feed Grain (FEED GRAINS section + CORN subsection)
         Page 15: U.S. Soybeans and Products
       Extract Production / Ending Stocks / Yield per Harvested Acre from the
       rightmost numeric column (the current release's freshest estimate).

Output schema matches wasde_loader.py:
    release_date (DATE), crop, line_item, value_reported (FLOAT)

Rerunning is idempotent: existing XLS files are not re-downloaded.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from pathlib import Path

import pandas as pd
import requests

from phase0.data_loaders import DATA_DIR
from phase0.wasde_loader import WASDE_CSV

BASE = "https://esmis.nal.usda.gov"
LISTING_URL = f"{BASE}/publication/world-agricultural-supply-and-demand-estimates"
XLS_DIR = DATA_DIR / "wasde_xls"
UA = "Mozilla/5.0 (compatible; hyxrestration-phase0/1)"

# Line-item label → (crop sheet resolver, row-pattern)
LINE_ITEMS = {
    "production": "Production",
    "ending_stocks": "Ending Stocks",
    "yield": "Yield per Harvested",  # matches "Yield per Harvested Acre" etc
}


# ---------------------------------------------------------------- listing


def enumerate_releases(
    start_date: str = "2014-01-01",
    end_date: str = "2024-12-31",
    max_pages: int = 25,
) -> dict[str, str]:
    """Return {release_date_iso: release_page_url} filtered to [start, end]."""
    dates: dict[str, str] = {}
    for page in range(1, max_pages + 1):
        url = f"{LISTING_URL}?per_page=10&page={page}"
        text = _get(url).text
        matches = re.findall(
            r"world-agricultural-supply-and-demand-estimates/(\d{4}-\d{2}-\d{2})",
            text,
        )
        if not matches:
            break
        for d in matches:
            if start_date <= d <= end_date:
                dates[d] = f"{LISTING_URL}/{d}"
        # Stop paging once we've gone past start_date
        oldest = min(matches)
        if oldest < start_date:
            break
        time.sleep(0.3)
    return dict(sorted(dates.items()))


def get_xls_url(release_page_url: str) -> str | None:
    text = _get(release_page_url).text
    m = re.search(r'(/sites/default/release-files/[^"\s]+\.xls)"', text)
    return BASE + m.group(1) if m else None


def _get(url: str) -> requests.Response:
    r = requests.get(url, headers={"User-Agent": UA}, allow_redirects=True, timeout=30)
    r.raise_for_status()
    return r


# ---------------------------------------------------------------- parsing


def _find_crop_sheet(
    xlsx: pd.ExcelFile,
    markers: tuple[str, ...],
) -> str | None:
    """Return the first sheet whose first 12 rows × 8 cols contain any marker."""
    for name in xlsx.sheet_names:
        try:
            df = pd.read_excel(xlsx, sheet_name=name, header=None, nrows=12)
        except Exception:
            continue
        for r in range(min(12, len(df))):
            for c in range(min(8, df.shape[1])):
                cell = str(df.iloc[r, c]) if pd.notna(df.iloc[r, c]) else ""
                if any(m in cell for m in markers):
                    return name
    return None


def _rightmost_numeric(row: pd.Series) -> float | None:
    for val in reversed(row.tolist()):
        if isinstance(val, (int, float)) and not pd.isna(val):
            return float(val)
        # Sometimes numeric values are stored as strings
        if isinstance(val, str):
            s = val.replace(",", "").strip()
            try:
                return float(s)
            except ValueError:
                continue
    return None


def _extract_values_from_sheet(
    df: pd.DataFrame,
    row_start: int = 0,
    row_end: int | None = None,
) -> dict[str, float]:
    """Within the given row range, find Production/Ending Stocks/Yield row labels
    in the first 3 columns and extract the rightmost numeric value from each row.
    """
    row_end = row_end if row_end is not None else len(df)
    out: dict[str, float] = {}
    for r in range(row_start, min(row_end, len(df))):
        for c in range(min(3, df.shape[1])):
            cell = str(df.iloc[r, c]) if pd.notna(df.iloc[r, c]) else ""
            for key, marker in LINE_ITEMS.items():
                if marker in cell and key not in out:
                    val = _rightmost_numeric(df.iloc[r])
                    if val is not None:
                        out[key] = val
                    break
    return out


def _find_corn_section(df: pd.DataFrame) -> int | None:
    """Page 12 has FEED GRAINS then CORN. Return row index where CORN starts."""
    for r in range(len(df)):
        for c in range(min(3, df.shape[1])):
            cell = str(df.iloc[r, c]) if pd.notna(df.iloc[r, c]) else ""
            # Match exact "CORN" or "Corn" as a section header line (not in running text)
            if cell.strip() in ("CORN", "Corn") or cell.strip().startswith("CORN "):
                return r
    return None


def parse_wasde(xls_path: Path) -> list[dict]:
    """Return rows for (crop, line_item) found in this XLS. Empty list on failure."""
    try:
        xlsx = pd.ExcelFile(xls_path)
    except Exception as e:
        print(f"  [warn] open failed for {xls_path.name}: {e}")
        return []

    wheat_sheet = _find_crop_sheet(xlsx, ("U.S. Wheat Supply",))
    corn_sheet = _find_crop_sheet(xlsx, ("U.S. Feed Grain", "U.S. Corn"))
    soy_sheet = _find_crop_sheet(xlsx, ("U.S. Soybean", "U.S. Soy "))

    rows: list[dict] = []

    if wheat_sheet:
        df = pd.read_excel(xlsx, sheet_name=wheat_sheet, header=None, nrows=40)
        for key, val in _extract_values_from_sheet(df, row_start=0, row_end=30).items():
            rows.append(("wheat", key, val))

    if corn_sheet:
        df = pd.read_excel(xlsx, sheet_name=corn_sheet, header=None, nrows=80)
        corn_start = _find_corn_section(df)
        if corn_start is not None:
            for key, val in _extract_values_from_sheet(
                df, row_start=corn_start, row_end=corn_start + 25
            ).items():
                rows.append(("corn", key, val))

    if soy_sheet:
        df = pd.read_excel(xlsx, sheet_name=soy_sheet, header=None, nrows=30)
        for key, val in _extract_values_from_sheet(df, row_start=0, row_end=28).items():
            rows.append(("soybeans", key, val))

    return [{"crop": c, "line_item": li, "value_reported": v} for c, li, v in rows]


# ---------------------------------------------------------------- orchestrator


def download_all(
    start_date: str = "2014-01-01",
    end_date: str = "2024-12-31",
    sleep_between: float = 0.5,
) -> Iterator[tuple[str, Path]]:
    """Download every WASDE XLS for releases in [start_date, end_date]. Yields
    (release_date, local_path) for each successful download."""
    XLS_DIR.mkdir(parents=True, exist_ok=True)
    releases = enumerate_releases(start_date, end_date)
    print(f"enumerated {len(releases)} releases in window {start_date} → {end_date}")

    for i, (date, page_url) in enumerate(releases.items(), 1):
        dest = XLS_DIR / f"{date}.xls"
        if dest.exists():
            yield (date, dest)
            continue
        xls_url = get_xls_url(page_url)
        if not xls_url:
            print(f"  [{i}/{len(releases)}] {date}: no XLS link found")
            continue
        r = _get(xls_url)
        dest.write_bytes(r.content)
        print(f"  [{i}/{len(releases)}] {date}: {len(r.content):,} bytes")
        time.sleep(sleep_between)
        yield (date, dest)


def build_csv(
    start_date: str = "2014-01-01",
    end_date: str = "2024-12-31",
    out_path: Path = WASDE_CSV,
) -> pd.DataFrame:
    """Download every release in window and extract into wasde_releases.csv."""
    all_rows: list[dict] = []
    for release_date, xls_path in download_all(start_date, end_date):
        parsed = parse_wasde(xls_path)
        if not parsed:
            print(f"  [warn] {release_date}: 0 values parsed")
            continue
        for row in parsed:
            row["release_date"] = release_date
            all_rows.append(row)

    df = pd.DataFrame(all_rows, columns=["release_date", "crop", "line_item", "value_reported"])
    df["release_date"] = pd.to_datetime(df["release_date"])
    df = df.sort_values(["release_date", "crop", "line_item"]).reset_index(drop=True)
    df.to_csv(out_path, index=False)
    print(
        f"\nwrote {len(df)} rows ({df['release_date'].nunique()} releases × ~9 values) to {out_path}"
    )
    return df


if __name__ == "__main__":
    build_csv()
