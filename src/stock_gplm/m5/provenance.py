"""Run provenance: portable content identity separated from environmental facts (M5.1).

``content_identity`` goes into every M5 ``run_id``. It hashes the bytes of every
source file on the execution path, grouped by component, keyed by package-relative
POSIX paths. It holds no absolute paths, hostnames, timestamps or git state, so
the same code gives the same identity on any machine or checkout.

``environment_provenance`` is recorded next to the run but kept out of its
``run_id``: git commit and dirty flag, the full Python build, platform, lockfile
hashes and the versions of any non-standard-library modules loaded. These facts
describe where a run happened and do not change its analytical content. The
execution path imports only the standard library (a test enforces this), so
dependency lockfiles are environmental.
"""
from __future__ import annotations

import hashlib
import platform
import subprocess
import sys
from importlib import metadata
from pathlib import Path
from typing import Any

from .models import ADAPTER_VERSION, canonical_hash

PROVENANCE_CONTRACT = "stock-gplm.m511-run-provenance.v2"
ENVIRONMENT_CONTRACT = "stock-gplm.m511-runtime-environment.v1"
PACKAGE = Path(__file__).resolve().parents[1]
# Every subpackage whose code can change which bars are released or how they are simulated.
COMPONENTS: dict[str, tuple[str, ...]] = {
    "m1_publication_validation": ("ingestion",),
    "m2_reference_importers": ("m2",),
    "m39b_corporate_actions": ("m39b",),
    "v1_strategy_execution": ("v1",),
    "m5_adapter_runner_eligibility": ("m5",),
}
LOCKFILES = ("pyproject.toml", "requirements-dev.lock", "requirements-m35.lock")


def _component_hash(directories: tuple[str, ...]) -> dict[str, Any]:
    files: dict[str, str] = {}
    for name in directories:
        for path in sorted(PACKAGE.joinpath(name).rglob("*.py")):
            if "__pycache__" not in path.parts:
                files[path.relative_to(PACKAGE).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"files": len(files), "sha256": canonical_hash(files)}


def content_identity() -> dict[str, Any]:
    """Deterministic, machine-independent identity of the executing code."""
    components = {name: _component_hash(dirs) for name, dirs in COMPONENTS.items()}
    components["package_init"] = {"files": 1, "sha256": hashlib.sha256(
        PACKAGE.joinpath("__init__.py").read_bytes()).hexdigest()}
    runtime = {"implementation": sys.implementation.name,
               "version": f"{sys.version_info.major}.{sys.version_info.minor}"}
    value = {"contract": PROVENANCE_CONTRACT, "adapter_version": ADAPTER_VERSION, "components": components,
             "python": runtime}
    return value | {"source_sha256": canonical_hash(value)}


def _third_party_versions() -> dict[str, str]:
    names = sorted({name.split(".")[0] for name in list(sys.modules)} - set(sys.stdlib_module_names)
                   - {"stock_gplm", "__main__", "_distutils_hack"})
    versions: dict[str, str] = {}
    for name in names:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
    return versions


def environment_provenance() -> dict[str, Any]:
    """Where and with what this process ran; recorded, never part of ``run_id``."""
    root = PACKAGE.parents[1]
    missing = [name for name in LOCKFILES if not (root / name).is_file()]
    if missing:
        raise FileNotFoundError(f"required reproducibility manifests are missing: {missing}")
    lockfiles = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in LOCKFILES}
    commit = dirty = None
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=PACKAGE, capture_output=True, text=True,
                                check=True, timeout=10).stdout.strip() or None
        status = subprocess.run(["git", "status", "--porcelain", "--", "."], cwd=PACKAGE, capture_output=True,
                                text=True, check=True, timeout=10).stdout
        dirty = bool(status.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return {"contract": ENVIRONMENT_CONTRACT, "required_reproducibility_manifests": list(LOCKFILES),
            "reproducibility_manifest_status": "COMPLETE",
            "git_commit": commit, "git_worktree_dirty": dirty, "python_version": platform.python_version(),
            "python_build": list(platform.python_build()), "platform": f"{platform.system()}-{platform.machine()}",
            "lockfile_sha256": lockfiles, "loaded_third_party_versions": _third_party_versions()}
