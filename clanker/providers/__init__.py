"""Cloud provider layer + factories that wire it into ``RunRegistry``."""

from __future__ import annotations

from ..state import RunRegistry
from .base import (
    PROVIDER_LABELS,
    PROVIDER_LOCATION_LABELS,
    PROVIDER_SCOPE_LABELS,
    CloudProvider,
    CloudProviderRegistry,
)
from .digitalocean import DigitalOceanCloudProvider
from .gcp import GcpCloudProvider

__all__ = [
    "CloudProvider",
    "CloudProviderRegistry",
    "GcpCloudProvider",
    "DigitalOceanCloudProvider",
    "build_provider_registry",
    "build_run_registry",
    "PROVIDER_LABELS",
    "PROVIDER_SCOPE_LABELS",
    "PROVIDER_LOCATION_LABELS",
]


def build_provider_registry() -> CloudProviderRegistry:
    return CloudProviderRegistry([GcpCloudProvider(), DigitalOceanCloudProvider()])


def build_run_registry(provider_registry: CloudProviderRegistry | None = None) -> RunRegistry:
    """A ``RunRegistry`` wired with live cloud discovery + status."""
    providers = provider_registry or build_provider_registry()
    return RunRegistry(
        discover=providers.discover_all,
        status=providers.get_status,
    )
