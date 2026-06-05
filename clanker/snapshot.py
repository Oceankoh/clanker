"""Run snapshot: a single JSON envelope instead of marker-delimited text.

Fixes BUGS.md B1 (marker collision) and B7 (control-plane only). The remote
gatherer is a Python program (see ``build_snapshot_script``) that emits ONE JSON
object — every variable-length field (pane output, findings/supervisor tails) is
base64-encoded, so no amount of marker-like or pipe content in the workspace can
corrupt parsing. Transport is the typed control client only; a run without
control credentials raises (surfaced as the snapshot ``error`` field), never an
SSH fallback (Invariant 1).
"""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone

from .config import MAX_FINDINGS_TAIL_BYTES, MAX_PANE_TAIL_BYTES, MAX_SUPERVISOR_TAIL_BYTES
from .controlclient import ControlPlaneClient, ControlPlaneError
from .models import (
    ArtifactEntry,
    ChallengeState,
    ExplicitStatus,
    PaneSnapshot,
    RunRecord,
    Snapshot,
    SnapshotMetrics,
)
from .remote import remote_python

ACTIVE_RUNTIME_STATUSES = {"running", "active"}
HALTED_PATTERNS = [
    re.compile(r"session exited with code", re.IGNORECASE),
    re.compile(r"dropping to shell", re.IGNORECASE),
    re.compile(r"container not running", re.IGNORECASE),
    re.compile(r"codex cli not found", re.IGNORECASE),
    re.compile(r"cli not found in ctf-toolbox", re.IGNORECASE),  # generalized for any agent
]

# The remote gatherer body. RUN_DIR / INCLUDE_ARTIFACTS / *_BYTES are prepended
# as a header; this body uses them and prints one JSON object.
_SNAPSHOT_BODY = r'''
import base64, json, os, subprocess, time


def _cap_tail(path, n):
    try:
        with open(path, "rb") as f:
            try:
                f.seek(-n, os.SEEK_END)
            except OSError:
                f.seek(0)
            return base64.b64encode(f.read()[-n:]).decode("ascii")
    except Exception:
        return ""


def _mtime(path):
    try:
        return int(os.stat(path).st_mtime)
    except Exception:
        return 0


out = {"error": None, "no_tmux": False, "panes": [], "findings_b64": "",
       "supervisor_b64": "", "explicit_status": None, "metrics": {}, "artifacts": []}

try:
    _have_tmux = subprocess.run(["tmux", "list-sessions"], capture_output=True).returncode == 0
except Exception:
    _have_tmux = False
if not _have_tmux:
    out["no_tmux"] = True
    print(json.dumps(out))
    raise SystemExit(0)


def _sh(args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return ""


panes = []
for sess in [s for s in _sh(["tmux", "list-sessions", "-F", "#{session_name}"]).splitlines() if s.strip()]:
    fmt = "#{window_index}\t#{window_name}\t#{window_active}"
    for line in _sh(["tmux", "list-windows", "-t", sess, "-F", fmt]).splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        idx, name, active = parts[0], parts[1], parts[2]
        cap = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", sess + ":" + idx, "-S", "-120"],
            capture_output=True,
        )
        data = cap.stdout[-PANE_BYTES:] if cap.returncode == 0 else b""
        panes.append({
            "session": sess, "index": idx, "name": name,
            "active": active.strip() == "1", "target": sess + ":" + idx,
            "output_b64": base64.b64encode(data).decode("ascii"),
        })
out["panes"] = panes

out["findings_b64"] = _cap_tail(os.path.join(RUN_DIR, "findings.md"), FINDINGS_BYTES)
out["supervisor_b64"] = _cap_tail(os.path.join(RUN_DIR, "logs", "supervisor.log"), SUPERVISOR_BYTES)

try:
    with open(os.path.join(RUN_DIR, "ui-status.json")) as f:
        loaded = json.load(f)
        out["explicit_status"] = loaded if isinstance(loaded, dict) else None
except Exception:
    out["explicit_status"] = None

art_dir = os.path.join(RUN_DIR, "artifacts")
art_mtime, art_count, artifacts = 0, 0, []
for root, _dirs, files in os.walk(art_dir):
    for fn in files:
        full = os.path.join(root, fn)
        art_count += 1
        m = _mtime(full)
        art_mtime = max(art_mtime, m)
        rel = os.path.relpath(full, RUN_DIR)
        if INCLUDE_ARTIFACTS and rel.count("/") <= 3:
            try:
                size = os.path.getsize(full)
            except Exception:
                size = 0
            artifacts.append({"relpath": rel, "size": size, "mtime": m})
artifacts.sort(key=lambda a: a["relpath"])
out["artifacts"] = artifacts

out["metrics"] = {
    "now": int(time.time()),
    "findings_mtime": _mtime(os.path.join(RUN_DIR, "findings.md")),
    "inject_mtime": _mtime(os.path.join(RUN_DIR, "inject.queue")),
    "supervisor_mtime": _mtime(os.path.join(RUN_DIR, "logs", "supervisor.log")),
    "artifact_mtime": art_mtime,
    "artifact_count": art_count,
}

print(json.dumps(out))
'''


def build_snapshot_script(remote_run_dir: str, include_artifacts: bool = True) -> str:
    header = (
        f"RUN_DIR = {remote_run_dir!r}\n"
        f"INCLUDE_ARTIFACTS = {bool(include_artifacts)}\n"
        f"PANE_BYTES = {MAX_PANE_TAIL_BYTES}\n"
        f"FINDINGS_BYTES = {MAX_FINDINGS_TAIL_BYTES}\n"
        f"SUPERVISOR_BYTES = {MAX_SUPERVISOR_TAIL_BYTES}\n"
    )
    return header + _SNAPSHOT_BODY


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

def _b64_text(value: str) -> str:
    try:
        return base64.b64decode((value or "").encode("ascii"), validate=False).decode("utf-8", "replace")
    except Exception:
        return ""


def _epoch_to_iso(epoch: int) -> str:
    if not epoch:
        return ""
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return ""


def _safe_int(value, default=0) -> int:
    try:
        return int(float(str(value).strip()))
    except Exception:
        return default


def _runtime_active(status: str) -> bool:
    return str(status or "").strip().lower() in ACTIVE_RUNTIME_STATUSES


def _tail_lines(text: str, count: int) -> str:
    lines = [ln for ln in str(text or "").splitlines() if ln.strip()]
    return "\n".join(lines[-count:]) if count > 0 else ""


def _notes_section(text: str) -> str:
    text = str(text or "")
    idx = text.rfind("## Notes")
    body = text[idx + len("## Notes"):] if idx != -1 else text
    return _tail_lines(body, 40)


def _format_age(age: int | None) -> str:
    if age is None or age < 0:
        return "unknown"
    if age < 60:
        return f"{age}s ago"
    minutes = age // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def derive_challenge_state(
    *,
    runtime_status: str,
    panes: list[PaneSnapshot],
    findings_tail: str,
    supervisor_tail: str,
    metrics: dict,
    explicit_status: ExplicitStatus | None,
) -> ChallengeState:
    signal = "\n".join(
        chunk for chunk in [
            _notes_section(findings_tail),
            _tail_lines(supervisor_tail, 40),
            *[_tail_lines(p.output, 16) for p in panes[:6]],
        ] if chunk
    ).lower()

    now = _safe_int(metrics.get("now"))
    last = max(
        _safe_int(metrics.get("findings_mtime")),
        _safe_int(metrics.get("supervisor_mtime")),
        _safe_int(metrics.get("artifact_mtime")),
        _safe_int(metrics.get("inject_mtime")),
    )
    age = max(now - last, 0) if now and last else None
    age_label = _format_age(age)
    count = _safe_int(metrics.get("artifact_count"))

    def cs(state, label, summary):
        return ChallengeState(state=state, label=label, summary=summary,
                              last_activity_age_sec=age, last_activity_label=age_label)

    estate = (explicit_status.state.strip().lower() if explicit_status and explicit_status.state else "")
    enote = (explicit_status.note.strip() if explicit_status and explicit_status.note else "")
    if estate == "solved":
        return cs("solved", "Solved", f"Marked solved. {enote}".strip() if enote else "Marked solved explicitly for this run.")
    if estate == "blocked":
        return cs("blocked", "Blocked", f"Marked blocked. {enote}".strip() if enote else "Marked blocked explicitly for this run.")

    if not _runtime_active(runtime_status):
        label = str(runtime_status or "UNKNOWN").strip() or "UNKNOWN"
        return cs("stopped", "Stopped", f"VM runtime is {label}.")
    if any(p.search(signal) for p in HALTED_PATTERNS):
        return cs("halted", "Halted", "The supervisor appears to have exited or fallen back to a shell.")
    if not panes:
        return cs("halted", "Halted", "No tmux panes are available for this run.")
    if age is not None and age >= 900:
        return cs("stalled", "Stalled", "No fresh findings, logs, or artifacts in the last 15 minutes.")
    return cs("progressing", "In Progress",
              f"VM running; workspace changing. Last activity {age_label}; artifacts {count}.")


def parse_snapshot_response(stdout: bytes, *, record: RunRecord, runtime_status: str = "") -> Snapshot:
    try:
        data = json.loads(stdout.decode("utf-8") or "{}")
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except Exception:
        return _error_snapshot(record, runtime_status, "Unexpected monitor output format.")

    if data.get("no_tmux"):
        return _error_snapshot(record, runtime_status, "No tmux sessions found in VM.")

    panes = []
    for p in data.get("panes", []) or []:
        idx_raw = str(p.get("index", "") or "")
        panes.append(PaneSnapshot(
            session=str(p.get("session", "")),
            window_index=int(idx_raw) if idx_raw.isdigit() else 0,
            window_name=str(p.get("name", "")),
            active=bool(p.get("active", False)),
            target=str(p.get("target", "")),
            output=_b64_text(p.get("output_b64", "")),
        ))

    findings_tail = _b64_text(data.get("findings_b64", ""))
    supervisor_tail = _b64_text(data.get("supervisor_b64", ""))

    explicit_status = None
    es = data.get("explicit_status")
    if isinstance(es, dict) and es.get("state"):
        explicit_status = ExplicitStatus(
            state=str(es.get("state", "")),
            note=str(es.get("note", "") or ""),
            updated_at=str(es.get("updated_at", "") or ""),
        )

    raw_metrics = data.get("metrics", {}) or {}
    metrics = SnapshotMetrics(
        snapshot_at=_epoch_to_iso(_safe_int(raw_metrics.get("now"))),
        findings_mtime=_epoch_to_iso(_safe_int(raw_metrics.get("findings_mtime"))),
        supervisor_mtime=_epoch_to_iso(_safe_int(raw_metrics.get("supervisor_mtime"))),
        artifact_mtime=_epoch_to_iso(_safe_int(raw_metrics.get("artifact_mtime"))),
        artifact_count=_safe_int(raw_metrics.get("artifact_count")),
    )

    artifacts = [
        ArtifactEntry(
            relpath=str(a.get("relpath", "")),
            size_bytes=_safe_int(a.get("size")),
            mtime=_epoch_to_iso(_safe_int(a.get("mtime"))),
        )
        for a in (data.get("artifacts", []) or [])
    ]

    # legacy: if panes exist but runtime status wasn't 'active', treat as active
    effective_status = runtime_status
    if panes and not _runtime_active(runtime_status):
        effective_status = "active"

    challenge = derive_challenge_state(
        runtime_status=effective_status, panes=panes,
        findings_tail=findings_tail, supervisor_tail=supervisor_tail,
        metrics=raw_metrics, explicit_status=explicit_status,
    )

    return Snapshot(
        record=record, runtime_status=effective_status,
        findings_tail=findings_tail, supervisor_tail=supervisor_tail,
        panes=panes, artifacts=artifacts, metrics=metrics,
        explicit_status=explicit_status, challenge_state=challenge, error=None,
    )


def _error_snapshot(record: RunRecord, runtime_status: str, error: str) -> Snapshot:
    challenge = derive_challenge_state(
        runtime_status=runtime_status, panes=[], findings_tail="", supervisor_tail="",
        metrics={}, explicit_status=None,
    )
    return Snapshot(record=record, runtime_status=runtime_status,
                    challenge_state=challenge, error=error)


def fetch_snapshot(
    client: ControlPlaneClient,
    record: RunRecord,
    *,
    include_artifacts: bool = True,
    runtime_status: str = "",
    timeout: int = 25,
) -> Snapshot:
    """Control-plane only: build → exec → parse. Never falls back to SSH."""
    script = build_snapshot_script(record.remote_run_dir, include_artifacts)
    try:
        result = client.exec(remote_python(script), timeout=timeout)
    except ControlPlaneError as exc:
        return _error_snapshot(record, runtime_status, str(exc))
    if not result.ok:
        return _error_snapshot(record, runtime_status,
                               result.stderr_text().strip() or "Failed to query VM")
    return parse_snapshot_response(result.stdout, record=record, runtime_status=runtime_status)
