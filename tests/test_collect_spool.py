"""A cycle that was FETCHED but not written must not be thrown away.

The 2026-09-10 07:08-07:36Z contention is the measurement behind this
file: the daily sweep held `data/hyxlab.duckdb`, seven consecutive
collector cycles waited out the full 240 s budget, exited 75, and each
discarded a complete fetch -- 426 Kalshi snapshots, 5,649 market infos
and 35 NWS forecasts already in memory and already stamped at fetch
time. `record_skip` counted all seven; nothing kept the rows.

The distinction the whole design rests on, and which the two amended
docstrings now carry: a cycle DROPPED before python started cannot be
backfilled, and that is what `acquire_writer_lock` has always said. A
cycle that loses the wait AFTER `fetch_cycle` can, because since EXP-957
the fetch runs first. Half of `qa_collect_skips`'s rationale was true of
the other half's cycle.

Three groups below: the codec (strict, because promote.sh can swap the
code under a spooled file), the buffer's behaviour under a live `main`,
and the QA check that decides whether recovery actually happened -- the
last including its inert-producer arm, since a recovery mechanism nobody
witnesses is the failure mode this repo keeps rediscovering (#43, #46).
"""

from __future__ import annotations

import builtins
import dataclasses
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from collector import collect, qa, spool
from hyxlab.models import Forecast, MarketInfo, Snapshot

TS = datetime(2026, 9, 10, 7, 8, 23, 123456, tzinfo=UTC)


def _cycle_rows() -> dict:
    """One of each model, including the None/date/datetime corners."""
    return {
        "infos": [
            MarketInfo(
                venue="kalshi",
                market_id="KXHIGHNY-26SEP10-B70",
                title="NY high",
                series="KXHIGHNY",
                close_time=datetime(2026, 9, 11, 3, 59, tzinfo=UTC),
                strike_type="between",
                floor_strike=69.5,
                cap_strike=70.5,
                result="",
                target_date=date(2026, 9, 10),
                open_time=None,
            )
        ],
        "kalshi_snaps": [
            Snapshot(venue="kalshi", market_id="A", ts=TS, yes_bid=0.4, yes_ask=None, volume=3.0)
        ],
        "poly_snaps": [Snapshot(venue="polymarket", market_id="P", ts=TS, last_price=0.61)],
        "forecasts": [
            Forecast(station="NYC", fetched_at=TS, target_date=date(2026, 9, 11), high_f=71)
        ],
    }


# --------------------------------------------------------------------------
# codec
# --------------------------------------------------------------------------


def test_round_trip_preserves_every_field_of_every_model(tmp_path):
    rows = _cycle_rows()
    path = spool.spool_cycle(
        TS, 2, spool_dir=str(tmp_path), log_path=str(tmp_path / "log.jsonl"), **rows
    )
    got = spool.decode_cycle(json.loads(path.read_text()))
    assert got["ts"] == TS
    assert got["errors"] == 2
    for name, expected in rows.items():
        assert got[name] == expected, name


def test_a_date_field_does_not_come_back_as_a_datetime(tmp_path):
    """`datetime` is a subclass of `date`, so a decoder that tests `date`
    first turns every timestamp in the archive into midnight. Both
    directions are pinned because both orderings type-check."""
    rows = _cycle_rows()
    path = spool.spool_cycle(
        TS, 0, spool_dir=str(tmp_path), log_path=str(tmp_path / "log.jsonl"), **rows
    )
    got = spool.decode_cycle(json.loads(path.read_text()))
    info = got["infos"][0]
    assert type(info.target_date) is date
    assert isinstance(info.close_time, datetime)
    assert info.close_time.hour == 3 and info.close_time.minute == 59
    assert got["kalshi_snaps"][0].ts == TS  # microseconds and tz intact


def test_row_models_covers_the_live_cycle_dataclass_exactly():
    """The mechanical half. A new list field on `Cycle` that nobody adds to
    ROW_MODELS would be silently dropped by the encoder -- rows fetched,
    spooled, and gone. This reddens on the dataclass edit instead."""
    fields = {f.name for f in dataclasses.fields(collect.Cycle)}
    assert set(spool.ROW_MODELS) | {"ts", "errors"} == fields


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda p: p.__setitem__("v", 99), id="version"),
        pytest.param(lambda p: p.__setitem__("surprise", 1), id="extra-cycle-key"),
        pytest.param(lambda p: p.pop("forecasts"), id="missing-cycle-key"),
        pytest.param(lambda p: p["infos"][0].__setitem__("new_field", 1), id="extra-row-key"),
        pytest.param(lambda p: p["infos"][0].pop("series"), id="missing-row-key"),
        pytest.param(
            lambda p: p["forecasts"][0].__setitem__("fetched_at", "nonsense"), id="bad-ts"
        ),
    ],
)
def test_a_payload_from_another_version_is_refused_not_partially_applied(tmp_path, mutate):
    """promote.sh can install new code while a spool file written by the old
    code is on disk, so reader and writer are routinely different versions.
    A tolerant decode would drop the field and write a row quietly missing
    data -- exactly the corruption the spool exists to prevent."""
    payload = spool.encode_cycle(TS, 0, **_cycle_rows())
    mutate(payload)
    with pytest.raises(spool.SpoolFormatError):
        spool.decode_cycle(payload)


def test_encode_refuses_a_cycle_whose_fields_it_does_not_know():
    with pytest.raises(spool.SpoolFormatError):
        spool.encode_cycle(TS, 0, infos=[])


# --------------------------------------------------------------------------
# buffer
# --------------------------------------------------------------------------


def _spool_at(tmp_path, ts, log=None, **kw):
    return spool.spool_cycle(
        ts,
        0,
        spool_dir=str(tmp_path),
        log_path=str(log or tmp_path / "log.jsonl"),
        infos=[],
        kalshi_snaps=[Snapshot(venue="kalshi", market_id="A", ts=ts)],
        poly_snaps=[],
        forecasts=[],
        **kw,
    )


def _events(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def test_files_sort_chronologically_and_no_partial_survives(tmp_path):
    for i in (3, 1, 2):
        _spool_at(tmp_path, TS + timedelta(minutes=5 * i))
    names = [p.name for p in spool.spooled(str(tmp_path))]
    assert names == sorted(names)
    assert [p.name for p in tmp_path.glob("*.partial")] == []


def test_the_cap_drops_the_oldest_and_says_so(tmp_path):
    log = tmp_path / "log.jsonl"
    for i in range(5):
        _spool_at(tmp_path, TS + timedelta(minutes=5 * i), log=log, max_files=3)
    kept = [p.name for p in spool.spooled(str(tmp_path))]
    assert len(kept) == 3
    assert kept[0].startswith("cycle-20260910T071823")  # the two oldest went
    dropped = [e for e in _events(log) if e["event"] == "dropped"]
    assert len(dropped) == 2, "a silently bounded queue is #46 with a cap on it"


def test_drain_replays_oldest_first_and_unlinks_only_after_a_successful_write(tmp_path):
    log = tmp_path / "log.jsonl"
    for i in range(3):
        _spool_at(tmp_path, TS + timedelta(minutes=5 * i), log=log)
    seen = []
    counts = spool.drain(
        lambda kw: (seen.append(kw["ts"]), True)[1], spool_dir=str(tmp_path), log_path=str(log)
    )
    assert seen == sorted(seen)
    assert counts["drained"] == 3 and counts["rows"] == 3 and counts["remaining"] == 0
    assert spool.spooled(str(tmp_path)) == []


def test_a_failed_write_keeps_the_cycle_for_the_next_firing(tmp_path):
    """The file existing at all is the promise that a bad archive costs a
    delay and not the rows."""
    log = tmp_path / "log.jsonl"
    for i in range(2):
        _spool_at(tmp_path, TS + timedelta(minutes=5 * i), log=log)
    counts = spool.drain(lambda kw: False, spool_dir=str(tmp_path), log_path=str(log))
    assert counts == {"drained": 0, "rows": 0, "failed": 1, "quarantined": 0, "remaining": 2}
    assert len(spool.spooled(str(tmp_path))) == 2


def test_an_undecodable_payload_is_quarantined_and_the_rest_still_drain(tmp_path):
    """Retrying an undecodable file every 5 minutes forever would bury the
    live cycle behind it; deleting it would destroy the only copy."""
    log = tmp_path / "log.jsonl"
    bad = _spool_at(tmp_path, TS, log=log)
    bad.write_text("{not json")
    _spool_at(tmp_path, TS + timedelta(minutes=5), log=log)
    seen = []
    counts = spool.drain(
        lambda kw: (seen.append(kw["ts"]), True)[1], spool_dir=str(tmp_path), log_path=str(log)
    )
    assert counts["quarantined"] == 1 and counts["drained"] == 1
    assert len(seen) == 1
    assert [p.name for p in tmp_path.glob("*.bad")] == [bad.name + ".bad"]
    assert any(e["event"] == "quarantined" for e in _events(log))


def test_the_drain_budget_never_stalls_at_zero(tmp_path):
    """A budget that could refuse the FIRST file would make a full spool
    permanent: every cycle would decline to start and the cap would drop the
    data the buffer was holding."""
    log = tmp_path / "log.jsonl"
    for i in range(4):
        _spool_at(tmp_path, TS + timedelta(minutes=5 * i), log=log)
    counts = spool.drain(lambda kw: True, spool_dir=str(tmp_path), log_path=str(log), budget_s=0.0)
    assert counts["drained"] == 1 and counts["remaining"] == 3


# --------------------------------------------------------------------------
# main(): the wiring, which is where the rows are actually saved or lost
# --------------------------------------------------------------------------


class _Store:
    def __init__(self) -> None:
        self.written: list[datetime] = []
        self.conn = self

    def execute(self, *a, **k):
        return None

    def counts(self):
        return {}

    def close(self):
        return None


def _run_main(monkeypatch, tmp_path, *, lock_ok: bool, store: _Store, cyc_ts: datetime):
    """One `--once` cycle with the network, the lock and the archive faked."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(collect, "load_watchlist", lambda p: {})
    monkeypatch.setattr(
        collect,
        "fetch_cycle",
        lambda wl, session=None: collect.Cycle(
            ts=cyc_ts, kalshi_snaps=[Snapshot(venue="kalshi", market_id="LIVE", ts=cyc_ts)]
        ),
    )
    monkeypatch.setattr(
        collect, "acquire_writer_lock", lambda **kw: _FakeLock() if lock_ok else None
    )
    monkeypatch.setattr(collect, "open_retry", lambda *a, **k: store)
    monkeypatch.setattr(collect, "read_holder", lambda p: None)
    monkeypatch.setattr(
        collect,
        "write_cycle",
        lambda st, cyc: (
            st.written.extend(s.ts for s in cyc.kalshi_snaps),
            {"errors": cyc.errors},
        )[1],
    )
    monkeypatch.setattr(collect.sys, "argv", ["collect", "--once"])
    monkeypatch.setattr(builtins, "print", lambda *a, **k: None)
    try:
        collect.main()
    except SystemExit as e:
        return e.code
    return 0


class _FakeLock:
    def fileno(self):
        return 0

    def close(self):
        return None


def test_a_cycle_that_loses_the_lock_is_spooled_not_discarded(monkeypatch, tmp_path):
    """The seven cycles of 2026-09-10, with the rows kept this time."""
    monkeypatch.setattr(collect.fcntl, "flock", lambda *a: None)
    code = _run_main(monkeypatch, tmp_path, lock_ok=False, store=_Store(), cyc_ts=TS)
    assert code == 75, "the skip is still a skip; qa_collect_skips still counts it"
    files = spool.spooled(str(tmp_path / "data/collect_spool"))
    assert len(files) == 1
    got = spool.decode_cycle(json.loads(files[0].read_text()))
    assert [s.market_id for s in got["kalshi_snaps"]] == ["LIVE"]
    assert Path(tmp_path / "data/collect_skips.jsonl").exists()


def test_the_next_successful_cycle_writes_the_spool_before_its_own_rows(monkeypatch, tmp_path):
    """Order is not cosmetic: the spool holds strictly EARLIER observations."""
    monkeypatch.setattr(collect.fcntl, "flock", lambda *a: None)
    _run_main(monkeypatch, tmp_path, lock_ok=False, store=_Store(), cyc_ts=TS)
    store = _Store()
    later = TS + timedelta(minutes=5)
    _run_main(monkeypatch, tmp_path, lock_ok=True, store=store, cyc_ts=later)
    assert store.written == [TS, later]
    assert spool.spooled(str(tmp_path / "data/collect_spool")) == []


# --------------------------------------------------------------------------
# qa_collect_spool
# --------------------------------------------------------------------------


def _run_check(**kw) -> tuple[set, str]:
    qa._failures.clear()
    printed: list[str] = []
    real = builtins.print
    builtins.print = lambda *a, **k: printed.append(" ".join(str(x) for x in a))
    try:
        qa.qa_collect_spool(**kw)
    finally:
        builtins.print = real
    failed = set(qa._failures)
    qa._failures.clear()
    return failed, "\n".join(printed)


NOW = qa.COLLECT_SPOOL_ARMED_AT + timedelta(hours=3)


def _logs(tmp_path, events=(), skips=()) -> dict:
    log = tmp_path / "spool.jsonl"
    skip = tmp_path / "skips.jsonl"
    d = tmp_path / "spool"
    d.mkdir(exist_ok=True)
    log.write_text("".join(json.dumps(e) + "\n" for e in events))
    skip.write_text("".join(json.dumps(s) + "\n" for s in skips))
    return {
        "log_path": str(log),
        "skip_path": str(skip),
        "spool_dir": str(d),
        "now": NOW,
    }


def test_a_quiet_box_passes_cleanly(tmp_path):
    failed, out = _run_check(**_logs(tmp_path))
    assert not failed, out
    assert "0 pending" in out


def test_a_skip_with_no_spool_beside_it_is_an_inert_producer(tmp_path):
    """The arm that makes the green line mean something. Both rows are
    written by the same branch of `collect.main`, one line apart."""
    kw = _logs(tmp_path, skips=[{"at": (NOW - timedelta(hours=1)).isoformat()}])
    failed, out = _run_check(**kw)
    assert failed and "PRODUCER INERT" in out


def test_skips_from_before_the_spool_shipped_do_not_accuse_it(tmp_path):
    """The 17 rows already on disk predate the module; the bound that
    excludes them falls out of the window on its own within a day."""
    kw = _logs(
        tmp_path, skips=[{"at": (qa.COLLECT_SPOOL_ARMED_AT - timedelta(minutes=1)).isoformat()}]
    )
    failed, out = _run_check(**kw)
    assert not failed, out


def test_a_spooled_skip_passes(tmp_path):
    at = (NOW - timedelta(hours=1)).isoformat()
    kw = _logs(
        tmp_path,
        events=[{"at": at, "event": "spooled"}, {"at": at, "event": "drained"}],
        skips=[{"at": at}],
    )
    failed, out = _run_check(**kw)
    assert not failed, out
    assert "1 spooled / 1 drained" in out


def test_a_cycle_dropped_at_the_cap_fails(tmp_path):
    at = (NOW - timedelta(hours=1)).isoformat()
    kw = _logs(
        tmp_path,
        events=[{"at": at, "event": "spooled"}, {"at": at, "event": "dropped"}],
        skips=[{"at": at}],
    )
    failed, out = _run_check(**kw)
    assert failed and "DROPPED" in out


def test_a_quarantined_payload_fails(tmp_path):
    kw = _logs(tmp_path)
    Path(kw["spool_dir"], "cycle-20260910T070000Z.json.bad").write_text("{")
    failed, out = _run_check(**kw)
    assert failed and "quarantined" in out


def test_a_spool_that_stopped_draining_fails(tmp_path):
    """Stuck is worse than deep: at the cap the buffer starts dropping, so
    the budget has to fire before the horizon it is protecting."""
    import os

    kw = _logs(tmp_path)
    f = Path(kw["spool_dir"], "cycle-20260910T070000Z.json")
    f.write_text("{}")
    old = NOW.timestamp() - (qa.COLLECT_SPOOL_STALE_H + 1) * 3600
    os.utime(f, (old, old))
    failed, out = _run_check(**kw)
    assert failed and "nothing is draining" in out
