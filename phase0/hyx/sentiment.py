"""FinBERT sentiment scoring for news headlines.

Uses `yiyanghkust/finbert-tone` per architecture.md §2.4. The model outputs a
softmax over {positive, negative, neutral}; we persist all three probabilities
and the argmax label so downstream consumers can re-threshold without re-running
inference.

Lazy-loaded so importing this module doesn't pull the weights.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache

MODEL_NAME = "yiyanghkust/finbert-tone"
MODEL_TAG = "finbert-tone"  # shorter tag stored in news_sentiment.model


@dataclass(frozen=True)
class SentimentScore:
    label: str  # positive | negative | neutral
    score: float  # argmax softmax probability
    pos: float
    neg: float
    neu: float


@lru_cache(maxsize=1)
def _pipeline():
    """Return the cached transformers text-classification pipeline on GPU if available.

    `yiyanghkust/finbert-tone` ships a pre-transformers-4 style repo: vocab.txt
    only (no tokenizer.json), and config.json is missing the `model_type` key.
    Transformers 5.x's Auto* classes choke on both, so we instantiate the
    concrete BertTokenizer + BertForSequenceClassification directly.
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
        top_k=None,  # return all three class probabilities
        truncation=True,
        max_length=128,  # FinBERT's native context is small; headlines fit
    )


def score_headlines(headlines: Sequence[str], batch_size: int = 64) -> list[SentimentScore]:
    """Score a batch of headlines. Preserves order; returns one SentimentScore per input."""
    if not headlines:
        return []
    clf = _pipeline()
    raw = clf(list(headlines), batch_size=batch_size)

    out: list[SentimentScore] = []
    for per_headline in raw:
        scores: dict[str, float] = {}
        for item in per_headline:
            # FinBERT-tone's labels are already lowercase: "Positive" | "Negative" | "Neutral"
            scores[item["label"].lower()] = float(item["score"])
        pos = scores.get("positive", 0.0)
        neg = scores.get("negative", 0.0)
        neu = scores.get("neutral", 0.0)
        label = max(("positive", pos), ("negative", neg), ("neutral", neu), key=lambda kv: kv[1])[0]
        out.append(
            SentimentScore(label=label, score=scores.get(label, 0.0), pos=pos, neg=neg, neu=neu)
        )
    return out
