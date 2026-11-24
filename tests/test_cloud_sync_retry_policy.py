from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path


def _load_cloud_sync():
    jobs = types.ModuleType("gplm_jobs")
    collector = types.ModuleType("live_collector")
    collector.session_api = lambda: None
    adaptive = types.ModuleType("adaptive_workers")
    adaptive.AdaptiveWorkers = object
    ledger = types.ModuleType("transfer_ledger")
    ledger.archived = lambda *_args: set()
    ledger.download_event = lambda *_args: None
    stubs = {
        "gplm_jobs": jobs,
        "live_collector": collector,
        "adaptive_workers": adaptive,
        "transfer_ledger": ledger,
    }
    old = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    try:
        path = Path(__file__).parents[1] / "deploy" / "oracle" / "live" / "cloud_sync.py"
        spec = importlib.util.spec_from_file_location("cloud_sync_retry_test", path)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        return module
    finally:
        for name, value in old.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def test_fifth_failure_becomes_unavailable_and_is_not_due(tmp_path):
    module = _load_cloud_sync()
    module.STATE = tmp_path / "state.json"
    retry = module.Retry()
    key = "ABC-EQ|minute|2026-01-19"
    for _ in range(module.MAX_ATTEMPTS):
        retry.fail(key, "provider returned no minute history")
    assert retry.s[key]["status"] == "UNAVAILABLE"
    assert retry.s[key]["attempts"] == module.MAX_ATTEMPTS
    assert not retry.due(key)
    assert retry.health("ABC-EQ")["status"] == "PARTIAL"


def test_old_gave_up_state_migrates_and_unavailable_count_excludes_downloaded(tmp_path):
    module = _load_cloud_sync()
    module.STATE = tmp_path / "state.json"
    module.STATE.write_text(json.dumps({
        "ABC-EQ|minute|2026-01-19": {
            "status": "GAVE_UP", "attempts": 5, "last_error": "no data",
        },
        "ABC-EQ|minute|2026-01-20": {
            "status": "UNAVAILABLE", "attempts": 5, "last_error": "no data",
        },
    }), encoding="utf-8")
    retry = module.Retry()
    assert retry.s["ABC-EQ|minute|2026-01-19"]["status"] == "UNAVAILABLE"
    days = ["2026-01-19", "2026-01-20", "2026-01-21"]
    assert retry.unavailable_count("ABC-EQ", days) == 2
    assert retry.unavailable_count("ABC-EQ", days, have={"2026-01-20"}) == 1


def test_retrying_and_unavailable_are_reported_separately(tmp_path):
    module = _load_cloud_sync()
    module.STATE = tmp_path / "state.json"
    retry = module.Retry()
    retry.mark_unavailable("ABC-EQ|minute|2026-01-19", "no data")
    retry.fail("ABC-EQ|minute|2026-01-20", "temporary error")
    health = retry.health("ABC-EQ")
    assert health["status"] == "RETRYING"
    assert "1 confirmed unavailable" in health["detail"]
