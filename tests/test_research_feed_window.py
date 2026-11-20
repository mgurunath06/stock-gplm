from datetime import date
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "research_feed", Path(__file__).parents[1] / "var/runs/research_feed.py")
feed = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(feed)


def test_accepts_cloud_provider_window(monkeypatch):
    monkeypatch.setattr(feed, "date", type("D", (), {"today": staticmethod(lambda: date(2026, 11, 20)),
                                                       "fromisoformat": date.fromisoformat}))
    raw = json.dumps({"window": ["2026-03-20", "2026-11-19"], "expected": 167})
    assert feed.parse_provider_window(raw) == ("2026-03-20", "2026-11-19", 167)


@pytest.mark.parametrize("payload", [
    {}, {"window": ["2026-03-20"], "expected": 167},
    {"window": ["2026-11-19", "2026-03-20"], "expected": 167},
    {"window": ["2026-03-20", "2026-11-19"], "expected": 0},
])
def test_rejects_invalid_cloud_provider_window(payload):
    with pytest.raises((ValueError, TypeError)):
        feed.parse_provider_window(json.dumps(payload))


def test_feeder_universe_excludes_etfs():
    watch, universe = feed.symbols()
    assert "SILVERBEES" not in universe
    assert "LIQUIDBEES" not in universe
    assert "JETFREIGHT" in universe  # explicit company exclusion, despite containing ETF
    assert "HDFCBANK" in watch
