"""The strategy library must accept higher-timeframe features emitted by lanes."""
import pytest

from stock_gplm.app.library import StrategyError, parse_feature


@pytest.mark.parametrize("key", [
    "4h:EMA(period=20).acceleration", "4h:RSI(period=14).slope",
    "4h:ATR(period=14).natr_change", "4h:ROC(period=12).value",
    "4h:ADX(period=14).di_spread",
])
def test_four_hour_feature_contract(key):
    assert parse_feature(key)["tf"] == "4h"


def test_unknown_timeframe_still_rejected():
    with pytest.raises(StrategyError):
        parse_feature("7h:EMA(period=20).acceleration")
