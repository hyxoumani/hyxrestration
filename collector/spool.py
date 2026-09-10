"""Durable buffer for a cycle that was FETCHED but could not be WRITTEN.

WHAT THIS IS NOT. It is not a retry, and it does not make the collector
wait longer for the archive: the 240 s budget in `collect.LOCK_WAIT_S` is
a budget for the whole cycle, and a cycle still running when the next
5-min firing arrives is worse than a late write.

THE DISTINCTION IT RESTS ON. `acquire_writer_lock` has said since
2026-08-02 that "a dropped cycle is an unrecoverable hole in the 5-min
tape; the collector cannot backfill a snapshot it never took" — and that
is TRUE of the cycle the old `flock -n` wrapper dropped before python
started. It is FALSE of the cycle `main` drops today: the fetch runs
first (EXP-957), so by the time the lock is refused the rows exist, are
complete, and carry the timestamp they were taken at. Measured on the
2026-09-10 07:08-07:36Z contention (7 skipped cycles, all with the daily
sweep as the recorded holder): each discarded cycle held 426 Kalshi
snapshots, 5,649 market infos and 35 NWS forecasts already in memory —
~1.8 MB per cycle (EXP-957's figure) thrown away for a lock that cleared
in minutes. Those 7 holes were recoverable and were not recovered.

NO-LOOKAHEAD IS PRESERVED BY CONSTRUCTION, NOT BY CARE. Every row a
cycle carries is stamped at FETCH time — `Snapshot.ts` is `cyc.ts`,
`Forecast.fetched_at` comes from the NWS pull — so writing it an hour
later inserts the same row it would have inserted on time. Nothing in
the archive records when a row reached the file, and nothing may: the
replayer keys on those stamps. A spooled cycle is a LATE WRITE, never a
late observation.

STRICT DECODE, AND WHY IT MATTERS HERE MORE THAN USUAL. `scripts/
promote.sh` can install new code while a spool file written by the old
code is still on disk, so the reader and the writer of these files are
routinely different versions. A tolerant decode would then drop a field
the new model gained and write a row that is quietly missing data — the
one failure mode this module exists to prevent. So a payload whose keys
do not EXACTLY match the live dataclass is quarantined as `.bad` and
reported, never partially applied.

BOUNDED, BECAUSE THE FAILURE IT COVERS IS THE UNBOUNDED ONE. Contention
lasting hours is an incident, not an overlap; `MAX_FILES` caps the spool
at 2 h of cycles (~43 MB at the measured 1.8 MB) and drops the OLDEST
beyond it, because the newest cycles are the ones whose neighbours are
still missing. Every drop is recorded — a silently bounded queue is
mistake #46 with a cap on it.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import fields as dataclass_fields
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, get_args, get_type_hints

from hyxlab.models import Forecast, MarketInfo, Snapshot

__all__ = [
    "DRAIN_BUDGET_S",
    "MAX_FILES",
    "ROW_MODELS",
    "SPOOL_DIR",
    "SPOOL_LOG",
    "SpoolFormatError",
    "decode_cycle",
    "drain",
    "encode_cycle",
    "note",
    "spool_cycle",
    "spooled",
]

SPOOL_DIR = "data/collect_spool"
#: One event per spool/drain/drop/quarantine — never per row. Read by
#: `collector.qa.qa_collect_spool`; per #46 the count has a consumer
#: before it has a producer.
SPOOL_LOG = "data/collect_spool.jsonl"

#: 2 h of 5-min cycles. See the module docstring: the cap bounds the
#: incident, it does not bound the overlap.
MAX_FILES = 24

#: Wall-clock the DRAIN may add to a cycle. The cycle it rides on already
#: costs ~20 s (fetch 14 s + write 3 s measured 2026-09-10) against a
#: 300 s period, and a drain that overran would recreate the stacking the
#: lock budget exists to prevent. One file is always attempted regardless,
#: or a full spool with a slow archive would never make progress.
DRAIN_BUDGET_S = 60.0

#: The list-valued fields of `collect.Cycle` and the model each holds.
#: `tests/test_collect_spool.py` asserts this covers `Cycle` exactly, so a
#: new field on the dataclass reddens here instead of being silently
#: dropped by the encoder.
ROW_MODELS: dict[str, type] = {
    "infos": MarketInfo,
    "kalshi_snaps": Snapshot,
    "poly_snaps": Snapshot,
    "forecasts": Forecast,
}

FORMAT_VERSION = 1


class SpoolFormatError(ValueError):
    """A spool payload does not match the live models. Never applied."""


def _encode_value(v: Any) -> Any:
    # datetime BEFORE date: datetime is a subclass, and the reverse order
    # would truncate every timestamp in the archive to a day.
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return v


def _decoder(hint: Any):
    """The parser for one field, chosen from the ANNOTATION's real types.

    Not from `str(hint)`: `date | None` renders as
    `typing.Optional[datetime.date]`, so a substring test for "datetime"
    matches the MODULE name and every `date` field comes back a
    `datetime` at midnight. Caught by
    `test_a_date_field_does_not_come_back_as_a_datetime` -- the arm exists
    because both orderings type-check and only one round-trips.
    """
    args = get_args(hint) or (hint,)
    # datetime BEFORE date, for the subclass reason in `_encode_value`.
    if any(a is datetime for a in args):
        return datetime.fromisoformat
    if any(a is date for a in args):
        return date.fromisoformat
    return None


def _row_codec(cls: type) -> tuple[tuple[str, ...], dict[str, Any]]:
    hints = get_type_hints(cls)
    names = tuple(f.name for f in dataclass_fields(cls))
    decoders = {n: _decoder(hints[n]) for n in names}
    return names, decoders


def encode_cycle(ts: datetime, errors: int, **rows: list) -> dict:
    """Serialize one cycle. `rows` keys must be exactly `ROW_MODELS`."""
    if set(rows) != set(ROW_MODELS):
        raise SpoolFormatError(f"cycle fields {sorted(rows)} != {sorted(ROW_MODELS)}")
    payload: dict[str, Any] = {"v": FORMAT_VERSION, "ts": ts.isoformat(), "errors": errors}
    for name, cls in ROW_MODELS.items():
        names, _ = _row_codec(cls)
        payload[name] = [{n: _encode_value(getattr(row, n)) for n in names} for row in rows[name]]
    return payload


def decode_cycle(payload: dict) -> dict:
    """Payload -> kwargs for `collect.Cycle`. Raises `SpoolFormatError`.

    Strict on purpose (see module docstring): an unknown or missing model
    field means the file was written by a different version of the code,
    and half a row is worse than no row.
    """
    if not isinstance(payload, dict) or payload.get("v") != FORMAT_VERSION:
        raise SpoolFormatError(f"unsupported spool version {payload.get('v')!r}")
    expected = {"v", "ts", "errors", *ROW_MODELS}
    if set(payload) != expected:
        raise SpoolFormatError(f"cycle keys {sorted(payload)} != {sorted(expected)}")
    try:
        out: dict[str, Any] = {
            "ts": datetime.fromisoformat(payload["ts"]),
            "errors": int(payload["errors"]),
        }
    except (TypeError, ValueError) as e:
        raise SpoolFormatError(f"bad cycle header: {e}") from e
    for name, cls in ROW_MODELS.items():
        names, decoders = _row_codec(cls)
        rows = []
        for raw in payload[name]:
            if not isinstance(raw, dict) or set(raw) != set(names):
                raise SpoolFormatError(
                    f"{name}: row keys {sorted(raw) if isinstance(raw, dict) else raw!r}"
                    f" != {sorted(names)}"
                )
            try:
                rows.append(
                    cls(
                        **{
                            n: (
                                decoders[n](raw[n])
                                if decoders[n] and raw[n] is not None
                                else raw[n]
                            )
                            for n in names
                        }
                    )
                )
            except (TypeError, ValueError) as e:
                raise SpoolFormatError(f"{name}: {type(e).__name__}: {e}") from e
        out[name] = rows
    return out


def note(event: str, path: str | None = None, **extra: Any) -> None:
    """Append one spool event to the sidecar journal.

    Beside the archive rather than in it, for `record_skip`'s reason: the
    archive is precisely what could not be opened.
    """
    p = Path(path or SPOOL_LOG)
    p.parent.mkdir(exist_ok=True)
    rec = {"at": datetime.now(UTC).isoformat(), "event": event, **extra}
    with open(p, "a") as fh:
        fh.write(json.dumps(rec) + "\n")


def spooled(spool_dir: str | None = None) -> list[Path]:
    """Spooled cycles, OLDEST FIRST. Filenames sort chronologically."""
    d = Path(spool_dir or SPOOL_DIR)
    if not d.is_dir():
        return []
    return sorted(d.glob("cycle-*.json"))


def spool_cycle(
    ts: datetime,
    errors: int,
    spool_dir: str | None = None,
    log_path: str | None = None,
    max_files: int = MAX_FILES,
    **rows: list,
) -> Path:
    """Write one unwritable cycle to disk and prune the spool to `max_files`.

    The write is tmp-then-`os.replace` so a crash mid-serialize cannot
    leave a truncated payload that the next drain would quarantine — the
    partial file simply never acquires the `cycle-*.json` name.
    """
    d = Path(spool_dir or SPOOL_DIR)
    d.mkdir(parents=True, exist_ok=True)
    payload = encode_cycle(ts, errors, **rows)
    name = f"cycle-{ts.astimezone(UTC):%Y%m%dT%H%M%S%f}Z.json"
    target = d / name
    tmp = d / (name + ".partial")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, target)
    n_rows = sum(len(v) for v in rows.values())
    note("spooled", path=log_path, file=name, rows=n_rows)

    existing = spooled(str(d))
    for stale in existing[: max(0, len(existing) - max_files)]:
        stale.unlink(missing_ok=True)
        # The OLDEST goes: its neighbours in the tape are the ones most
        # likely already written, and the newest cycle is the one whose
        # gap is still open.
        note("dropped", path=log_path, file=stale.name, reason="spool full")
    return target


def drain(
    write: Any,
    spool_dir: str | None = None,
    log_path: str | None = None,
    budget_s: float = DRAIN_BUDGET_S,
) -> dict:
    """Replay spooled cycles oldest-first through `write(kwargs) -> bool`.

    A file is unlinked only after `write` reports success, so an archive
    that fails mid-drain leaves the cycle for the next firing rather than
    losing it — the whole point of the file existing. A file that cannot
    be DECODED is quarantined as `.bad` and skipped: it is not transient,
    and retrying it every 5 minutes forever would bury the live cycle.
    """
    counts = {"drained": 0, "rows": 0, "failed": 0, "quarantined": 0, "remaining": 0}
    files = spooled(spool_dir)
    t0 = time.monotonic()
    for i, f in enumerate(files):
        if i and time.monotonic() - t0 >= budget_s:
            break
        try:
            kwargs = decode_cycle(json.loads(f.read_text()))
        except (SpoolFormatError, json.JSONDecodeError, OSError) as e:
            bad = f.with_suffix(f.suffix + ".bad")
            os.replace(f, bad)
            counts["quarantined"] += 1
            note("quarantined", path=log_path, file=f.name, error=f"{type(e).__name__}: {e}")
            print(f"[spool] {f.name} QUARANTINED: {type(e).__name__}: {e}")
            continue
        if not write(kwargs):
            counts["failed"] += 1
            break
        n_rows = sum(len(v) for k, v in kwargs.items() if k in ROW_MODELS)
        f.unlink(missing_ok=True)
        counts["drained"] += 1
        counts["rows"] += n_rows
        note(
            "drained", path=log_path, file=f.name, rows=n_rows, spooled_at=kwargs["ts"].isoformat()
        )
    counts["remaining"] = len(spooled(spool_dir))
    return counts
