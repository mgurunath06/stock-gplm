"""Offline checks of the Yahoo daily parser and the Shoonya join (synthetic payload)."""
from stock_gplm.app.yahoo_history import link, parse

DAY = 86400
T0 = 1667187900  # 2022-10-31 03:45 UTC (09:15 IST)


def _payload(n):
    ts = [T0 + i * DAY for i in range(n)]
    c = [100.0 + i for i in range(n)]
    q = {"open": c[:], "high": [x + 1 for x in c], "low": [x - 1 for x in c], "close": c[:], "volume": [1000] * n}
    q["close"][3] = None            # missing price -> rejected
    q["high"][4] = q["low"][4] - 5  # impossible -> rejected
    q["volume"][5] = 0              # zero volume -> rejected
    return {"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [q]}}]}}


def test_parse_rejects_and_ist_dates():
    rows, rej = parse(_payload(10))
    assert rej == {"missing_price": 1, "impossible_ohlc": 1, "zero_volume": 1}
    assert len(rows) == 7 and rows[0][0] == "2022-10-31"


def test_link_scales_and_cuts():
    rows, _ = parse(_payload(30))
    shoonya = {d: c * 2 for d, o, h, lo, c, v in rows[10:]}  # Shoonya prices are 2x (e.g. other adjustment)
    keep, note = link(rows, shoonya, before=rows[10][0])
    assert note == "ok" and len(keep) == 10 and keep[-1][0] < rows[10][0] and abs(keep[0][4] - rows[0][4] * 2) < 1e-9
    bad = dict(shoonya)
    bad[rows[12][0]] *= 1.05
    assert link(rows, bad, rows[10][0])[1].startswith("Yahoo and Shoonya disagree")
