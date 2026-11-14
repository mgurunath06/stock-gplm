"""M5.1.3 partition-migration specification tests (synthetic plans; nothing is migrated)."""
from __future__ import annotations

import copy
import inspect

import pytest
from m5_fixtures import FIXTURE_GOVERNED_HOLDOUT

from stock_gplm.m5 import migration_spec as spec
from stock_gplm.m5.authority import GovernedHoldout

GOVERNED = GovernedHoldout(**FIXTURE_GOVERNED_HOLDOUT)  # cutoff 2024-10-01


def _source(symbol: str, start: str, end: str, seed: str = "a") -> dict:
    return {"symbol": symbol, "file": f"{start}_{end}.json.gz", "window": [start, end],
            "payload_sha256": seed * 64, "done_sha256": "b" * 64, "meta_sha256": "c" * 64}


SOURCES = [_source("BETA-EQ", "2024-01-01", "2024-12-31", "d"), _source("ALPHA-EQ", "2023-01-01", "2023-12-31"),
           _source("ALPHA-EQ", "2024-01-01", "2024-12-31", "e"), _source("ALPHA-EQ", "2025-01-01", "2025-12-31", "f")]


def _plan(sources=SOURCES, **changes) -> dict:
    plan = {
        "contract": spec.MIGRATION_PLAN_CONTRACT, "governed_holdout": FIXTURE_GOVERNED_HOLDOUT,
        "source_root": {"path": "/data/raw/shoonya/NSE", "inventory_sha256": spec.inventory_sha256(sources)},
        "target_root": {"path": "/data/raw/shoonya/NSE-v2-cutoff-2024-10-01"},
        "sources": sources, "targets": spec.plan_partitions(sources, GOVERNED.cutoff),
        "publication": {"method": spec.PUBLICATION_METHOD, "writes_source_root": False},
        "validation": list(spec.REQUIRED_VALIDATION),
        "rollback": {"method": spec.ROLLBACK_METHOD, "source_root_retained": True},
        "protected_decoding": {"method": "RECOLLECT_CUTOFF_ALIGNED_WINDOWS", "decodes_protected_rows": False},
    }
    plan.update(changes)
    plan["plan_id"] = spec.plan_identity(plan)
    return plan


def test_partitioning_is_deterministic_and_order_independent() -> None:
    forward = spec.plan_partitions(SOURCES, "2024-10-01")
    assert forward == spec.plan_partitions(list(reversed(SOURCES)), "2024-10-01")
    windows = [(t["symbol"], t["window"], t["side"], t["derivation"]) for t in forward]
    assert windows == [
        ("ALPHA-EQ", ["2023-01-01", "2023-12-31"], "DEVELOPMENT", "BYTE_COPY"),
        ("ALPHA-EQ", ["2024-01-01", "2024-09-30"], "DEVELOPMENT", "SPLIT_AT_CUTOFF"),
        ("ALPHA-EQ", ["2024-10-01", "2024-12-31"], "PROTECTED", "SPLIT_AT_CUTOFF"),
        ("ALPHA-EQ", ["2025-01-01", "2025-12-31"], "PROTECTED", "BYTE_COPY"),
        ("BETA-EQ", ["2024-01-01", "2024-09-30"], "DEVELOPMENT", "SPLIT_AT_CUTOFF"),
        ("BETA-EQ", ["2024-10-01", "2024-12-31"], "PROTECTED", "SPLIT_AT_CUTOFF"),
    ]
    assert all(t["lineage"][0]["payload_sha256"] for t in forward)  # every target names its source bytes


@pytest.mark.parametrize("window, expected", [
    (("2024-07-01", "2024-09-30"), [(["2024-07-01", "2024-09-30"], "DEVELOPMENT")]),
    (("2024-07-01", "2024-10-01"), [(["2024-07-01", "2024-09-30"], "DEVELOPMENT"),
                                    (["2024-10-01", "2024-10-01"], "PROTECTED")]),
    (("2024-10-01", "2024-12-31"), [(["2024-10-01", "2024-12-31"], "PROTECTED")]),
    (("2024-09-30", "2024-09-30"), [(["2024-09-30", "2024-09-30"], "DEVELOPMENT")]),
])
def test_exact_cutoff_day_partitioning(window, expected) -> None:
    targets = spec.plan_partitions([_source("ALPHA-EQ", *window)], "2024-10-01")
    assert [(t["window"], t["side"]) for t in targets] == expected
    assert all(t["window"][1] < "2024-10-01" for t in targets if t["side"] == "DEVELOPMENT")
    assert all(t["window"][0] >= "2024-10-01" for t in targets if t["side"] == "PROTECTED")


def test_valid_recollection_and_authorized_split_plans_pass() -> None:
    spec.validate_migration_plan(_plan(), GOVERNED)
    split = _plan(protected_decoding={"method": "PUBLISHER_SPLIT", "decodes_protected_rows": True, "authorization": {
        "authorized_by": "named-data-owner", "authorization_record_sha256": "9" * 64,
        "authorized_at_utc": "2026-11-14T00:00:00Z",
        "scope": "SPLIT_ONLY_NO_RETENTION_OR_ANALYSIS_OF_DECODED_PROTECTED_ROWS"}})
    spec.validate_migration_plan(split, GOVERNED)


def _broken(mutate) -> dict:
    plan = copy.deepcopy(_plan())
    mutate(plan)
    plan["plan_id"] = spec.plan_identity(plan)
    return plan


@pytest.mark.parametrize("mutate, message", [
    (lambda p: p.update(governed_holdout=FIXTURE_GOVERNED_HOLDOUT | {"cutoff": "2099-01-01"}), "governed_holdout"),
    (lambda p: p["targets"][1]["window"].__setitem__(1, "2024-10-01"), "deterministic partition"),
    (lambda p: p["targets"].pop(), "deterministic partition"),
    (lambda p: p["sources"][0].update(payload_sha256="Z" * 64), "lowercase SHA-256"),
    (lambda p: p["sources"][0].update(file="renamed.json.gz"), "matching its file name"),
    (lambda p: p["source_root"].update(inventory_sha256="0" * 64), "inventory_sha256"),
    (lambda p: p["target_root"].update(path="/data/raw/shoonya/NSE"), "new version outside the source root"),
    (lambda p: p["target_root"].update(path="/data/raw/shoonya/NSE/v2"), "new version outside the source root"),
    (lambda p: p["target_root"].update(path="/data/raw"), "new version outside the source root"),
    (lambda p: p["target_root"].update(path="relative/v2"), "absolute"),
    (lambda p: p.update(publication={"method": "IN_PLACE_REWRITE", "writes_source_root": True}), "publication"),
    (lambda p: p["validation"].pop(), "validation"),
    (lambda p: p.update(rollback={"method": "RESTORE_FROM_BACKUP", "source_root_retained": False}), "rollback"),
    (lambda p: p.update(protected_decoding={"method": "PUBLISHER_SPLIT", "decodes_protected_rows": True}),
     "explicit hash-bound authorization"),
    (lambda p: p.update(protected_decoding={"method": "RECOLLECT_CUTOFF_ALIGNED_WINDOWS",
                                            "decodes_protected_rows": True}), "decodes_protected_rows false"),
    (lambda p: p.update(protected_decoding={"method": "M5_DECODES_AND_SPLITS"}), "protected_decoding.method"),
])
def test_invalid_plans_fail_closed(mutate, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        spec.validate_migration_plan(_broken(mutate), GOVERNED)


def test_tampered_plan_identity_is_rejected() -> None:
    plan = _plan()
    plan["target_root"] = {"path": "/data/raw/shoonya/NSE-other"}
    with pytest.raises(ValueError, match="plan_id"):
        spec.validate_migration_plan(plan, GOVERNED)


def test_specification_module_performs_no_io_or_decoding() -> None:
    source = inspect.getsource(spec)
    for forbidden in ("open(", "read_bytes", "read_text", "write_", "gzip", "json.load", "os.", "shutil",
                      "subprocess", "urlopen", "rename("):
        assert forbidden not in source.split('"""', 2)[2]  # code only, after the module docstring
