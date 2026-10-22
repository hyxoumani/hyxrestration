"""`qa_collect_source_errors` -- the collect cycle's per-source except
branches, read back from the journal (mistakes #133).

The failure this closes: on 2026-10-22 NWS AUS raised on every tick for
21 cycles and nothing in QA could see it -- the cycle exited 0 and the
archive stayed fresh off the other four stations. Pinned: an episode past
the budget FAILS and names the source with its exception class, the
benign one-line-a-day band passes, an unreadable journal is a SKIP, and
journalctl's `-- No entries --` sentinel reads as zero errors."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

import collector.qa as qa

NOW = datetime(2026, 10, 23, 10, 0, tzinfo=UTC)

# Verbatim from the collector's journal, 2026-10-22.
NWS_LINE = (
    "2026-10-22T20:15:37+00:00 hyz python[2547846]: [collect] nws AUS: TypeError: "
    "int() argument must be a string, a bytes-like object or a real number, not 'NoneType'"
)
KALSHI_LINE = (
    "2026-10-22T07:12:41+00:00 hyz python[1921001]: [collect] kalshi KXLOWTNYC: HTTPError: "
    "503 Server Error: Service Unavailable for url: https://api.elections.kalshi.com/"
    "trade-api/v2/markets?series_ticker=KXLOWTNYC&status=open&limit=200"
)
CYCLE_LINE = (
    "2026-10-22T20:15:37+00:00 hyz python[2547846]: [collect] 2026-10-22T20:15:37.5+00:00 "
    "{'kalshi_snaps': 695, 'kalshi_markets': 6061, 'poly_snaps': 0, 'forecasts': 28, 'errors': 1}"
)


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(qa, "STATE", tmp_path / "sections.json")
    qa._failures.clear()
    qa._skipped.clear()
    qa._passes = 0
    yield
    qa._failures.clear()
    qa._skipped.clear()


def _run(journal, capsys):
    qa.qa_collect_source_errors(now=NOW)
    return set(qa._failures), list(qa._skipped), capsys.readouterr().out


def test_reader_groups_by_source_and_exception(monkeypatch):
    text = "\n".join([NWS_LINE] * 3 + [KALSHI_LINE, CYCLE_LINE])
    monkeypatch.setattr(qa, "_journal", lambda unit, since, until: text)
    assert qa.read_collect_source_errors(NOW, NOW) == {
        "nws AUS": {"TypeError": 3},
        "kalshi KXLOWTNYC": {"HTTPError": 1},
    }


def test_the_aus_episode_fails_and_is_named(monkeypatch, capsys):
    text = "\n".join([NWS_LINE] * 21 + [KALSHI_LINE] * 2)
    monkeypatch.setattr(qa, "_journal", lambda unit, since, until: text)
    failed, skipped, out = _run(text, capsys)
    assert failed == {"collect sources each failing <= 5 cycles in 24h"}
    assert "FAILING nws AUS 21 cycle(s) (TypeError)" in out
    assert "KXLOWTNYC" not in out  # under budget: not on the line
    assert skipped == []


def test_benign_band_passes_and_records(monkeypatch):
    text = "\n".join([KALSHI_LINE, CYCLE_LINE])
    monkeypatch.setattr(qa, "_journal", lambda unit, since, until: text)
    qa.qa_collect_source_errors(now=NOW)
    assert qa._failures == []
    assert qa._last_ok(qa.COLLECT_SOURCE_ERR_SECTION) is not None


def test_unreadable_journal_is_a_skip_not_a_pass(monkeypatch, capsys):
    monkeypatch.setattr(qa, "_journal", lambda unit, since, until: None)
    failed, skipped, out = _run(None, capsys)
    assert failed == set() and skipped == [qa.COLLECT_SOURCE_ERR_SECTION]
    assert out.startswith("SKIP")
    assert qa._last_ok(qa.COLLECT_SOURCE_ERR_SECTION) is None


def test_no_entries_sentinel_reads_as_zero(monkeypatch, capsys):
    monkeypatch.setattr(qa, "_journal", lambda unit, since, until: "-- No entries --\n")
    failed, skipped, out = _run(None, capsys)
    assert failed == set() and skipped == []
    assert "no source error line" in out
