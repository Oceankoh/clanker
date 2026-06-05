"""Model -> API.md JSON mapping. The one place wire JSON shapes are produced."""

from __future__ import annotations

from ..models import (
    ArtifactEntry,
    ChallengeState,
    ExplicitStatus,
    PaneSnapshot,
    ProvisioningJob,
    RunListing,
    Snapshot,
    SnapshotMetrics,
    Subagent,
)
from .jobs import SpawnJob


def challenge_state(cs: ChallengeState | None) -> dict | None:
    if cs is None:
        return None
    return {
        "state": cs.state,
        "label": cs.label,
        "summary": cs.summary,
        "last_activity_age_sec": cs.last_activity_age_sec,
        "last_activity_label": cs.last_activity_label,
    }


def run_listing(l: RunListing) -> dict:
    r = l.record
    return {
        "run_id": r.run_id,
        "name": r.challenge_name,
        "provider": r.provider,
        "agent_backend": r.agent_backend,
        "instance": r.instance,
        "zone": r.zone,
        "project": r.project,
        "ip": r.ip,
        "started_at": r.started_at,
        "runtime_status": l.runtime_status,
        "challenge_state": None,
    }


def pane(p: PaneSnapshot) -> dict:
    return {
        "session": p.session,
        "window_index": p.window_index,
        "window_name": p.window_name,
        "active": p.active,
        "target": p.target,
        "output": p.output,
    }


def metrics(m: SnapshotMetrics) -> dict:
    return {
        "snapshot_at": m.snapshot_at,
        "findings_mtime": m.findings_mtime,
        "supervisor_mtime": m.supervisor_mtime,
        "artifact_mtime": m.artifact_mtime,
        "artifact_count": m.artifact_count,
    }


def explicit_status(es: ExplicitStatus | None) -> dict | None:
    if es is None:
        return None
    return {"state": es.state, "note": es.note, "updated_at": es.updated_at}


def artifact(a: ArtifactEntry) -> dict:
    return {"relpath": a.relpath, "size_bytes": a.size_bytes, "mtime": a.mtime}


def subagent(s: Subagent) -> dict:
    return {
        "id": s.id,
        "tmux_target": s.tmux_target,
        "kind": s.kind,
        "label": s.label,
        "state": s.state,
        "steerable": s.steerable,
    }


def snapshot(snap: Snapshot) -> dict:
    r = snap.record
    return {
        "run_id": r.run_id,
        "name": r.challenge_name,
        "provider": r.provider,
        "agent_backend": r.agent_backend,
        "instance": r.instance,
        "zone": r.zone,
        "project": r.project,
        "ip": r.ip,
        "started_at": r.started_at,
        "runtime_status": snap.runtime_status,
        "findings_tail": snap.findings_tail,
        "supervisor_tail": snap.supervisor_tail,
        "panes": [pane(p) for p in snap.panes],
        "artifacts": [artifact(a) for a in snap.artifacts],
        "metrics": metrics(snap.metrics),
        "explicit_status": explicit_status(snap.explicit_status),
        "challenge_state": challenge_state(snap.challenge_state),
        "subagents": [subagent(s) for s in snap.subagents],
        "error": snap.error,
        "provisioning": [provisioning_job(p) for p in snap.provisioning],
    }


def provisioning_job(p: ProvisioningJob) -> dict:
    return {
        "job_id": p.job_id,
        "state": p.state,
        "started_at": p.started_at,
        "finished_at": p.finished_at,
        "output_tail": p.output_tail,
    }


def job(j: SpawnJob) -> dict:
    return {
        "job_id": j.job_id,
        "state": j.state,
        "run_id": j.run_id,
        "started_at": j.started_at,
        "finished_at": j.finished_at,
        "output_tail": j.output,
    }
