"""Polymarket archival: flatteners pinned to probed API shapes, store
round-trips, YES-normalization of trades."""

from datetime import UTC, datetime

from hyxlab.store import Store
from hyxlab.venues.polymarket import (
    gamma_market_info,
    poly_trade_row,
    price_rows,
    token_pair,
)

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
