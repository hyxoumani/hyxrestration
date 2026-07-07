"""Backfill parsing: IEM MOS CSV conventions, Kalshi candles, Tier-1 snapshots."""

import io
from datetime import date, datetime, timezone
from unittest.mock import patch

from hyxlab.store import Store
from hyxlab.venues.iem import get_mos_forecasts
from hyxlab.venues.kalshi import candle_row

MOS_CSV = """runtime,ftime,model,n_x,tmp,dpt,cld,wsp,p12,q12,t12_1,t12_2,station,q24,p24,t24,t12
2025-07-01 00:00:00,2025-07-02 00:00:00,MEX,86,77,72,OV,4,87,4.0,23,52,KNYC,,,,23/52
2025-07-01 00:00:00,2025-07-02 12:00:00,MEX,71,73,70,OV,2,87,3.0,52,25,KNYC,,,53.0,52/25
2025-07-01 00:00:00,2025-07-03 00:00:00,MEX,85,79,65,PC,2,32,0.0,25,6,KNYC,4.0,87.0,,25/6
2025-07-01 00:00:00,2025-07-03 12:00:00,MEX,,74,63,CL,2,5,0.0,6,3,KNYC,,,10.0,6/3
"""


class _FakeResp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


def test_mos_parsing_keeps_00z_maxes_only():
    with patch("requests.Session.get", return_value=_FakeResp(MOS_CSV)):
        fcs = get_mos_forecasts("NYC", date(2025, 7, 1), date(2025, 7, 4))
    # 12Z ftimes (overnight mins) and empty n_x rows are dropped.
    assert len(fcs) == 2
    # n_x at ftime 00Z July 2 = daytime max for July 1.
    assert fcs[0].target_date == date(2025, 7, 1)
    assert fcs[0].high_f == 86
    assert fcs[0].fetched_at == datetime(2025, 7, 1, 0, 0, tzinfo=timezone.utc)
    assert fcs[1].target_date == date(2025, 7, 2)
    assert fcs[1].high_f == 85


def test_candle_row_flattens_api_shape():
    c = {
        "end_period_ts": 1783177200,
        "open_interest_fp": "1227.32",
        "volume_fp": "1238.32",
        "price": {
            "open_dollars": "0.0800",
            "high_dollars": "0.0800",
            "low_dollars": "0.0200",
            "close_dollars": "0.0200",
        },
        "yes_bid": {"close_dollars": "0.0100", "high_dollars": "0.0900"},
        "yes_ask": {"close_dollars": "0.0200", "low_dollars": "0.0200"},
    }
    row = candle_row("KXHIGHNY", {"ticker": "KXHIGHNY-26JUL05-T91"}, c, 3600)
    assert row[0] == "kalshi"
    assert row[1] == "KXHIGHNY-26JUL05-T91"
    assert row[2] == datetime.fromtimestamp(1783177200, tz=timezone.utc)
    assert row[7] == 0.02  # price_close
    assert row[8] == 0.01  # yes_bid_close
    assert row[9] == 0.02  # yes_ask_close
    assert row[12] == 1238.32  # volume


def test_candles_as_snapshots_complement_and_order(tmp_path):
    store = Store(tmp_path / "t.duckdb")
    ts1 = datetime(2026, 7, 1, 12, tzinfo=timezone.utc)
    ts0 = datetime(2026, 7, 1, 11, tzinfo=timezone.utc)
    store.insert_candles(
        [
            ("kalshi", "M1", ts1, 3600, None, None, None, 0.30, 0.29, 0.31, None, None, 10.0, 5.0),
            ("kalshi", "M1", ts0, 3600, None, None, None, 0.20, 0.19, 0.21, None, None, 10.0, 5.0),
        ]
    )
    snaps = store.candles_as_snapshots()
    # Stored as naive UTC (see store._naive_utc), returned in replay order.
    assert [s.ts for s in snaps] == [ts0.replace(tzinfo=None), ts1.replace(tzinfo=None)]
    s = snaps[1]
    assert s.yes_bid == 0.29 and s.yes_ask == 0.31
    assert s.no_bid == 1.0 - 0.31 and s.no_ask == 1.0 - 0.29  # binary complement
    assert s.yes_ask_size == float("inf")  # unknown depth -> optimistic Tier-1
    store.close()
