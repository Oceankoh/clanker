"""DigitalOcean cloud provider — discovery + status (ported from ctfvm_ui.providers)."""

from __future__ import annotations

import shutil
import threading
import time

from ..identity import iso_started_at_from_run_id, run_id_from_instance_name
from ..models import RunRecord
from ..proc import run_cmd
from .base import CloudProvider

_BATCH_STATUS_TTL = 30  # seconds — droplet status changes slowly (provisioning takes minutes)


class DigitalOceanCloudProvider(CloudProvider):
    name = "digitalocean"

    def __init__(self):
        self._batch_lock = threading.Lock()
        self._batch_at: float = 0.0
        self._batch_data: dict[str, str] = {}

    @property
    def cli_available(self) -> bool:
        return shutil.which("doctl") is not None

    def _list_droplets(self) -> tuple[int, str]:
        """One ``doctl compute droplet list`` call — shared by discovery and
        status so we never run N per-instance ``doctl get`` calls."""
        rc, out, _ = run_cmd(
            [
                "doctl", "compute", "droplet", "list",
                "--tag-name", "ctfvm",
                "--format", "Name,Region,Status,PublicIPv4",
                "--no-header",
            ],
            timeout=12,
        )
        return rc, out

    def discover_runs(self) -> list[RunRecord]:
        rc, out = self._list_droplets()
        if rc != 0:
            return []

        statuses: dict[str, str] = {}
        discovered: list[RunRecord] = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            name, region, status, ip = parts[0], parts[1], parts[2], parts[3]
            statuses[name] = status
            if not name.startswith("ctfvm-") or status != "active":
                continue
            run_id = run_id_from_instance_name(name)
            discovered.append(
                RunRecord(
                    provider=self.name,
                    run_id=run_id,
                    instance=name,
                    zone=region,
                    project="digitalocean",
                    ip=ip,
                    started_at=iso_started_at_from_run_id(run_id),
                )
            )
        # Populate the batch status cache so the per-run get_status() calls
        # that follow discovery (e.g. list_runs with include_status) are free.
        with self._batch_lock:
            self._batch_at = time.monotonic()
            self._batch_data = statuses
        return discovered

    def _batch_statuses(self) -> dict[str, str] | None:
        """All ctfvm droplet statuses in one API call, with a short-TTL cache.
        Returns None only if the doctl call fails entirely."""
        now = time.monotonic()
        with self._batch_lock:
            if self._batch_data and (now - self._batch_at) < _BATCH_STATUS_TTL:
                return dict(self._batch_data)
        rc, out = self._list_droplets()
        if rc != 0:
            return None
        statuses: dict[str, str] = {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 3:
                statuses[parts[0]] = parts[2]
        with self._batch_lock:
            self._batch_at = time.monotonic()
            self._batch_data = statuses
        return statuses

    def get_status(self, run: RunRecord) -> str:
        statuses = self._batch_statuses()
        if statuses is None:
            return "UNKNOWN"
        # Instance missing from the list → droplet is gone (equivalent to "not found").
        return statuses.get(run.instance, "")
