"""CloudProvider abstraction — answers "where does the work run?".

Phase 2 implements the read paths every provider needs (``discover_runs`` /
``get_status``) plus shared helpers. The provisioning surface (``create`` /
``destroy`` / ``prepare_registry_pull``) and interactive ``ssh_break_glass`` are
declared here as the documented contract but land in Phase 3 — calling them now
raises ``NotImplementedError`` rather than silently doing nothing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..identity import normalize_provider
from ..models import RunRecord

# Display labels (ported from scripts/lib/ctfvm/providers.sh).
PROVIDER_LABELS = {"gcp": "GCP", "digitalocean": "DigitalOcean"}
PROVIDER_SCOPE_LABELS = {"gcp": "Project", "digitalocean": "Account"}
PROVIDER_LOCATION_LABELS = {"gcp": "Zone", "digitalocean": "Region"}


def resource_missing(stderr: str) -> bool:
    """Heuristic: does this CLI error mean the instance no longer exists?
    Ported from ``providers._resource_missing``."""
    text = str(stderr or "").strip().lower()
    if not text:
        return False
    return any(
        pat in text
        for pat in ("not found", "could not fetch resource", "was not found", "404", "does not exist")
    )


class CloudProvider(ABC):
    name: str = ""

    # --- read paths (Phase 2) ---------------------------------------------
    @abstractmethod
    def discover_runs(self) -> list[RunRecord]:
        """Query the cloud API for running ``ctfvm-*`` instances."""

    @abstractmethod
    def get_status(self, run: RunRecord) -> str:
        """Single-instance status string ('' when the instance is gone)."""

    @property
    def cli_available(self) -> bool:
        """Whether the provider CLI is installed (used by cleanup-state)."""
        return True

    # --- interactive break-glass (Phase 3) --------------------------------
    def ssh_break_glass(self, run: RunRecord, remote_cmd: str, *, tty: bool = True) -> int:
        raise NotImplementedError("break-glass SSH is ported in Phase 3")

    # --- provisioning (Phase 3) -------------------------------------------
    def create(self, spec) -> RunRecord:  # noqa: ANN001 - RunSpec lands in Phase 3
        raise NotImplementedError("create is ported in Phase 3")

    def destroy(self, run: RunRecord) -> None:
        raise NotImplementedError("destroy is ported in Phase 3")

    def prepare_registry_pull(self, run: RunRecord) -> None:
        raise NotImplementedError("registry pull is ported in Phase 3")


class CloudProviderRegistry:
    """Holds the configured providers and fans discovery/status out to them."""

    def __init__(self, providers: list[CloudProvider]):
        self._providers = {p.name: p for p in providers}

    def get(self, provider: str) -> CloudProvider:
        backend = self._providers.get(normalize_provider(provider))
        if backend is None:
            raise KeyError(f"unknown provider: {provider}")
        return backend

    def discover_all(self, force: bool = False) -> list[RunRecord]:
        # `force` is accepted for the RunRegistry callable signature; caching is
        # owned by RunRegistry, so every call here is a live query.
        discovered: list[RunRecord] = []
        for provider in self._providers.values():
            discovered.extend(provider.discover_runs())
        return discovered

    def get_status(self, run: RunRecord) -> str:
        try:
            return self.get(run.provider).get_status(run)
        except KeyError:
            return "UNKNOWN"
