"""Polymarket archival: flatteners pinned to probed API shapes, store
round-trips, YES-normalization of trades."""

from datetime import UTC, datetime

from collector.venues.polymarket import (
    gamma_market_info,
    iter_markets_by_volume,
    poly_trade_row,
    price_rows,
    token_pair,
)
from hyxlab.store import Store

GAMMA_ROW = {
    "id": "558963",
    "question": "Will Morocco win the 2026 FIFA World Cup?",
    "conditionId": "0x37a6de1b",
    "slug": "morocco-2026-wc",
    "endDate": "2026-07-20T00:00:00Z",
    "volumeNum": "138647014.5",
    "liquidityNum": "2310299.8",
    "clobTokenIds": '["69910730841487615802", "5015436051822"]',
    "closed": False,
}

TRADE_ROW = {
    "proxyWallet": "0xbc13c7d5",
    "side": "BUY",
    "asset": "4619766065218912142666",
    "conditionId": "0xe3e3c90a",
    "size": 2000,
    "price": 0.001,
    "timestamp": 1783480483,
    "outcome": "Yes",
    "transactionHash": "0xa671a72f",
}


def test_gamma_market_info_open_market():
    info = gamma_market_info(GAMMA_ROW)
    assert info.venue == "polymarket"
    assert info.market_id == "0x37a6de1b"
    assert info.result == ""
    assert info.close_time == datetime(2026, 7, 20, tzinfo=UTC)
    assert token_pair(GAMMA_ROW) == ("69910730841487615802", "5015436051822")


def test_gamma_market_info_settled_result_from_outcome_prices():
    closed = {**GAMMA_ROW, "closed": True, "outcomePrices": '["0", "1"]'}
    assert gamma_market_info(closed).result == "no"
    closed = {**GAMMA_ROW, "closed": True, "outcomePrices": '["1", "0"]'}
    assert gamma_market_info(closed).result == "yes"


def test_poly_trade_yes_normalization():
    # BUY Yes @ 0.001 -> yes_price 0.001, aggressor toward yes
    r = poly_trade_row(TRADE_ROW)
    assert (r[0], r[1]) == ("polymarket", "0xe3e3c90a")
    assert r[4] == 0.001 and r[6] == "yes"
    # SELL No @ 0.97 -> yes_price 0.03, SELLing No = pressure toward yes
    r = poly_trade_row({**TRADE_ROW, "side": "SELL", "outcome": "No", "price": 0.97})
    assert r[4] == 0.03 and r[6] == "yes"
    # BUY No @ 0.97 -> yes_price 0.03, aggressor toward no
    r = poly_trade_row({**TRADE_ROW, "side": "BUY", "outcome": "No", "price": 0.97})
    assert r[4] == 0.03 and r[6] == "no"


def test_poly_trade_rows_dedup_in_store(tmp_path):
    store = Store(tmp_path / "t.duckdb")
    row = poly_trade_row(TRADE_ROW)
    assert store.insert_trades([row]) == 1
    assert store.insert_trades([row]) == 0
    store.close()


def test_price_rows_roundtrip_and_watermark(tmp_path):
    hist = [{"t": 1783480000, "p": 0.42}, {"t": 1783483600, "p": 0.43}]
    rows = price_rows("tok1", "0xcond", "yes", hist)
    store = Store(tmp_path / "t.duckdb")
    assert store.insert_poly_prices(rows) == 2
    assert store.insert_poly_prices(rows) == 0  # (token, ts) dedup
    wm = store.poly_price_watermarks()
    assert wm["tok1"] == datetime.fromtimestamp(1783483600, tz=UTC).replace(tzinfo=None)
    store.close()


class _KeysetSession:
    """Canned Gamma /markets/keyset responses (shapes probed 2026-07-08:
    offset paging rejects offset > 2000; keyset chains via after_cursor)."""

    def __init__(self, pages, error_first=False):
        self.pages = pages  # cursor-or-None -> response dict
        self.calls = []
        self._error_next = error_first

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params)))

        class R:
            def __init__(self, body):
                self._body = body

            def json(self):
                return self._body

        assert url.endswith("/markets/keyset")
        if self._error_next:
            self._error_next = False
            return R({"type": "validation error", "error": "boom"})
        return R(self.pages[params.get("after_cursor")])


def _mkt(mid, vol):
    return {"id": mid, "volumeNum": vol}


def test_iter_markets_keyset_follows_cursor_and_stops_at_threshold(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)
    sess = _KeysetSession(
        {
            None: {"markets": [_mkt("1", 500.0), _mkt("2", 200.0)], "next_cursor": "C1"},
            "C1": {"markets": [_mkt("3", 150.0), _mkt("4", 50.0)], "next_cursor": "C2"},
        }
    )
    out = iter_markets_by_volume(100.0, session=sess)
    assert [m["id"] for m in out] == ["1", "2", "3"]  # 50 < threshold dropped
    assert len(sess.calls) == 2  # below-threshold tail ends the walk
    assert sess.calls[0][1]["volume_num_min"] == "100.0"  # server-side filter too
    assert sess.calls[1][1]["after_cursor"] == "C1"


def test_iter_markets_keyset_retries_error_page_once(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)
    sess = _KeysetSession(
        {None: {"markets": [_mkt("1", 500.0)], "next_cursor": None}},
        error_first=True,
    )
    out = iter_markets_by_volume(100.0, session=sess)
    assert [m["id"] for m in out] == ["1"]


def test_iter_markets_keyset_null_markets_page_ends_walk(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)
    sess = _KeysetSession({None: {"markets": None, "next_cursor": "C1"}})
    assert iter_markets_by_volume(100.0, session=sess) == []


def test_iter_markets_keyset_persistent_error_logs_incomplete(monkeypatch, capsys):
    """The Gamma-offset regression class: a walk that stops early must
    say so in the same run, not wait for the QA tripwire a day later."""
    monkeypatch.setattr("time.sleep", lambda _s: None)

    class _AlwaysError(_KeysetSession):
        def get(self, url, params=None, timeout=None):
            self.calls.append((url, dict(params)))

            class R:
                status_code = 200

                def json(self):
                    return {"type": "validation error"}

            return R()

    sess = _AlwaysError({})
    out = iter_markets_by_volume(100.0, session=sess)
    assert out == []
    # 4-attempt ladder (~65s). The long ladder existed for a fault
    # window the 2026-08-22 probes falsified; the tail fault it was
    # chasing is persistent, so waiting longer buys nothing and the
    # chain restart is the real recovery. Page 0 has nothing collected
    # below which to restart, so this walk is genuinely INCOMPLETE.
    assert len(sess.calls) == 4
    logged = capsys.readouterr().out
    assert "INCOMPLETE" in logged
    # Each failed attempt logs status + clock so the window's length
    # gets measured from the journal.
    assert "attempt 1 failed" in logged
    assert "cursor None" in logged


def test_iter_markets_keyset_non_json_body_retries_then_incomplete(monkeypatch, capsys):
    """A Gamma 5xx with an HTML body must hit the same retry/INCOMPLETE
    path, not raise out of the walk."""
    monkeypatch.setattr("time.sleep", lambda _s: None)

    class _NonJson(_KeysetSession):
        def get(self, url, params=None, timeout=None):
            self.calls.append((url, dict(params)))

            class R:
                status_code = 502

                def json(self):
                    raise ValueError("no JSON")

            return R()

    sess = _NonJson({})
    out = iter_markets_by_volume(100.0, session=sess)
    assert out == []
    assert len(sess.calls) == 4  # shortened ladder; see the restart tests below
    assert "INCOMPLETE" in capsys.readouterr().out


class _TailFaultSession(_KeysetSession):
    """Gamma's tail fault: the last page of a chain 500s instead of
    carrying `next_cursor: null`. A fresh chain ceilinged below the
    last collected row serves that remainder (probed 2026-08-22)."""

    def __init__(self):
        super().__init__({})
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params)))
        ceiling = params.get("volume_num_max")

        class R:
            status_code = 500

            def __init__(self, body=None):
                self._body = body

            def json(self):
                if self._body is None:
                    raise ValueError("no JSON")
                return self._body

        if ceiling is None:
            if not params.get("after_cursor"):
                return R({"markets": [_mkt("1", 500.0), _mkt("2", 300.0)], "next_cursor": "C1"})
            return R()  # tail of the first chain: persistent 500
        # Restarted chain: the ceiling row comes back as a duplicate.
        return R({"markets": [_mkt("2", 300.0), _mkt("3", 200.0)], "next_cursor": None})


def test_iter_markets_keyset_restarts_chain_below_last_row(monkeypatch, capsys):
    monkeypatch.setattr("time.sleep", lambda _s: None)
    sess = _TailFaultSession()
    out = iter_markets_by_volume(100.0, session=sess)

    # The tail is recovered and the boundary row is not double-counted.
    assert [m["id"] for m in out] == ["1", "2", "3"]
    logged = capsys.readouterr().out
    assert "chain restart 1 below volume 300" in logged
    assert "INCOMPLETE" not in logged
    # The restarted chain drops the cursor and ceilings on the last row.
    restarted = [c for _u, c in sess.calls if "volume_num_max" in c]
    assert restarted and all("after_cursor" not in c for c in restarted)
    assert restarted[0]["volume_num_max"] == "300.0"


def test_iter_markets_keyset_restart_requires_strict_progress(monkeypatch, capsys):
    """A restart that cannot lower the ceiling must give up, not spin."""
    monkeypatch.setattr("time.sleep", lambda _s: None)

    class _NeverPastTheCeiling(_TailFaultSession):
        def get(self, url, params=None, timeout=None):
            resp = super().get(url, params, timeout)
            if params.get("volume_num_max"):
                # Serves only the boundary row, so the ceiling never moves.
                class R:
                    status_code = 500

                    def json(self):
                        raise ValueError("no JSON")

                return R()
            return resp

    sess = _NeverPastTheCeiling()
    out = iter_markets_by_volume(100.0, session=sess)
    assert [m["id"] for m in out] == ["1", "2"]
    logged = capsys.readouterr().out
    assert "INCOMPLETE" in logged
    assert "restarts 1" in logged


def test_iter_markets_keyset_page_budget_exhausted_logs_truncated(monkeypatch, capsys):
    """`max_pages` is a truncation GUARD, not a budget. Exhausting it with
    a live cursor means the caller silently got less than it asked for --
    the Gamma-offset regression class -- so it must be loud by default."""
    monkeypatch.setattr("time.sleep", lambda _s: None)
    sess = _KeysetSession(
        {
            None: {"markets": [_mkt("1", 500.0)], "next_cursor": "C1"},
            "C1": {"markets": [_mkt("2", 400.0)], "next_cursor": "C2"},
        }
    )
    out = iter_markets_by_volume(100.0, session=sess, max_pages=1)
    assert [m["id"] for m in out] == ["1"]
    assert "TRUNCATED at 1 markets" in capsys.readouterr().out


def test_iter_markets_keyset_want_top_n_silences_only_the_intended_truncation(
    monkeypatch, capsys
):
    """streamd asks for the top page by volume on purpose, so its nightly
    TRUNCATED line is noise that trains the reader to ignore a real one.
    `want_top_n` declares that intent -- and must silence ONLY that line,
    never the INCOMPLETE path, which is a different failure."""
    monkeypatch.setattr("time.sleep", lambda _s: None)
    sess = _KeysetSession(
        {
            None: {"markets": [_mkt("1", 500.0)], "next_cursor": "C1"},
            "C1": {"markets": [_mkt("2", 400.0)], "next_cursor": "C2"},
        }
    )
    out = iter_markets_by_volume(100.0, session=sess, max_pages=1, want_top_n=True)
    assert [m["id"] for m in out] == ["1"]
    assert "TRUNCATED" not in capsys.readouterr().out


def test_want_top_n_does_not_silence_incomplete(monkeypatch, capsys):
    """`want_top_n` declares "I asked for one page", which says nothing
    about Gamma failing. A walk that dies on a persistent error must
    still report INCOMPLETE even for the top-N caller -- otherwise the
    intent flag would disarm the alarm it was carefully scoped around."""
    monkeypatch.setattr("time.sleep", lambda _s: None)

    class _AlwaysError(_KeysetSession):
        def get(self, url, params=None, timeout=None):
            self.calls.append((url, dict(params)))

            class R:
                status_code = 500

                def json(self):
                    return {"type": "validation error"}

            return R()

    sess = _AlwaysError({})
    assert iter_markets_by_volume(0.0, session=sess, max_pages=1, want_top_n=True) == []
    assert "INCOMPLETE" in capsys.readouterr().out
