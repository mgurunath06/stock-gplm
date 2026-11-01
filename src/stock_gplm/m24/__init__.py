"""M2.4 secure, resumable historical-publication synchronization."""

from .models import CollectorWindow, SyncLimits, SyncRequestV1
from .sync import (
    build_plan,
    discover_catalog,
    publish_verified_triplet,
    pull_plan,
    reconcile_run,
    validate_run,
)
from .transport import LocalFixtureTransport, WindowsOpenSSHTransport

__all__ = [
    "CollectorWindow", "LocalFixtureTransport", "SyncLimits", "SyncRequestV1",
    "WindowsOpenSSHTransport", "build_plan", "discover_catalog",
    "publish_verified_triplet", "pull_plan", "reconcile_run", "validate_run",
]
