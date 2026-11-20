"""Official NSE index history: cloud fetcher format + PC symbol resolution (owner 2026-11-20: all indices)."""
from __future__ import annotations

import gzip
import importlib.util
import json
import sys
import types
from datetime import date
from pathlib import Path

from stock_gplm.app import strategy_dsl as dsl

ROOT = Path(__file__).resolve().parents[1]


def _cloud(tmp_path):
    sys.modules.setdefault("nse_mcp_client", types.SimpleNamespace(NSEMCPClient=None))
    spec = importlib.util.spec_from_file_location("index_history", ROOT / "deploy/oracle/live/index_history.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.OUT = tmp_path
    return m


def test_symbol_rule_matches_cloud_and_resolves_names(tmp_path):
    m = _cloud(tmp_path)
    for n in ("Nifty 50", "Nifty Bank", "NIFTY Smallcap 100", "India VIX",
              "Nifty India Corporate Group Index - Tata Group 25% Cap"):
        assert m.index_symbol(n) == dsl.index_symbol(n)
        assert len(m.index_symbol(n)) <= 40
    assert dsl.resolve_symbol("Nifty 50") == "IDX_NIFTY_50-EQ"
    assert dsl.resolve_symbol("NIFTY") == "IDX_NIFTY_50-EQ"
    assert dsl.resolve_symbol("banknifty") == "IDX_NIFTY_BANK-EQ"
    assert dsl.resolve_symbol("IDX_NIFTY_IT") == "IDX_NIFTY_IT-EQ"
    assert dsl.resolve_symbol("NIFTYBEES") == "NIFTYBEES-EQ"  # ETF stays a stock
    assert dsl.resolve_symbol("INFY") == "INFY-EQ"
    assert dsl.Context.default_benchmark == "IDX_NIFTY_50-EQ"


def test_ref_symbols_resolve_index_names():
    from stock_gplm.app.research import _ref_symbols
    s = types.SimpleNamespace(blocked=None, exit_node=None,
                              entry_node=dsl.parse('bench(close, "Nifty Bank") > bench(close) and close > 0'))
    assert _ref_symbols([s], None) == {"IDX_NIFTY_BANK-EQ", "IDX_NIFTY_50-EQ"}


def test_publish_shoonya_format_years_then_chunks(tmp_path):
    m = _cloud(tmp_path)
    rec = [{"date": "19-Nov-2026", "open": "26,000.5", "high": "26100", "low": "25900", "close": "26050", "volume": "0"},
           {"Date": "2025-12-31", "Close": "24000"}]
    rows = sorted(x for x in map(m.parse, rec) if x)
    assert rows[0] == (date(2025, 12, 31), 24000.0, 24000.0, 24000.0, 24000.0, 0.0)
    assert m.publish_all("IDX_NIFTY_50-EQ", "Nifty 50", rows, date(2026, 11, 20)) == 2
    d = tmp_path / "IDX_NIFTY_50-EQ"
    assert sorted(p.name for p in d.glob("*.done")) == ["2025-01-01_2025-12-31.json.gz.done",
                                                       "2026-01-01_2026-11-19.json.gz.done"]
    row = json.loads(json.loads(gzip.decompress((d / "2026-01-01_2026-11-19.json.gz").read_bytes()))[0])
    assert row == {"time": "19-NOV-2026", "into": "26000.50", "inth": "26100.00", "intl": "25900.00",
                   "intc": "26050.00", "ssboe": "1795046400", "intv": "0.00"}  # same ssboe as Shoonya
    # next day: only a non-overlapping tail chunk; published files are never rewritten
    rows.append((date(2026, 11, 20), 1, 1, 1, 1, 0))
    assert m.publish_all("IDX_NIFTY_50-EQ", "Nifty 50", rows, date(2026, 11, 21)) == 1
    assert (d / "2026-11-20_2026-11-20.json.gz.done").exists()
    assert m.publish_all("IDX_NIFTY_50-EQ", "Nifty 50", rows, date(2026, 11, 21)) == 0
