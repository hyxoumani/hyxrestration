"""`collector.trades_backfill` measures and prints its own holds.

The daily `hyxlab-tradepass` timer attaches the 5-minute collector's file
four ways -- the flocked schema burst, the proven-empty mark, the pending
read outside the flock, and one `_flush` per market batch -- and printed
a hand-written `done:` line with no hold in it: the #96/#102 shape
(`collector.signals` was the previous instance, 2026-10-16). The 10-10..
10-12 storm drain ran 210 minutes a day at ~11.9M prints per run and
charged its exclusion of the collector to nobody.

Now every attach goes through `held_open`, and the run's whole exclusion
prints as ONE `[tradepass] attach_wait:` line through the one renderer
(#95), db-scoped, after the `done:` line.
"""

from pathlib import Path

import collector.trades_backfill as tb
from tests.test_tradepass_deadline import _run_main


def test_the_run_prints_its_whole_exclusion_of_the_collectors_file(tmp_path, monkeypatch, capsys):
    """Two markets, one flush: schema burst + proven-empty mark + pending
    read + one `_flush` = 4 attaches, all four held and measured."""
    _run_main(tmp_path, monkeypatch, n_markets=2, deadline_min=0, fetch_cost_s=0.0)
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.startswith("[tradepass] attach_wait:")]
    assert len(lines) == 1, out
    assert "on t.duckdb 4 attaches" in lines[0], lines[0]
    assert "held 4 for" in lines[0] and "0 unmeasured" in lines[0], lines[0]
    assert out.index("[tradepass] done:") < out.index("[tradepass] attach_wait:")


def test_every_attach_in_the_module_is_a_measured_one():
    """A bare `open_retry`/`Store(` here is a hold nobody measured inside a
    module that now claims to report holds -- the exact thing the printed
    line would then under-count."""
    text = Path(tb.__file__).read_text()
    assert "open_retry(" not in text.replace("(open_retry +", "")
    assert "Store(" not in text
    assert text.count("held_open(") == 4
