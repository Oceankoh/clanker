"""Data contracts — the single source of truth for wire/data shapes.

Nothing else in the package defines a wire shape. Service functions return these
dataclasses, never inline dicts (ARCHITECTURE.md Invariant 3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import DEFAULT_CONTROL_PORT
from .identity import (
    normalize_instance_name,
    normalize_provider,
    normalize_run_id,
)

DEFAULT_REMOTE_RUN_DIR = "/home/ctf/run"
DEFAULT_AGENT_BACKEND = "codex"


@dataclass(frozen=True)
class RunRecord:
    """Canonical descriptor for a single run.

    Superset of the old ``providers.RunRecord``: it additionally carries the
    ``agent_backend`` (new in platform-v2) and the control-plane connection
    fields, so the typed control client can be constructed straight from a
    record. The extra fields are *not* part of run identity, so dedup behavior is
    unchanged.
    """

    provider: str
    run_id: str
    instance: str
    zone: str
    project: str
    ip: str = ""
    started_at: str = ""
    challenge_name: str = ""  # display name (challenge folder); see UiService._assign_names
    remote_run_dir: str = DEFAULT_REMOTE_RUN_DIR
    agent_backend: str = DEFAULT_AGENT_BACKEND
    control_scheme: str = ""
    control_host: str = ""
    control_port: str = ""
    control_user: str = ""
    control_password: str = ""

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "RunRecord":
        data = data or {}
        instance = normalize_instance_name(data.get("instance", ""))
        run_id = normalize_run_id(data.get("run_id", ""), instance)
        backend = str(data.get("agent_backend", "") or "").strip().lower() or DEFAULT_AGENT_BACKEND
        return cls(
            provider=normalize_provider(data.get("provider", "gcp")),
            run_id=run_id,
            instance=instance,
            zone=str(data.get("zone", "") or "").strip(),
            project=str(data.get("project", "") or "").strip(),
            ip=str(data.get("ip", "") or "").strip(),
            started_at=str(data.get("started_at", "") or "").strip(),
            challenge_name=str(data.get("challenge_name", "") or "").strip(),
            remote_run_dir=str(data.get("remote_run_dir", "") or DEFAULT_REMOTE_RUN_DIR),
            agent_backend=backend,
            control_scheme=str(data.get("control_scheme", "") or "").strip(),
            control_host=str(data.get("control_host", "") or "").strip(),
            control_port=str(data.get("control_port", "") or "").strip(),
            control_user=str(data.get("control_user", "") or "").strip(),
            control_password=str(data.get("control_password", "") or "").strip(),
        )

    def to_state_dict(self) -> dict[str, str]:
        """The shape the provider/status helpers consume (matches old ``to_dict``
        plus the control + agent fields)."""
        return {
            "provider": self.provider,
            "agent_backend": self.agent_backend,
            "run_id": self.run_id,
            "instance": self.instance,
            "zone": self.zone,
            "project": self.project,
            "ip": self.ip,
            "started_at": self.started_at,
            "challenge_name": self.challenge_name,
            "remote_run_dir": self.remote_run_dir,
            "control_scheme": self.control_scheme,
            "control_host": self.control_host,
            "control_port": self.control_port,
            "control_user": self.control_user,
            "control_password": self.control_password,
        }

    # --- control plane -----------------------------------------------------
    @property
    def control_endpoint(self) -> str:
        scheme = self.control_scheme or "http"
        host = self.control_host or self.ip
        port = self.control_port or DEFAULT_CONTROL_PORT
        if not host:
            return ""
        return f"{scheme}://{host}:{port}"

    @property
    def has_control_plane(self) -> bool:
        return bool(self.control_endpoint and self.control_user and self.control_password)


@dataclass
class RunListing:
    """A run as surfaced by ``RunRegistry.list_runs`` — the record plus the
    transient list-only fields (selector key, cached runtime status)."""

    record: RunRecord
    run_key: str
    runtime_status: str = ""
    is_runtime_active: bool = False
    source: str = ""


@dataclass
class ExecResult:
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def stdout_text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")

    def stderr_text(self) -> str:
        return self.stderr.decode("utf-8", errors="replace")


# --- Snapshot family (consumed in Phase 4; defined here as the contract) ----


@dataclass
class PaneSnapshot:
    session: str
    window_index: int
    window_name: str
    active: bool
    target: str
    output: str = ""


@dataclass
class SnapshotMetrics:
    snapshot_at: str = ""
    findings_mtime: str = ""
    supervisor_mtime: str = ""
    artifact_mtime: str = ""
    artifact_count: int = 0


@dataclass
class ExplicitStatus:
    state: str
    note: str = ""
    updated_at: str = ""


@dataclass
class ChallengeState:
    state: str
    label: str = ""
    summary: str = ""
    last_activity_age_sec: int | None = None
    last_activity_label: str = ""


@dataclass
class ArtifactEntry:
    relpath: str
    size_bytes: int
    mtime: str


@dataclass
class Subagent:
    id: str
    tmux_target: str
    kind: str = ""
    label: str = ""
    state: str = "running"
    steerable: bool = True


# NB: the live spawn-job shape lives in clanker/server/jobs.py (SpawnJob) — that's
# what /api/v1/jobs serializes. A duplicate dataclass here was unused and only bred
# confusion, so it was removed; ProvisioningJob below is the model-layer job shape.
@dataclass
class ProvisioningJob:
    """A spawn job still provisioning a run that has no control plane yet —
    surfaced in the run's snapshot so the UI can show progress
    ("Waiting for VM control plane availability…") instead of a bare error."""
    job_id: str
    state: str = ""
    started_at: str = ""
    finished_at: str = ""
    output_tail: str = ""


@dataclass
class Snapshot:
    record: RunRecord
    runtime_status: str = ""
    findings_tail: str = ""
    supervisor_tail: str = ""
    panes: list[PaneSnapshot] = field(default_factory=list)
    artifacts: list[ArtifactEntry] = field(default_factory=list)
    metrics: SnapshotMetrics = field(default_factory=SnapshotMetrics)
    explicit_status: ExplicitStatus | None = None
    challenge_state: ChallengeState | None = None
    subagents: list[Subagent] = field(default_factory=list)
    error: str | None = None
    # Live provisioning output, surfaced when the run has no control plane yet
    # (e.g. still "Waiting for VM control plane availability…"). One entry per
    # spawn job still working on this run_id; empty once the run is reachable.
    provisioning: list[ProvisioningJob] = field(default_factory=list)
