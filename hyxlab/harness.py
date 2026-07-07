"""Run manifests (proposal C6, minimal core): every sim run leaves a
re-runnable, comparable record under data/runs/<run_id>/.

The data fingerprint is cheap (counts + ts range), not a full content
hash — episodes (C2 export) will carry the strong hash; this pins enough
to detect "same code, different data" drift meanwhile.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from hyxlab.models import Snapshot
from hyxlab.sim import SimResult


def _git_rev() -> str:
    try:
        rev = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
        return f"{rev}+dirty" if dirty else rev
    except Exception:
        return "unknown"


def data_fingerprint(snapshots: list[Snapshot]) -> dict:
    return {
        "n_snapshots": len(snapshots),
        "ts_min": str(snapshots[0].ts) if snapshots else None,
        "ts_max": str(snapshots[-1].ts) if snapshots else None,
    }


def write_manifest(
    result: SimResult,
    *,
    strategies: list[dict],
    fingerprint: dict,
    trial_context: dict | None = None,
    runs_dir: str | Path = "data/runs",
) -> Path:
    body = {
        "created_at": datetime.now(UTC).isoformat(),
        "git_rev": _git_rev(),
        "strategies": strategies,
        "data": fingerprint,
        "trial_context": trial_context or {"n_trials_in_family": 1},
        "metrics": result.metrics,
    }
    digest = hashlib.sha256(
        json.dumps([strategies, fingerprint], sort_keys=True, default=str).encode()
    ).hexdigest()[:8]
    run_id = f"{datetime.now(UTC):%Y%m%dT%H%M%S}_{digest}"
    out = Path(runs_dir) / run_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(json.dumps(body, indent=1, default=str))
    (out / "fills.json").write_text(
        json.dumps([asdict(f) for f in result.fills], indent=0, default=str)
    )
    return out / "manifest.json"
