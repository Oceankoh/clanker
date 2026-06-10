"""GCP cloud provider — discovery + status (ported from ctfvm_ui.providers)."""

from __future__ import annotations

import json
import shutil

from ..identity import iso_started_at_from_run_id, run_id_from_instance_name
from ..models import RunRecord
from ..proc import run_cmd
from .base import CloudProvider, resource_missing


def project_from_gcloud_config() -> str:
    rc, out, _ = run_cmd(["gcloud", "config", "get-value", "project"], timeout=5)
    if rc != 0:
        return ""
    value = out.strip()
    return "" if value in {"", "(unset)"} else value


class GcpCloudProvider(CloudProvider):
    name = "gcp"

    @property
    def cli_available(self) -> bool:
        return shutil.which("gcloud") is not None

    def discover_runs(self) -> list[RunRecord]:
        project = project_from_gcloud_config()
        if not project:
            return []

        rc, out, _ = run_cmd(
            [
                "gcloud", "compute", "instances", "list",
                "--project", project,
                "--filter=name~^ctfvm- AND status=RUNNING",
                "--format=json(name,zone,networkInterfaces[0].accessConfigs[0].natIP)",
            ],
            timeout=12,
        )
        if rc != 0:
            return []
        try:
            rows = json.loads(out or "[]")
        except Exception:
            rows = []

        discovered: list[RunRecord] = []
        for row in rows:
            name = str((row or {}).get("name", "")).strip()
            if not name:
                continue
            zone_raw = str((row or {}).get("zone", "")).strip()
            zone = zone_raw.rsplit("/", 1)[-1] if "/" in zone_raw else zone_raw
            run_id = run_id_from_instance_name(name)
            ip = (
                ((row or {}).get("networkInterfaces") or [{}])[0]
                .get("accessConfigs", [{}])[0]
                .get("natIP", "")
            )
            discovered.append(
                RunRecord(
                    provider=self.name,
                    run_id=run_id,
                    instance=name,
                    zone=zone,
                    project=project,
                    ip=ip,
                    started_at=iso_started_at_from_run_id(run_id),
                )
            )
        return discovered

    def get_status(self, run: RunRecord) -> str:
        rc, out, err = run_cmd(
            [
                "gcloud", "compute", "instances", "describe", run.instance,
                "--zone", run.zone,
                "--project", run.project,
                "--format=get(status)",
            ],
            timeout=15,
        )
        if rc == 0:
            return out.strip()
        return "" if resource_missing(err) else "UNKNOWN"
