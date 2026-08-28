"""Who is allowed to load the whole `markets` table into a process.

EXP-1378, the rung above the memory ladder's DuckDB work. Rungs 9, 10 and
12 bounded what the ENGINE may hold (`memory_limit`) and what it may
spill (`max_temp_directory_size`). Neither reaches the Python heap the
same cgroup kills the process for, and the ladder's next rung was named
as `run_l2`'s unbounded equity curve.

**MEASURED FIRST, AND THE NAMED SUSPECT WAS NOT THE CONSUMER.** On the
live 15.7 GB stream archive, a real 3 h replay (121,054 snapshots):

  * the full in-process equity curve costs **144.4 bytes/row**, 16.7 MiB
    — 3.1 MiB per window-hour at a quiet hour, 5.6 MiB at a busy one;
  * `store.markets()`, unfiltered, costs **1.32 GiB resident / 1.56 GiB
    traced peak** — 1,869,512 MarketInfo objects — allocated BEFORE the
    first event replays.

That is 80x the thing the ladder was pointed at, and it is worse than
big: the curve is sized by the operator's window, while the metadata
load is sized by the ARCHIVE. Nothing a caller chooses bounds it, and it
grows on its own (486k rows / ~430 MB on 2026-08-07, 1.87M / 1.32 GiB
three weeks later). The 3 h window touches **714 of the 1.87M markets:
0.04%**.

  DERIVED — every `.markets(...)` call in the four packages either
            BOUNDS the load (`market_ids=` or `alive_days=`) or is in an
            ENUMERATED set of unbounded offline one-shots, by AST. A new
            unbounded caller — especially one inside a `MemoryMax` unit
            — is a red suite rather than a discovery after the OOM
            (mistake #37: sweep by ROLE, then pin the answer).
  CLAIMED — verified by RUNNING against a real Store: a bounded load
            returns the pinned rows and NOTHING else, field-for-field
            identical to what the unfiltered load returns for those same
            keys, so the cheap path is not a different answer; and the
            empty id set loads nothing, since "no ids" degrading to "all
            markets" is exactly the mutation that silently restores the
            1.32 GiB.

The two sites fixed at this rung are the two that run against the live
archive: `simulator.run_l2` (whose reachable set is precisely the ids
`book_events` carries over its own window) and `simulator.simui`, which
is a `MemoryMax=1G` service that was loading 1.32 GiB on a page view and
had simply not been asked for one since the archive outgrew its cap.
"""

import ast
from datetime import timedelta
from pathlib import Path

from hyxlab.models import MarketInfo
from hyxlab.store import Store
from simulator.simui.session import _try_load_markets
from tests.test_hyxlab_run_l2 import T0, _build_archives

PACKAGES = ("collector", "simulator", "strategies", "hyxlab")

# Kwargs that make a load proportional to something the CALLER chose
# rather than to the size of the archive. `venue=` is not one of them:
# kalshi alone is 1.79M of the 1.87M rows.
BOUNDING = ("market_ids", "alive_days")

# Unbounded on purpose, and each one an offline one-shot run by hand
# outside any cgroup, on a laptop-sized window. They are LISTED, not
# waived: deleting a line here is how a site graduates, and adding one
# is a deliberate act with a reviewer.
UNBOUNDED_ONE_SHOTS = {
    "simulator/divergence.py",
    "simulator/run_backtest.py",
    "simulator/run_favlong.py",
    "simulator/run_favlong_tight.py",
    "simulator/run_sim.py",
}


def _markets_calls() -> dict[str, list[ast.Call]]:
    out: dict[str, list[ast.Call]] = {}
    for pkg in PACKAGES:
        for path in sorted(Path(pkg).rglob("*.py")):
            tree = ast.parse(path.read_text())
            calls = [
                c
                for c in ast.walk(tree)
                if isinstance(c, ast.Call)
                and isinstance(c.func, ast.Attribute)
                and c.func.attr == "markets"
            ]
            if calls:
                out[str(path)] = calls
    return out


def test_every_markets_load_is_bounded_or_enumerated():
    """The rule. A `.markets()` with no bounding kwarg pulls the entire
    table — 1.32 GiB and growing — into the calling process."""
    calls = _markets_calls()
    assert calls, "no `.markets()` call sites found; the sweep is broken"
    unbounded = {
        module
        for module, cs in calls.items()
        if any(not any(k.arg in BOUNDING for k in c.keywords) for c in cs)
    }
    assert unbounded == UNBOUNDED_ONE_SHOTS, (
        f"unbounded `.markets()` loads changed: new {sorted(unbounded - UNBOUNDED_ONE_SHOTS)}, "
        f"fixed {sorted(UNBOUNDED_ONE_SHOTS - unbounded)}"
    )


def test_the_two_live_archive_readers_bound_their_load():
    """Named, not merely absent from the list above: `run_l2` reads the
    live archive by hand and `simui` reads it from inside a
    `MemoryMax=1G` unit, so their bound is the point of the rung."""
    calls = _markets_calls()
    for module in ("simulator/run_l2.py", "simulator/simui/session.py"):
        assert module in calls, f"{module} no longer loads market metadata"
        for c in calls[module]:
            assert any(k.arg == "market_ids" for k in c.keywords), (
                f"{module}:{c.lineno} loads market metadata without an id bound"
            )


def test_bounded_load_is_the_unfiltered_load_restricted(tmp_path):
    """CLAIM, by running. The cheap path must be the SAME answer, not a
    cheaper different one — and the empty id set must load nothing."""
    store = Store(tmp_path / "a.duckdb")
    store.upsert_markets(
        [
            MarketInfo(venue="kalshi", market_id=f"M{i}", title=f"t{i}", series="S")
            for i in range(5)
        ]
        + [MarketInfo(venue="polymarket", market_id="M1", title="poly")]
    )

    everything = store.markets()
    assert len(everything) == 6

    picked = store.markets(market_ids=["M1", "M3"])
    assert set(picked) == {("kalshi", "M1"), ("kalshi", "M3"), ("polymarket", "M1")}
    for key, info in picked.items():
        assert info == everything[key]  # field for field, not just present

    # `venue=` composes as AND, and the empty set is a real bound.
    assert set(store.markets(venue="kalshi", market_ids=["M1", "M3"])) == {
        ("kalshi", "M1"),
        ("kalshi", "M3"),
    }
    assert store.markets(market_ids=[]) == {}

    # `include=` still pins past the id bound — a held position whose
    # settlement lands outside the caller's id set must not vanish.
    pinned = store.markets(market_ids=["M1"], include=[("kalshi", "M4")])
    assert ("kalshi", "M4") in pinned
    store.close()


def test_run_l2_loads_only_markets_its_window_can_touch(tmp_path, monkeypatch):
    """Behavioural, on the real runner: a market the archive knows and
    the window never sees must not reach the Simulator."""
    from simulator import run_l2 as mod

    archive_db, stream_db = _build_archives(tmp_path)
    store = Store(archive_db)
    store.upsert_markets(
        [MarketInfo(venue="kalshi", market_id="KXNOTHING-26JUL08-B1.5", series="KXNOTHING")]
    )
    store.close()

    seen = {}
    real = mod.Simulator

    def spy(markets, *a, **kw):
        seen["markets"] = markets
        return real(markets, *a, **kw)

    monkeypatch.setattr(mod, "Simulator", spy)
    _, result, n_snaps = mod.run_l2(
        ["hylshi_fade"],
        (T0 - timedelta(minutes=10)).replace(tzinfo=None),
        (T0 + timedelta(minutes=10)).replace(tzinfo=None),
        stream_db=str(stream_db),
        archive_db=str(archive_db),
        latency=0.0,
        runs_dir=str(tmp_path / "runs"),
    )
    assert n_snaps and result.fills, "the replay must still trade"
    assert set(seen["markets"]) == {
        ("kalshi", "KXLOWTPHIL-26JUL08-B62.5"),
        ("kalshi", "KXHIGHCHI-26JUL08-B85.5"),
    }


def test_simui_asks_only_for_the_ids_it_renders(tmp_path):
    """simui's three call sites each already know their id set; the
    loader must honour it, and must distinguish an unreachable archive
    (None) from one with no metadata for those ids ({})."""
    archive_db = tmp_path / "a.duckdb"
    store = Store(archive_db)
    store.upsert_markets(
        [
            MarketInfo(venue="kalshi", market_id="EV-M1", title="one"),
            MarketInfo(venue="kalshi", market_id="EV-M2", title="two"),
        ]
    )
    store.close()

    assert set(_try_load_markets(str(archive_db), ["EV-M1"])) == {("kalshi", "EV-M1")}
    assert _try_load_markets(str(archive_db), ["EV-NOPE"]) == {}  # reachable, no rows
    assert _try_load_markets(str(tmp_path / "missing.duckdb"), ["EV-M1"]) is None
