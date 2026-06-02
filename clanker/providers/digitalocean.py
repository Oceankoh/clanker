"""DigitalOcean cloud provider — discovery + status (ported from ctfvm_ui.providers)."""

from __future__ import annotations

import shutil

from ..identity import iso_started_at_from_run_id, run_id_from_instance_name
from ..models import RunRecord
from ..proc import run_cmd
from .base import CloudProvider, resource_missing


class DigitalOceanCloudProvider(CloudProvider):
    name = "digitalocean"

    @property
    def cli_available(self) -> bool:
        return shutil.which("doctl") is not None

    def discover_runs(self) -> list[RunRecord]:
        rc, out, _ = run_cmd(
            [
                "doctl", "compute", "droplet", "list",
                "--tag-name", "ctfvm",
                "--format", "Name,Region,Status,PublicIPv4",
                "--no-header",
            ],
            timeout=12,
        )
        if rc != 0:
            return []

        discovered: list[RunRecord] = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            name, region, status, ip = parts[0], parts[1], parts[2], parts[3]
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
        return discovered

    def get_status(self, run: RunRecord) -> str:
        rc, out, err = run_cmd(
            [
                "doctl", "compute", "droplet", "get", run.instance,
                "--format", "Status",
                "--no-header",
            ],
            timeout=15,
        )
        if rc == 0:
            return out.strip()
        return "" if resource_missing(err) else "UNKNOWN"
