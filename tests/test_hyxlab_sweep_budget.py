"""Per-series sweep budget + KXMVE exclusion (EXP-1275, 2026-08-12).

Post-294a5ae the truncated series resume honestly, but resume alone
cannot outrun density: MAX_MARKETS_PER_SERIES=2000 (effectively one
accepted window past it, ~3.4k) against measured close-time densities of
~7-8k/day (KXETH/KXETHD/KXSOLD/KXSOLE) and ~85k-7M/day (KXMVE*) means a
frontier that recedes until it pins at the purge horizon.

Measured decision (sweep_log 08-03..08-12, hyxlab.duckdb):
- The 3 chronic KXMVE truncators consumed 12,939 markets = 21% of one
  day's sweep (61,566 markets, 9.4h) while covering <3% of their own
  close-time — and NOTHING consumes KXMVE data in either repo (grep:
  zero hits in strategies/, simulator/, hyxlab/; hylshi's
  archive_reconcile_job EXCLUDES KXMVE% as 99.97% of the raw deficit).
  Keeping up would need 20-35 hours of sweep per day. Excluded.
- KXETH/KXETHD/KXSOLD/KXSOLE ARE consumed (hylshi decided_tail_scan +
  price_path_signal_scan read hyxlab.duckdb crypto directly) and keep-up
  is feasible: densities 6.7-7.8k/day vs ~3.4k delivered. KXBTC/KXBTCD
  sit at ~4.4-4.6k/day vs ~4.4k delivered — borderline. All six get a
  raised per-series budget; in steady state a series only fetches what
  settled since its watermark, so the raise costs catch-up transients,
  not a permanent 4x spend. The MVE exclusion frees more than it costs.
"""

from contextlib import contextmanager

from collector import sweep as sweep_mod


class _StubStore:
    def upsert_series(self, rows):
        pass


def _run_sweep(monkeypatch, series_list, **kw):
    """Run run_sweep with stubbed venue + store; capture sweep_series calls."""
    calls = []

    @contextmanager
    def fake_burst(db, lock_file=None):
        yield _StubStore()

    def fake_series_list(session):
        return series_list

    def fake_sweep_series(db, ticker, days, session, max_markets):
        calls.append((ticker, max_markets))
        return 0, 0, False

    monkeypatch.setattr(sweep_mod, "writer_burst", fake_burst)
    monkeypatch.setattr(sweep_mod.kalshi, "get_series_list", fake_series_list)
    monkeypatch.setattr(sweep_mod, "sweep_series", fake_sweep_series)
    sweep_mod.run_sweep("unused.duckdb", 2, sweep_mod.DEFAULT_CATEGORIES, session=object(), **kw)
    return calls


_SERIES = [
    {"ticker": "KXMVECROSSCATEGORY", "category": "Exotics"},
    {"ticker": "KXMVESPORTSMULTIGAMEEXTENDED", "category": "Exotics"},
    {"ticker": "KXHIGHNY", "category": "Climate and Weather"},
    {"ticker": "KXETH", "category": "Crypto"},
]


def test_kxmve_excluded_from_default_targets(monkeypatch):
    # Nothing in either repo reads KXMVE data, keeping up is arithmetically
    # impossible (~85k-7M markets/day vs ~3.4k/run), and the family ate 21%
    # of the 08-11 sweep. It must not be swept by default.
    calls = _run_sweep(monkeypatch, _SERIES)
    swept = {t for t, _ in calls}
    assert not any(t.startswith("KXMVE") for t in swept), (
        f"KXMVE series swept by default: {sorted(swept)} — deliberately-"
        f"excluded family (EXP-1275); use --series for a targeted repair"
    )
    assert {"KXHIGHNY", "KXETH"} <= swept  # exclusion must not over-reach


def test_explicit_series_flag_still_reaches_excluded_family(monkeypatch):
    # The exclusion is a default, not a ban: an operator naming the series
    # explicitly (--series, the targeted-repair path) gets it.
    calls = _run_sweep(monkeypatch, _SERIES, only_series=["KXMVECROSSCATEGORY"])
    assert [t for t, _ in calls] == ["KXMVECROSSCATEGORY"]


def test_dense_crypto_series_get_budget_override(monkeypatch):
    # KXETH settles ~7.6k/day; the flat 2000 budget delivers ~3.5k/run so
    # the frontier recedes ~13h/day. The override must lift KXETH above
    # its density while leaving ordinary series at the flat budget.
    calls = _run_sweep(monkeypatch, _SERIES)
    budgets = dict(calls)
    assert budgets["KXETH"] == sweep_mod.SERIES_MAX_MARKETS["KXETH"]
    assert budgets["KXETH"] >= 8000, "override must exceed the ~7.8k/day peak density"
    assert budgets["KXHIGHNY"] == sweep_mod.MAX_MARKETS_PER_SERIES


def test_explicit_max_markets_wins_over_override(monkeypatch):
    # A caller-chosen budget (--max-markets, e.g. a smoke test) applies
    # uniformly; the per-series raise only rides the default. Otherwise a
    # --limit smoke run that reaches KXETH burns an hour on 8000 markets.
    calls = _run_sweep(monkeypatch, _SERIES, max_markets=50)
    assert dict(calls)["KXETH"] == 50


def test_override_covers_all_six_dense_crypto_series():
    # The measured cannot-keep-up set (EXP-1271 + sweep_log 08-03..08-12).
    assert set(sweep_mod.SERIES_MAX_MARKETS) == {
        "KXBTC", "KXBTCD", "KXETH", "KXETHD", "KXSOLD", "KXSOLE",
    }
