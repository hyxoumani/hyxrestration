"""Configuration loader. Reads .env at the project root; fails loudly on missing required keys."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


class ConfigError(RuntimeError):
    pass


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"missing required env var {name!r}. copy .env.example to .env and fill it in."
        )
    return value


def _optional(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


@dataclass(frozen=True)
class Config:
    alpaca_key: str
    alpaca_secret: str
    db_path: Path
    reports_dir: Path

    @classmethod
    def load(cls, require_alpaca: bool = True) -> Config:
        """Load config. Set require_alpaca=False for tooling that doesn't hit Alpaca."""
        if require_alpaca:
            alpaca_key = _require("ALPACA_KEY")
            alpaca_secret = _require("ALPACA_SECRET")
        else:
            alpaca_key = os.environ.get("ALPACA_KEY", "").strip()
            alpaca_secret = os.environ.get("ALPACA_SECRET", "").strip()

        db_path = Path(_optional("HYX_DB_PATH", str(PROJECT_ROOT / "data" / "hyx.duckdb")))
        reports_dir = Path(_optional("HYX_REPORTS_DIR", str(PROJECT_ROOT / "reports")))

        db_path.parent.mkdir(parents=True, exist_ok=True)
        reports_dir.mkdir(parents=True, exist_ok=True)

        return cls(
            alpaca_key=alpaca_key,
            alpaca_secret=alpaca_secret,
            db_path=db_path,
            reports_dir=reports_dir,
        )
