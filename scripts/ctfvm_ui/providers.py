#!/usr/bin/env python3
import json
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ctfvm_control_client import (
    control_credentials_from_state,
    control_endpoint_from_state,
    control_plane_exec,
)
from .common import DISCOVERY_TTL_SECONDS, STATE_DIR, run_cmd


def normalize_provider(provider):
    provider = str(provider or "").strip().lower()
    if provider in {"", "gcp", "google", "google-cloud", "googlecloud"}:
        return "gcp"
    if provider in {"do", "digitalocean", "digital-ocean"}:
        return "digitalocean"
    return provider


def provider_from_state(state):
    provider = str((state or {}).get("provider", "") or "").strip().lower()
    return normalize_provider(provider)


def normalize_instance_name(name):
    value = str(name or "").strip()
    if not value:
        return ""
    value = value.lstrip("/").strip()
    if "/" in value:
        value = value.split("/")[-1].strip()
    return value


def run_id_from_instance_name(name):
    instance = normalize_instance_name(name)
    match = re.match(r"^ctfvm-(?:[A-Za-z0-9-]+-)?(\d{8}-\d{6})$", instance)
    if match:
        return match.group(1)
    match = re.search(r"(\d{8}-\d{6})$", instance)
    if match and instance.startswith("ctfvm-"):
        return match.group(1)
    return instance


def normalize_run_id(run_id, instance):
    rid = str(run_id or "").strip()
    if re.match(r"^\d{8}-\d{6}$", rid):
        return rid
    inferred = run_id_from_instance_name(instance)
    if re.match(r"^\d{8}-\d{6}$", inferred):
        return inferred
    return rid


def iso_started_at_from_run_id(run_id):
    try:
        dt = datetime.strptime(run_id, "%Y%m%d-%H%M%S")
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return ""


def project_from_gcloud_config():
    rc, out, _ = run_cmd(["gcloud", "config", "get-value", "project"], timeout=5)
    if rc != 0:
        return ""
    value = out.strip()
    if value in {"", "(unset)"}:
        return ""
    return value


def _resource_missing(stderr: str) -> bool:
    text = str(stderr or "").strip().lower()
    if not text:
        return False
    patterns = (
        "not found",
        "could not fetch resource",
        "was not found",
        "404",
        "does not exist",
    )
    return any(pattern in text for pattern in patterns)


def _control_plane_enabled(state: dict[str, Any]) -> bool:
    endpoint = control_endpoint_from_state(state)
    user, password = control_credentials_from_state(state)
    return bool(endpoint and user and password)


def _control_plane_exec_text(state: dict[str, Any], remote_cmd: str, timeout: int = 20):
    endpoint = control_endpoint_from_state(state)
    user, password = control_credentials_from_state(state)
    rc, stdout, stderr = control_plane_exec(
        endpoint,
        user,
        password,
        remote_cmd,
        timeout=timeout,
    )
    return (
        rc,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


def _do_ssh_host_key_alias(state: dict[str, Any]) -> str:
    instance = normalize_instance_name(state.get("instance", ""))
    ip = str(state.get("ip", "") or "").strip()
    if instance:
        return f"ctfvm-do-{instance}"
    return f"ctfvm-do-{re.sub(r'[^A-Za-z0-9_.-]+', '-', ip)}"


def _do_known_hosts_file() -> Path:
    path = STATE_DIR / "ssh_known_hosts"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    try:
        path.chmod(0o600)
    except Exception:
        pass
    return path


def _do_ssh_common_args(state: dict[str, Any]) -> list[str]:
    return [
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"UserKnownHostsFile={_do_known_hosts_file()}",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        f"HostKeyAlias={_do_ssh_host_key_alias(state)}",
    ]


@dataclass(frozen=True)
class RunRecord:
    provider: str
    run_id: str
    instance: str
    zone: str
    project: str
    ip: str = ""
    started_at: str = ""
    remote_run_dir: str = "/home/ctf/run"

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "RunRecord":
        instance = normalize_instance_name(data.get("instance", ""))
        run_id = normalize_run_id(data.get("run_id", ""), instance)
        return cls(
            provider=normalize_provider(data.get("provider", "gcp")),
            run_id=run_id,
            instance=instance,
            zone=str(data.get("zone", "") or "").strip(),
            project=str(data.get("project", "") or "").strip(),
            ip=str(data.get("ip", "") or "").strip(),
            started_at=str(data.get("started_at", "") or "").strip(),
            remote_run_dir=str(data.get("remote_run_dir", "/home/ctf/run") or "/home/ctf/run"),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "run_id": self.run_id,
            "instance": self.instance,
            "zone": self.zone,
            "project": self.project,
            "ip": self.ip,
            "started_at": self.started_at,
            "remote_run_dir": self.remote_run_dir,
        }


class ProviderBackend(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def discover_runs(self) -> list[RunRecord]:
        raise NotImplementedError

    @abstractmethod
    def ssh_cmd(self, state: dict[str, Any], remote_cmd: str, timeout: int = 20):
        raise NotImplementedError

    @abstractmethod
    def get_status(self, state: dict[str, Any]) -> str:
        raise NotImplementedError


class GcpProviderBackend(ProviderBackend):
    @property
    def name(self) -> str:
        return "gcp"

    def discover_runs(self) -> list[RunRecord]:
        project = project_from_gcloud_config()
        if not project:
            return []

        rc, out, _ = run_cmd(
            [
                "gcloud",
                "compute",
                "instances",
                "list",
                "--project",
                project,
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

        discovered = []
        for row in rows:
            name = str((row or {}).get("name", "")).strip()
            if not name:
                continue
            zone_raw = str((row or {}).get("zone", "")).strip()
            zone = zone_raw.rsplit("/", 1)[-1] if "/" in zone_raw else zone_raw
            run_id = run_id_from_instance_name(name)
            discovered.append(
                RunRecord(
                    provider=self.name,
                    run_id=run_id,
                    instance=name,
                    zone=zone,
                    project=project,
                    ip=(
                        ((row or {}).get("networkInterfaces") or [{}])[0]
                        .get("accessConfigs", [{}])[0]
                        .get("natIP", "")
                    ),
                    started_at=iso_started_at_from_run_id(run_id),
                )
            )
        return discovered

    def ssh_cmd(self, state: dict[str, Any], remote_cmd: str, timeout: int = 20):
        if _control_plane_enabled(state):
            return _control_plane_exec_text(state, remote_cmd, timeout=timeout)
        cmd = [
            "gcloud",
            "compute",
            "ssh",
            state["instance"],
            "--zone",
            state["zone"],
            "--project",
            state["project"],
            "--command",
            remote_cmd,
        ]
        return run_cmd(cmd, timeout=timeout)

    def get_status(self, state: dict[str, Any]) -> str:
        rc, out, err = run_cmd(
            [
                "gcloud",
                "compute",
                "instances",
                "describe",
                state["instance"],
                "--zone",
                state["zone"],
                "--project",
                state["project"],
                "--format=get(status)",
            ],
            timeout=15,
        )
        if rc == 0:
            return out.strip()
        return "" if _resource_missing(err) else "UNKNOWN"


class DigitalOceanProviderBackend(ProviderBackend):
    @property
    def name(self) -> str:
        return "digitalocean"

    def discover_runs(self) -> list[RunRecord]:
        rc, out, _ = run_cmd(
            [
                "doctl",
                "compute",
                "droplet",
                "list",
                "--tag-name",
                "ctfvm",
                "--format",
                "Name,Region,Status,PublicIPv4",
                "--no-header",
            ],
            timeout=12,
        )
        if rc != 0:
            return []

        discovered = []
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

    def ssh_cmd(self, state: dict[str, Any], remote_cmd: str, timeout: int = 20):
        if _control_plane_enabled(state):
            return _control_plane_exec_text(state, remote_cmd, timeout=timeout)
        ip = str(state.get("ip", "") or "").strip()
        if not ip:
            return 1, "", "missing DigitalOcean IP in local state"
        cmd = [
            "ssh",
            *_do_ssh_common_args(state),
            f"root@{ip}",
            remote_cmd,
        ]
        return run_cmd(cmd, timeout=timeout)

    def get_status(self, state: dict[str, Any]) -> str:
        rc, out, err = run_cmd(
            [
                "doctl",
                "compute",
                "droplet",
                "get",
                state["instance"],
                "--format",
                "Status",
                "--no-header",
            ],
            timeout=15,
        )
        if rc == 0:
            return out.strip()
        return "" if _resource_missing(err) else "UNKNOWN"


class ProviderRegistry:
    def __init__(self, providers: list[ProviderBackend]):
        self._providers = {provider.name: provider for provider in providers}
        self._discovery_cache = {"at": 0.0, "runs": []}

    def get(self, provider: str) -> ProviderBackend:
        normalized = normalize_provider(provider)
        backend = self._providers.get(normalized)
        if backend is None:
            raise KeyError(f"unknown provider: {provider}")
        return backend

    def discover_runs(self, force: bool = False) -> list[dict[str, str]]:
        now = time.monotonic()
        if (
            not force
            and self._discovery_cache["runs"]
            and (now - self._discovery_cache["at"] < DISCOVERY_TTL_SECONDS)
        ):
            return list(self._discovery_cache["runs"])

        discovered: list[dict[str, str]] = []
        for provider in self._providers.values():
            for run in provider.discover_runs():
                discovered.append(run.to_dict())

        self._discovery_cache["at"] = now
        self._discovery_cache["runs"] = discovered
        return list(discovered)
