"""Qwen 2.5 7B Instruct zero-shot sentiment scorer for Phase 0 §2.9.

Parallel to phase0/sentiment.py (FinBERT). Same CSV schema — downstream code
is scorer-agnostic: `news_id, label, score, pos, neg, neu, scored_at`.

Pre-registered prompt + decoding per phase0_testing.md §2.9.3:

    System: financial-sentiment classifier instructions (ag-focused).
    User:   Headline: "..."  Respond with P / N / Z.
    Decoding: greedy, 1 token max.
    Labels:  extract logits at first-gen-position for " P", " N", " Z" token ids,
             softmax over those three.

Output CSV: phase0/data/qwen_scores.csv (new — NOT replacing finbert_scores.csv).
"""

from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

import pandas as pd

from phase0.data_loaders import DATA_DIR
from phase0.news_loader import NEWS_CSV

MODEL_NAME = "Qwen/Qwen2.5-7B-Instruct"
MODEL_TAG = "qwen2.5-7b-instruct-zero-shot"
SCORES_CSV = DATA_DIR / "qwen_scores.csv"

SYSTEM_PROMPT = (
    "You are a financial sentiment classifier. Classify news headlines about "
    "publicly traded agricultural companies as positive, negative, or neutral, "
    "based on their likely short-term impact on the company's stock price."
)

USER_TEMPLATE = (
    'Headline: "{headline}"\n\n'
    "Respond with exactly one letter: P (positive), N (negative), or Z (neutral)."
)


@lru_cache(maxsize=1)
def _loaded_model():
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    tok = AutoTokenizer.from_pretrained(MODEL_NAME)
    tok.padding_side = "left"  # decoder-only: pad prepended so logits[-1] = next-token
    if tok.pad_token_id is None:
        tok.pad_token_id = tok.eos_token_id
    model = AutoModelForCausalLM.from_pretrained(
        MODEL_NAME,
        torch_dtype=torch.float16,
        device_map="cuda",
    ).eval()

    # Resolve P/N/Z token ids. In Qwen's BPE these are single tokens ("P", "N", "Z");
    # we resolve by looking up with a leading space (assistant responses typically
    # start after a whitespace). Fall back to no-space if that fails.
    def _resolve(label: str) -> int:
        for candidate in (label, " " + label):
            ids = tok.encode(candidate, add_special_tokens=False)
            if len(ids) == 1:
                return ids[0]
        # Multi-token fallback: take first subtoken of no-space encoding.
        ids = tok.encode(label, add_special_tokens=False)
        return ids[0]

    label_ids = {
        "pos": _resolve("P"),
        "neg": _resolve("N"),
        "neu": _resolve("Z"),
    }
    return tok, model, label_ids


def _render_prompts(headlines: list[str], tok) -> list[str]:
    prompts: list[str] = []
    for h in headlines:
        msgs = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_TEMPLATE.format(headline=h or "")},
        ]
        text = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
        prompts.append(text)
    return prompts


def score_batch(headlines: list[str], batch_size: int = 16) -> list[dict[str, float]]:
    """Return per-headline dicts: {pos, neg, neu, label, score}."""
    import torch

    tok, model, label_ids = _loaded_model()
    prompts = _render_prompts(headlines, tok)
    out: list[dict[str, float]] = []

    label_ids_tensor = torch.tensor(
        [label_ids["pos"], label_ids["neg"], label_ids["neu"]],
        device=model.device,
    )

    for i in range(0, len(prompts), batch_size):
        batch = prompts[i : i + batch_size]
        enc = tok(
            batch,
            return_tensors="pt",
            padding=True,
            truncation=True,
            max_length=512,
        ).to(model.device)
        with torch.no_grad():
            outputs = model(**enc)
        # logits[:, -1, :] = distribution over the next token (first generated)
        last_logits = outputs.logits[:, -1, :]
        label_logits = last_logits[:, label_ids_tensor]  # (batch, 3)
        probs = torch.softmax(label_logits, dim=-1).cpu().tolist()
        for pos, neg, neu in probs:
            label = max(
                (("positive", pos), ("negative", neg), ("neutral", neu)),
                key=lambda kv: kv[1],
            )
            out.append(
                {
                    "pos": float(pos),
                    "neg": float(neg),
                    "neu": float(neu),
                    "label": label[0],
                    "score": float(label[1]),
                }
            )
    return out


def score_corpus(
    news_path: Path = NEWS_CSV,
    out_path: Path = SCORES_CSV,
    batch_size: int = 16,
    refresh: bool = False,
) -> pd.DataFrame:
    """Score every unique headline in news_path via Qwen; cache to out_path.

    Incremental: only unscored news_ids are scored if the cache exists.
    """
    if not news_path.exists():
        raise FileNotFoundError(f"{news_path} not found — run phase0.news_loader first")

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

    headlines = unscored["headline"].fillna("").tolist()
    scored = score_batch(headlines, batch_size=batch_size)
    now = datetime.now(tz=UTC).isoformat()

    new_rows: list[dict[str, object]] = []
    for nid, s in zip(unscored["news_id"].tolist(), scored, strict=True):
        new_rows.append(
            {
                "news_id": nid,
                "label": s["label"],
                "score": s["score"],
                "pos": s["pos"],
                "neg": s["neg"],
                "neu": s["neu"],
                "scored_at": now,
            }
        )

    new_df = pd.DataFrame(new_rows)
    combined = pd.concat([existing, new_df], ignore_index=True) if existing is not None else new_df
    combined.to_csv(out_path, index=False)
    return combined


if __name__ == "__main__":
    df = score_corpus()
    print(f"qwen_scores.csv: {len(df)} rows")
    if len(df):
        counts = df["label"].value_counts().to_dict()
        print(f"  label distribution: {counts}")
