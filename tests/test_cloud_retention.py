import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("transfer_ledger", Path(__file__).parents[1] / "deploy/oracle/live/transfer_ledger.py")
ledger = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ledger)


@pytest.fixture
def staged(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "ROOT", tmp_path / "data")
    monkeypatch.setattr(ledger, "DB", tmp_path / "ledger.sqlite")
    rel = "minute/shoonya/NSE/TEST-EQ/2026-01-19.json.gz"
    pp = ledger.paths(rel)
    pp[0].parent.mkdir(parents=True)
    pp[0].write_bytes(b"synthetic payload")
    pp[1].write_text("{}")
    pp[2].write_text(json.dumps({"status": "COMPLETE", **ledger.digest(pp[0])}))
    return rel, pp


def test_delete_only_after_matching_receipt_and_preserve_coverage(staged):
    rel, pp = staged
    item = ledger.listing()[0]
    ledger.acknowledge({**item, "pc_verified_at": "synthetic", "pc_paths": ["synthetic"]})
    assert not any(p.exists() for p in pp)
    assert ledger.archived("minute", "TEST-EQ") == {"2026-01-19"}
    ledger.acknowledge(item)  # safe retry after lost response
    with ledger.connect() as c:
        assert [x[0] for x in c.execute("SELECT event FROM events")] == [
            "CLOUD_PUBLICATION_VERIFIED", "PC_VERIFIED", "DELETE_INTENT", "CLOUD_DELETED"]


def test_bad_pc_receipt_keeps_cloud_files(staged):
    _, pp = staged
    item = ledger.listing()[0]
    item["manifest"][0]["sha256"] = "bad"
    with pytest.raises(ValueError):
        ledger.acknowledge(item)
    assert all(p.exists() for p in pp)


def test_changed_cloud_component_keeps_files(staged):
    _, pp = staged
    item = ledger.listing()[0]
    pp[1].write_text('{"changed":true}')
    with pytest.raises(ValueError):
        ledger.acknowledge(item)
    assert all(p.exists() for p in pp)


def test_partial_delete_is_resumable(staged, monkeypatch):
    _, pp = staged
    item = ledger.listing()[0]
    original = Path.unlink
    def fail(p, *a, **kw):
        if p == pp[0]:
            raise OSError("synthetic crash")
        return original(p, *a, **kw)
    with monkeypatch.context() as m:
        m.setattr(Path, "unlink", fail)
        with pytest.raises(OSError):
            ledger.acknowledge(item)
    assert ledger.listing()[0] == item
    ledger.acknowledge(item)
    assert not any(p.exists() for p in pp)


def test_path_escape_rejected():
    with pytest.raises(ValueError):
        ledger.paths("minute/shoonya/NSE/../../outside.json.gz")


def test_invalid_publication_never_listed(staged):
    _, pp = staged
    pp[0].write_bytes(b"corrupt")
    assert ledger.listing() == []
    assert all(p.exists() for p in pp)


def test_pc_requires_all_three_matching_files(staged):
    from stock_gplm.app.cloud_link import CloudSync
    from stock_gplm.app.cloud_retention import local_receipt
    _, pp = staged
    item = ledger.listing()[0]
    sync = CloudSync([], ledger.ROOT / "daily/shoonya/NSE", [ledger.ROOT / "minute/shoonya/NSE"], ledger.ROOT)
    assert local_receipt(sync, item) is not None
    pp[1].unlink()
    assert local_receipt(sync, item) is None


def test_pc_conflict_is_not_acknowledged(staged, monkeypatch, tmp_path):
    from stock_gplm.app import cloud_retention as pc
    from stock_gplm.app.cloud_link import CloudSync
    _, pp = staged
    item = ledger.listing()[0]
    pp[0].write_bytes(b"different")
    calls = []
    def rpc(op, **kw):
        calls.append(op)
        return [item] if op == "list" else {"ok": True}
    monkeypatch.setattr(pc, "rpc", rpc)
    sync = CloudSync([], ledger.ROOT / "daily/shoonya/NSE", [ledger.ROOT / "minute/shoonya/NSE"], ledger.ROOT)
    monkeypatch.setattr(sync, "pull", lambda files: [])
    assert pc.cycle(sync, tmp_path / "audit.jsonl") == 0
    assert "ack" not in calls
