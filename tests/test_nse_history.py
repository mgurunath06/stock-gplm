"""Synthetic check of nse_history parsing + split adjustment + Shoonya linking (no network)."""
from stock_gplm.app.nse_history import adjust_and_link, parse_rows


def test_parse_adjust_link() -> None:
    # 2:1 split on 2022-10-28 (NSE prev-close halves); Shoonya (already adjusted) starts 2022-10-31
    payload = {"data": [
        {"symbol": "ABC", "series": "EQ", "date": "2022-10-26", "open": 200, "high": 210, "low": 190, "close": 200,
         "prevClose": 198, "volume": 1000},
        {"symbol": "ABC", "series": "EQ", "date": "2022-10-27", "open": 200, "high": 204, "low": 198, "close": 202,
         "prevClose": 200, "volume": 1000},
        {"symbol": "ABC", "series": "BE", "date": "2022-10-27", "open": 1, "high": 1, "low": 1, "close": 1,
         "volume": 1},
        {"symbol": "ABC", "series": "EQ", "date": "28-Oct-2022", "open": 101, "high": 103, "low": 100, "close": 102,
         "prevClose": 101, "volume": 2000},
    ] + [{"symbol": "ABC", "series": "EQ", "date": f"2022-11-0{i}", "open": 102, "high": 104, "low": 100,
          "close": 103, "prevClose": 102 if i == 1 else 103, "volume": 2000} for i in range(1, 7)]}
    rows = parse_rows(payload, "ABC")
    assert len(rows) == 9 and rows[0][0] == "2022-10-26"  # BE row ignored, dates normalised
    shoonya = {f"2022-11-0{i}": 103.0 for i in range(1, 7)} | {"2022-10-31": 103.0}
    keep, note = adjust_and_link(rows, shoonya)
    assert note == "ok"
    assert [k[0] for k in keep] == ["2022-10-26", "2022-10-27", "2022-10-28"]  # only before Shoonya's start
    assert abs(keep[0][4] - 100.0) < 1e-9 and abs(keep[0][5] - 2000) < 1e-9  # halved price, doubled volume
    # disagreement with Shoonya is refused, not forced
    assert adjust_and_link(rows, {k: 150.0 + i for i, k in enumerate(shoonya)})[1].startswith("NSE and Shoonya")
