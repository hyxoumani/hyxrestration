"""FinBERT scoring for Phase 0. Parallel to hyx/sentiment.py (§0 separation).

Scores the headlines corpus from phase0/data/alpaca_news.csv with
`yiyanghkust/finbert-tone` on the local GPU and caches per-article results
to phase0/data/finbert_scores.csv keyed by news_id. One-time cost ~5 min
for ~100k headlines on a 5090.

Output schema:
    news_id, label, score, pos, neg, neu, scored_at
"""

from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

import pandas as pd

from phase0.data_loaders import DATA_DIR
from phase0.news_loader import NEWS_CSV

MODEL_NAME = "yiyanghkust/finbert-tone"
MODEL_TAG = "finbert-tone"
SCORES_CSV = DATA_DIR / "finbert_scores.csv"


@lru_cache(maxsize=1)
def _pipeline():
    """Cached transformers pipeline on GPU if available.

    Same Auto-class workaround as hyx/sentiment.py — the FinBERT repo
    predates transformers-4 metadata conventions, so we instantiate
    BertTokenizer + BertForSequenceClassification directly.
    """
    import torch
    from transformers import BertForSequenceClassification, BertTokenizer, pipeline

    device = 0 if torch.cuda.is_available() else -1
    tok = BertTokenizer.from_pretrained(MODEL_NAME)
    model = BertForSequenceClassification.from_pretrained(MODEL_NAME)
    return pipeline(
        "text-classification",
        model=model,
        tokenizer=tok,
        device=device,
        top_k=None,
        truncation=True,
        max_length=128,
    )


def score_corpus(
    news_path: Path = NEWS_CSV,
    out_path: Path = SCORES_CSV,
    batch_size: int = 64,
    refresh: bool = False,
) -> pd.DataFrame:
    """Score every unique headline in news_path; cache results to out_path.

    If out_path exists, only new news_ids are scored (idempotent incremental
    scoring — same semantics as hyx/slice1.py's _score_unscored).
    """
    if not news_path.exists():
        raise FileNotFoundError(f"{news_path} not found. Run phase0/news_loader.py first.")

    news = pd.read_csv(news_path)
    unique = news.drop_duplicates(subset=["news_id"])[["news_id", "headline"]]

    existing: pd.DataFrame | None = None
    if out_path.exists() and not refresh:
        existing = pd.read_csv(out_path)
        unscored = unique[~unique["news_id"].astype(str).isin(existing["news_id"].astype(str))]
    else:
        unscored = unique

    if len(unscored) == 0:
        return existing if existing is not None else pd.DataFrame()

    clf = _pipeline()
    batched = clf(unscored["headline"].fillna("").tolist(), batch_size=batch_size)
    now = datetime.now(tz=UTC).isoformat()

    new_rows: list[dict[str, object]] = []
    for nid, per_headline in zip(unscored["news_id"].tolist(), batched, strict=True):
        scores = {item["label"].lower(): float(item["score"]) for item in per_headline}
        pos = scores.get("positive", 0.0)
        neg = scores.get("negative", 0.0)
        neu = scores.get("neutral", 0.0)
        label, score = max(
            (("positive", pos), ("negative", neg), ("neutral", neu)),
            key=lambda kv: kv[1],
        )
        new_rows.append(
            {
                "news_id": nid,
                "label": label,
                "score": score,
                "pos": pos,
                "neg": neg,
                "neu": neu,
                "scored_at": now,
            }
        )

    new_df = pd.DataFrame(new_rows)
    combined = pd.concat([existing, new_df], ignore_index=True) if existing is not None else new_df
    combined.to_csv(out_path, index=False)
    return combined


def load_scores(path: Path = SCORES_CSV) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"{path} not found. Run score_corpus() first.")
    return pd.read_csv(path)


if __name__ == "__main__":
    df = score_corpus()
    print(f"finbert_scores.csv: {len(df)} rows")
