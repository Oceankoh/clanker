#!/usr/bin/env python3
import base64
import mimetypes
import os
import re
import shlex
import shutil
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .common import (
    MAX_ARTIFACT_PREVIEW_BYTES,
    MAX_FINDINGS_TAIL_BYTES,
    MAX_PANE_TAIL_BYTES,
    ROOT,
    RUNS_DIR,
    STATE_FILE,
    load_json,
    state_valid,
)
from .providers import (
    DigitalOceanProviderBackend,
    GcpProviderBackend,
    ProviderRegistry,
    RunRecord,
    normalize_instance_name,
    normalize_provider,
    normalize_run_id,
    project_from_gcloud_config,
    provider_from_state,
)


STATUS_CACHE_TTL_SECONDS = 10
MAX_SUPERVISOR_TAIL_BYTES = 16_000
MAX_SPAWN_OUTPUT_BYTES = 64_000
MAX_SPAWN_JOBS = 12
RUN_BUNDLE_MAX_BYTES = 32_000_000
ACTIVE_RUNTIME_STATUSES = {"running", "active"}
HALTED_PATTERNS = [
    re.compile(r"session exited with code", re.IGNORECASE),
    re.compile(r"dropping to shell", re.IGNORECASE),
    re.compile(r"container not running", re.IGNORECASE),
    re.compile(r"codex cli not found", re.IGNORECASE),
]


def _utc_now():
    return datetime.now(timezone.utc)


def _utc_now_iso():
    return _utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_run_entry(entry):
    row = dict(entry or {})
    row["provider"] = provider_from_state(row)
    row["instance"] = normalize_instance_name(row.get("instance", ""))
    row["run_id"] = normalize_run_id(row.get("run_id", ""), row.get("instance", ""))
    row["zone"] = str(row.get("zone", "") or "").strip()
    row["project"] = str(row.get("project", "") or "").strip()
    row["ip"] = str(row.get("ip", "") or "").strip()
    row["started_at"] = str(row.get("started_at", "") or "").strip()
    row["run_key"] = _run_selector(row)
    return row


def _run_selector(run):
    run = dict(run or {})
    provider = normalize_provider(run.get("provider", ""))
    project = str(run.get("project", "") or "").strip()
    zone = str(run.get("zone", "") or "").strip()
    ip = str(run.get("ip", "") or "").strip()
    instance = normalize_instance_name(run.get("instance", ""))
    run_id = normalize_run_id(run.get("run_id", ""), instance)
    if instance:
        return f"instance:{provider}|{project}|{zone}|{instance}"
    if ip:
        return f"ip:{provider}|{project}|{zone}|{ip}"
    if run_id:
        return f"run:{provider}|{project}|{zone}|{run_id}"
    return ""


def _run_identity_keys(run):
    run = _normalize_run_entry(run)
    keys = []
    provider = str((run or {}).get("provider", "")).strip()
    project = str((run or {}).get("project", "")).strip()
    zone = str((run or {}).get("zone", "")).strip()
    ip = str((run or {}).get("ip", "")).strip()
    instance = str((run or {}).get("instance", "")).strip()
    run_id = str((run or {}).get("run_id", "")).strip()
    if project and zone and ip:
        keys.append(f"ip:{provider}|{project}|{zone}|{ip}")
    elif ip:
        keys.append(f"ip:{provider}|{ip}")
    if project and zone and instance:
        keys.append(f"inst:{provider}|{project}|{zone}|{instance}")
    elif instance:
        keys.append(f"inst:{provider}|{instance}")
    if run_id and not instance:
        keys.append(f"run:{provider}|{run_id}")
    return keys


def _merge_run_entries(old, new):
    merged = _normalize_run_entry(old)
    new = _normalize_run_entry(new)
    source_rank = {"local": 0, "current": 1, "discovered": 2}
    old_src = str((old or {}).get("__source", "local"))
    new_src = str((new or {}).get("__source", "local"))
    prefer_new = source_rank.get(new_src, 0) >= source_rank.get(old_src, 0)

    fields = ["run_id", "instance", "zone", "project", "ip", "started_at"]
    if new.get("provider"):
        merged["provider"] = new.get("provider")
    for field in fields:
        old_val = str((old or {}).get(field, "") or "")
        new_val = str((new or {}).get(field, "") or "")
        if prefer_new and new_val:
            merged[field] = new_val
        elif not old_val and new_val:
            merged[field] = new_val
    merged["__source"] = new_src if prefer_new else old_src
    merged["run_key"] = _run_selector(merged)
    return merged


def _safe_int(value, default=0):
    try:
        return int(float(str(value).strip()))
    except Exception:
        return default


def _runtime_status_is_active(status):
    return str(status or "").strip().lower() in ACTIVE_RUNTIME_STATUSES


def _tail_lines(text, count):
    lines = [line for line in str(text or "").splitlines() if line.strip()]
    if count <= 0:
        return ""
    return "\n".join(lines[-count:])


def _notes_section(text):
    text = str(text or "")
    marker = "## Notes"
    idx = text.rfind(marker)
    if idx == -1:
        return _tail_lines(text, 40)
    notes = text[idx + len(marker) :].strip()
    return _tail_lines(notes, 40)


def _format_age(age_seconds):
    if age_seconds is None or age_seconds < 0:
        return "unknown"
    if age_seconds < 60:
        return f"{age_seconds}s ago"
    minutes = age_seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def _challenge_signal_text(snapshot):
    recent_notes = _notes_section(snapshot.get("findings_tail", ""))
    recent_log = _tail_lines(snapshot.get("supervisor_tail", ""), 40)
    pane_chunks = [_tail_lines(window.get("output", ""), 16) for window in snapshot.get("windows_list", [])[:6]]
    return "\n".join(chunk for chunk in [recent_notes, recent_log, *pane_chunks] if chunk).strip()


def _parse_explicit_state(raw):
    raw = str(raw or "").strip().lower()
    if raw in {"solved", "blocked", "clear"}:
        return raw
    return ""


def _read_optional_text(path: Path) -> str:
    try:
        return path.read_text().strip()
    except Exception:
        return ""


class CTFVMUIService:
    def __init__(self, providers: ProviderRegistry):
        self.providers = providers
        self._status_cache = {}
        self._status_lock = threading.Lock()
        self._spawn_jobs = {}
        self._spawn_job_order = []
        self._spawn_lock = threading.Lock()

    def load_state(self, run_id=None):
        wanted = str(run_id or "").strip()
        if wanted:
            path = RUNS_DIR / f"{wanted}.json"
            if path.exists():
                state = load_json(path)
                return state if state_valid(state) else None
            current = load_json(STATE_FILE) if STATE_FILE.exists() else None
            if state_valid(current) and self._state_matches_selector(current, wanted):
                return current
            if RUNS_DIR.exists():
                for candidate in sorted(RUNS_DIR.glob("*.json")):
                    state = load_json(candidate)
                    if state_valid(state) and self._state_matches_selector(state, wanted):
                        return state
            return None

        if not STATE_FILE.exists():
            return None
        state = load_json(STATE_FILE)
        return state if state_valid(state) else None

    def discover_runs(self, force: bool = False):
        return self.providers.discover_runs(force=force)

    def _state_matches_selector(self, state, selector):
        selector = str(selector or "").strip()
        if not selector or not state_valid(state):
            return False
        row = _normalize_run_entry(state)
        return selector in {
            str(row.get("run_key", "") or "").strip(),
            str(row.get("run_id", "") or "").strip(),
            str(row.get("instance", "") or "").strip(),
        }

    def _prune_local_state_for_run(self, run):
        run = _normalize_run_entry(run)
        run_id = str(run.get("run_id", "") or "").strip()
        instance = str(run.get("instance", "") or "").strip()

        for name in [run_id, instance]:
            if not name:
                continue
            path = RUNS_DIR / f"{name}.json"
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except Exception:
                pass

        current = load_json(STATE_FILE) if STATE_FILE.exists() else None
        current_run_id = str((current or {}).get("run_id", "") or "").strip()
        current_instance = normalize_instance_name((current or {}).get("instance", ""))
        if current and ((run_id and current_run_id == run_id) or (instance and current_instance == instance)):
            try:
                STATE_FILE.unlink()
            except FileNotFoundError:
                pass
            except Exception:
                pass

    def get_spawn_defaults(self):
        provider = normalize_provider(os.environ.get("CTFVM_PROVIDER") or "gcp")
        return {
            "provider": provider,
            "timeout_min": str(os.environ.get("CTFVM_TIMEOUT_MIN") or "1440"),
            "toolbox_variant": str(os.environ.get("CTFVM_TOOLBOX_VARIANT") or "lean"),
            "gcp_project": str(os.environ.get("CTFVM_GCP_PROJECT") or project_from_gcloud_config() or ""),
            "gcp_zone": str(os.environ.get("CTFVM_GCP_ZONE") or ""),
            "gcp_machine_type": str(os.environ.get("CTFVM_GCP_MACHINE_TYPE") or "e2-standard-4"),
            "do_region": str(os.environ.get("CTFVM_DO_REGION") or ""),
            "do_size_slug": str(os.environ.get("CTFVM_DO_SIZE_SLUG") or "s-4vcpu-8gb"),
        }

    def choose_local_directory(self, current_path="", batch_mode=False):
        if os.uname().sysname.lower() != "darwin":
            return {"ok": False, "error": "Finder directory selection is only available on macOS."}
        if shutil.which("osascript") is None:
            return {"ok": False, "error": "osascript is not available on this machine."}

        prompt = "Select challenges root directory" if batch_mode else "Select challenge directory"
        current = str(current_path or "").strip()
        script_lines = [
            "on run argv",
            'set promptText to item 1 of argv',
            'set selectedFolder to choose folder with prompt promptText',
            'return POSIX path of selectedFolder',
            "end run",
        ]
        cmd = ["osascript"]
        for line in script_lines:
            cmd += ["-e", line]
        cmd.append(prompt)

        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=300,
                cwd=str(ROOT),
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "Finder selection timed out."}
        except Exception as exc:
            return {"ok": False, "error": f"Failed to open Finder: {exc}"}

        if proc.returncode != 0:
            text = (proc.stderr or proc.stdout or "").strip()
            lowered = text.lower()
            if "user canceled" in lowered:
                return {"ok": False, "canceled": True, "error": "Selection canceled."}
            return {"ok": False, "error": text or "Finder selection failed."}

        selected = (proc.stdout or "").strip()
        if not selected:
            return {"ok": False, "error": "No directory selected."}

        path = Path(selected).expanduser().resolve()
        if not path.exists():
            return {"ok": False, "error": f"Selected directory not found: {path}"}
        if not path.is_dir():
            return {"ok": False, "error": f"Selected path is not a directory: {path}"}

        return {
            "ok": True,
            "path": str(path),
            "changed": str(path) != current,
            "description_text": "" if batch_mode else _read_optional_text(path / "description.txt"),
        }

    def _status_cache_key(self, state):
        state = state or {}
        return "|".join(
            [
                provider_from_state(state),
                str(state.get("project", "") or ""),
                str(state.get("zone", "") or ""),
                str(state.get("instance", "") or ""),
                str(state.get("run_id", "") or ""),
            ]
        )

    def get_status(self, state):
        provider = self.providers.get(provider_from_state(state))
        return provider.get_status(state)

    def get_status_cached(self, state, force=False):
        key = self._status_cache_key(state)
        now = _utc_now().timestamp()
        with self._status_lock:
            cached = self._status_cache.get(key)
            if (
                cached
                and not force
                and (now - float(cached.get("at", 0))) < STATUS_CACHE_TTL_SECONDS
            ):
                return cached.get("status", "UNKNOWN")

        status = self.get_status(state)
        with self._status_lock:
            self._status_cache[key] = {"at": now, "status": status}
        return status

    def list_runs(self, include_status=False, force_status=False, force_discovery=False, only_live=False):
        runs = []
        current = self.load_state()
        current_run_id = _run_selector(current) if current else ""

        if RUNS_DIR.exists():
            for path in sorted(RUNS_DIR.glob("*.json")):
                state = load_json(path)
                if not state_valid(state):
                    continue
                run_id = str(state.get("run_id") or path.stem)
                runs.append(
                    _normalize_run_entry(
                        {
                            "provider": provider_from_state(state),
                            "run_id": run_id,
                            "instance": state.get("instance", ""),
                            "zone": state.get("zone", ""),
                            "project": state.get("project", ""),
                            "ip": state.get("ip", ""),
                            "started_at": state.get("started_at", ""),
                            "__source": "local",
                        }
                    )
                )

        if current and current_run_id:
            runs.append(
                _normalize_run_entry(
                    {
                        "provider": provider_from_state(current),
                        "run_id": current.get("run_id", ""),
                        "instance": current.get("instance", ""),
                        "zone": current.get("zone", ""),
                        "project": current.get("project", ""),
                        "ip": current.get("ip", ""),
                        "started_at": current.get("started_at", ""),
                        "__source": "current",
                    }
                )
            )

        for discovered in self.discover_runs(force=force_discovery):
            row = _normalize_run_entry(discovered)
            row["__source"] = "discovered"
            runs.append(row)

        merged = []
        key_to_idx = {}
        for run in runs:
            keys = _run_identity_keys(run)
            idx = None
            for key in keys:
                if key in key_to_idx:
                    idx = key_to_idx[key]
                    break
            if idx is None:
                idx = len(merged)
                merged.append(run)
            else:
                merged[idx] = _merge_run_entries(merged[idx], run)
            for key in _run_identity_keys(merged[idx]):
                key_to_idx[key] = idx

        runs = merged
        runs.sort(key=lambda run: run.get("started_at", ""), reverse=True)

        if include_status or only_live:
            for run in runs:
                state = self.state_from_run_entry(run)
                runtime_status = self.get_status_cached(state, force=force_status) if state else "UNKNOWN"
                run["runtime_status"] = runtime_status
                run["is_runtime_active"] = _runtime_status_is_active(runtime_status)

        if only_live:
            live_runs = []
            for run in runs:
                source = str(run.get("__source", "") or "")
                runtime_status = str(run.get("runtime_status", "") or "").strip()
                if source == "discovered" or run.get("is_runtime_active"):
                    live_runs.append(run)
                    continue
                if runtime_status == "":
                    self._prune_local_state_for_run(run)
            runs = live_runs
            if current_run_id and not any(str(run.get("run_key", "")) == current_run_id for run in runs):
                current_run_id = ""

        if not current_run_id:
            for run in runs:
                candidate_run_id = str(run.get("run_key", "") or "").strip()
                if candidate_run_id:
                    current_run_id = candidate_run_id
                    break

        for run in runs:
            run.pop("__source", None)

        return runs, current_run_id

    def state_from_run_entry(self, entry):
        if not entry:
            return None
        state = RunRecord.from_mapping(entry).to_dict()
        return state if state_valid(state) else None

    def resolve_state(self, run_id=None):
        wanted = str(run_id or "").strip()
        if not wanted:
            return None

        state = self.load_state(wanted)
        if state:
            return state

        runs, _ = self.list_runs()
        for run in runs:
            if wanted in {
                str(run.get("run_key", "") or "").strip(),
                str(run.get("run_id", "") or "").strip(),
                str(run.get("instance", "") or "").strip(),
            }:
                return self.state_from_run_entry(run)
        return None

    def _ensure_selected_run_present(self, runs, state, runtime_status=None):
        state = state if state_valid(state) else None
        if not state:
            return runs
        run_key = _run_selector(state)
        if run_key and any(str(run.get("run_key", "")) == run_key for run in runs):
            return runs
        row = _normalize_run_entry(
            {
                "provider": provider_from_state(state),
                "run_id": state.get("run_id", ""),
                "instance": state.get("instance", ""),
                "zone": state.get("zone", ""),
                "project": state.get("project", ""),
                "ip": state.get("ip", ""),
                "started_at": state.get("started_at", ""),
            }
        )
        if runtime_status is not None:
            row["runtime_status"] = runtime_status
            row["is_runtime_active"] = _runtime_status_is_active(runtime_status)
        merged = list(runs)
        merged.append(row)
        merged.sort(key=lambda run: run.get("started_at", ""), reverse=True)
        return merged

    def ssh_cmd(self, state, remote_cmd, timeout=20):
        provider = self.providers.get(provider_from_state(state))
        return provider.ssh_cmd(state, remote_cmd, timeout=timeout)

    def derive_challenge_state(self, snapshot):
        runtime_status = snapshot.get("status", "")
        signal_text = _challenge_signal_text(snapshot)
        signal_text_lower = signal_text.lower()
        metrics = snapshot.get("metrics", {}) or {}
        explicit_status = snapshot.get("explicit_status", {}) or {}
        explicit_state = _parse_explicit_state(explicit_status.get("state"))
        now_epoch = _safe_int(metrics.get("now"), default=0)
        last_activity_epoch = max(
            _safe_int(metrics.get("findings_mtime")),
            _safe_int(metrics.get("supervisor_mtime")),
            _safe_int(metrics.get("artifact_mtime")),
            _safe_int(metrics.get("inject_mtime")),
        )
        last_activity_age = max(now_epoch - last_activity_epoch, 0) if now_epoch and last_activity_epoch else None
        last_activity_label = _format_age(last_activity_age)
        artifact_count = _safe_int(metrics.get("artifact_count"), default=0)

        if explicit_state == "solved":
            note = str(explicit_status.get("note", "") or "").strip()
            return {
                "state": "solved",
                "label": "Solved",
                "summary": "Marked solved explicitly for this run.",
                "detail": note or f"Last activity {last_activity_label}; artifacts {artifact_count}.",
                "last_activity_age_sec": last_activity_age,
                "last_activity_label": last_activity_label,
            }

        if explicit_state == "blocked":
            note = str(explicit_status.get("note", "") or "").strip()
            return {
                "state": "blocked",
                "label": "Blocked",
                "summary": "Marked blocked explicitly for this run.",
                "detail": note or f"Last activity {last_activity_label}; artifacts {artifact_count}.",
                "last_activity_age_sec": last_activity_age,
                "last_activity_label": last_activity_label,
            }

        if not _runtime_status_is_active(runtime_status):
            status_label = str(runtime_status or "UNKNOWN").strip() or "UNKNOWN"
            return {
                "state": "stopped",
                "label": "Stopped",
                "summary": f"VM runtime is {status_label}.",
                "detail": "The instance is not currently in a running state.",
                "last_activity_age_sec": last_activity_age,
                "last_activity_label": last_activity_label,
            }

        if any(pattern.search(signal_text_lower) for pattern in HALTED_PATTERNS):
            return {
                "state": "halted",
                "label": "Halted",
                "summary": "The supervisor appears to have exited or fallen back to a shell.",
                "detail": f"Last activity {last_activity_label}. Review supervisor logs below.",
                "last_activity_age_sec": last_activity_age,
                "last_activity_label": last_activity_label,
            }

        if not snapshot.get("windows_list"):
            return {
                "state": "halted",
                "label": "Halted",
                "summary": "No tmux panes are available for this run.",
                "detail": "The VM is up, but the session output is unavailable.",
                "last_activity_age_sec": last_activity_age,
                "last_activity_label": last_activity_label,
            }

        if last_activity_age is not None and last_activity_age >= 900:
            return {
                "state": "stalled",
                "label": "Stalled",
                "summary": "No fresh findings, logs, or artifacts were updated in the last 15 minutes.",
                "detail": f"Last activity {last_activity_label}; artifacts {artifact_count}.",
                "last_activity_age_sec": last_activity_age,
                "last_activity_label": last_activity_label,
            }

        if last_activity_age is not None and last_activity_age <= 300:
            detail = f"Last activity {last_activity_label}; artifacts {artifact_count}."
        else:
            detail = f"Last visible activity {last_activity_label}; artifacts {artifact_count}."
        return {
            "state": "progressing",
            "label": "In Progress",
            "summary": "The VM is running and the challenge workspace is still changing.",
            "detail": detail,
            "last_activity_age_sec": last_activity_age,
            "last_activity_label": last_activity_label,
        }

    def _parse_metrics_block(self, block):
        metrics = {}
        for line in str(block or "").splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            metrics[key.strip()] = value.strip()
        return metrics

    def get_overview(self, force=False):
        runs, current_run_id = self.list_runs(
            include_status=True,
            force_status=force,
            force_discovery=force,
            only_live=True,
        )
        return {
            "runs": runs,
            "current_run_id": current_run_id,
            "spawn_defaults": self.get_spawn_defaults(),
            "spawn_jobs": self.list_spawn_jobs(),
        }

    def get_snapshot(self, run_id=None, include_artifacts=True, include_overview=True):
        if include_overview:
            runs, current_run_id = self.list_runs(
                include_status=True,
                force_status=True,
                force_discovery=True,
                only_live=True,
            )
        else:
            runs, current_run_id = self.list_runs(include_status=False)
        requested = str(run_id or "").strip()
        if not requested:
            requested = str(current_run_id or "").strip()
        base_payload = {
            "runs": runs,
            "current_run_id": current_run_id,
            "selected_run_id": requested,
        }
        if include_overview:
            base_payload["spawn_defaults"] = self.get_spawn_defaults()
            base_payload["spawn_jobs"] = self.list_spawn_jobs()
        if not requested:
            return {
                **base_payload,
                "error": "No run selected. Choose a run from the fleet list or start a new VM.",
                "instance": "",
                "zone": "",
                "project": "",
                "ip": "",
                "windows": "",
                "windows_list": [],
                "findings_tail": "",
                "artifacts": "",
                "status": "",
                "supervisor_tail": "",
            "metrics": {},
            "explicit_status": {},
            "challenge_state": None,
        }

        state = self.resolve_state(requested)
        if not state:
            return {
                **base_payload,
                "error": f"Run not found: {requested}",
                "challenge_state": None,
            }

        runtime_status = self.get_status_cached(state, force=False) if include_overview else ""
        runs = self._ensure_selected_run_present(runs, state, runtime_status=runtime_status if include_overview else None)
        base_payload["runs"] = runs
        snapshot = {
            **base_payload,
            "provider": provider_from_state(state),
            "run_id": state.get("run_id", ""),
            "instance": state.get("instance", ""),
            "zone": state.get("zone", ""),
            "project": state.get("project", ""),
            "ip": state.get("ip", ""),
            "windows": "",
            "windows_list": [],
            "findings_tail": "",
            "artifacts": "",
            "status": runtime_status,
            "supervisor_tail": "",
            "metrics": {},
            "explicit_status": {},
            "challenge_state": None,
        }

        artifact_cmd = 'find "${RUN_DIR}/artifacts" -maxdepth 3 -type f 2>/dev/null | sed "s#^${RUN_DIR}/##" | sort || true'
        if not include_artifacts:
            artifact_cmd = "true"
        remote = f"""sudo -u ctf bash -lc '
RUN_DIR=/home/ctf/run
if ! tmux list-sessions >/dev/null 2>&1; then
  echo "__NO_TMUX__"
  exit 0
fi
echo "__WINDOWS_BEGIN__"
tmux list-sessions -F "#{{session_name}}" 2>/dev/null | while IFS= read -r sess; do
  [ -z "$sess" ] && continue
  tmux list-windows -t "$sess" -F "#{{window_index}}|#{{window_name}}|#{{window_active}}" 2>/dev/null | while IFS="|" read -r idx wname wactive; do
    printf "%s|%s|%s|%s\\n" "$sess" "$idx" "$wname" "$wactive"
  done
done
echo "__WINDOWS_END__"
tmux list-sessions -F "#{{session_name}}" 2>/dev/null | while IFS= read -r sess; do
  [ -z "$sess" ] && continue
  tmux list-windows -t "$sess" -F "#{{window_index}}" 2>/dev/null | while IFS= read -r idx; do
    [ -z "$idx" ] && continue
    printf "__PANE_BEGIN__%s|%s\\n" "$sess" "$idx"
    tmux capture-pane -p -t "${{sess}}:${{idx}}" -S -120 2>/dev/null | tail -c {MAX_PANE_TAIL_BYTES} || echo "window ${{sess}}:${{idx}} not found"
    printf "__PANE_END__%s|%s\\n" "$sess" "$idx"
done
done
echo "__FINDINGS__"
tail -c {MAX_FINDINGS_TAIL_BYTES} "${{RUN_DIR}}/findings.md" 2>/dev/null || true
echo "__ART__"
{artifact_cmd}
echo "__SUPERVISOR__"
tail -c {MAX_SUPERVISOR_TAIL_BYTES} "${{RUN_DIR}}/logs/supervisor.log" 2>/dev/null || true
echo "__STATUS__"
cat "${{RUN_DIR}}/ui-status.json" 2>/dev/null || true
echo "__METRICS__"
now="$(date +%s)"
findings_mtime="$(stat -c %Y "${{RUN_DIR}}/findings.md" 2>/dev/null || echo 0)"
inject_mtime="$(stat -c %Y "${{RUN_DIR}}/inject.queue" 2>/dev/null || echo 0)"
supervisor_mtime="$(stat -c %Y "${{RUN_DIR}}/logs/supervisor.log" 2>/dev/null || echo 0)"
artifact_mtime="$(find "${{RUN_DIR}}/artifacts" -type f -printf "%T@\\n" 2>/dev/null | sort -nr | head -n1 | cut -d. -f1)"
artifact_count="$(find "${{RUN_DIR}}/artifacts" -type f 2>/dev/null | wc -l | tr -d " ")"
printf "now=%s\\n" "${{now}}"
printf "findings_mtime=%s\\n" "${{findings_mtime:-0}}"
printf "inject_mtime=%s\\n" "${{inject_mtime:-0}}"
printf "supervisor_mtime=%s\\n" "${{supervisor_mtime:-0}}"
printf "artifact_mtime=%s\\n" "${{artifact_mtime:-0}}"
printf "artifact_count=%s\\n" "${{artifact_count:-0}}"
'"""
        rc, out, err = self.ssh_cmd(state, remote, timeout=25)
        if rc != 0:
            snapshot["error"] = (err or out or "Failed to query VM").strip()
            snapshot["challenge_state"] = self.derive_challenge_state(snapshot)
            return snapshot

        if "__NO_TMUX__" in out:
            snapshot["error"] = "No tmux sessions found in VM."
            snapshot["challenge_state"] = self.derive_challenge_state(snapshot)
            return snapshot

        def slice_between(text, start_marker, end_marker):
            start = text.find(start_marker)
            if start == -1:
                return ""
            start += len(start_marker)
            end = text.find(end_marker, start)
            if end == -1:
                end = len(text)
            return text[start:end].strip()

        windows_block = slice_between(out, "__WINDOWS_BEGIN__", "__WINDOWS_END__")
        if not windows_block:
            snapshot["error"] = "Unexpected monitor output format."
            snapshot["challenge_state"] = self.derive_challenge_state(snapshot)
            return snapshot

        windows_list = []
        for line in windows_block.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("|", 3)
            if len(parts) < 4:
                continue
            session = parts[0].strip()
            index = parts[1].strip()
            name = parts[2].strip()
            active = parts[3].strip() == "1"
            output = slice_between(out, f"__PANE_BEGIN__{session}|{index}", f"__PANE_END__{session}|{index}")
            windows_list.append(
                {
                    "session": session,
                    "index": index,
                    "name": name,
                    "active": active,
                    "target": f"{session}:{index}",
                    "output": output,
                }
            )

        snapshot["windows_list"] = windows_list
        snapshot["windows"] = "\n".join(
            f"{window['target']}: {window['name']}{' *' if window['active'] else ''}"
            for window in windows_list
        )
        if windows_list and not _runtime_status_is_active(snapshot.get("status", "")):
            snapshot["status"] = "active"
        snapshot["findings_tail"] = slice_between(out, "__FINDINGS__", "__ART__")
        snapshot["artifacts"] = slice_between(out, "__ART__", "__SUPERVISOR__")
        snapshot["supervisor_tail"] = slice_between(out, "__SUPERVISOR__", "__STATUS__")
        status_block = slice_between(out, "__STATUS__", "__METRICS__")
        try:
            parsed_status = json.loads(status_block) if status_block else {}
        except Exception:
            parsed_status = {}
        snapshot["explicit_status"] = parsed_status if isinstance(parsed_status, dict) else {}
        metrics_block = out.split("__METRICS__", 1)[1].strip() if "__METRICS__" in out else ""
        snapshot["metrics"] = self._parse_metrics_block(metrics_block)
        snapshot["challenge_state"] = self.derive_challenge_state(snapshot)
        return snapshot

    def get_run_detail(self, run_id=None):
        return self.get_snapshot(run_id=run_id, include_artifacts=False, include_overview=False)

    def get_artifacts_index(self, run_id=None):
        runs, current_run_id = self.list_runs(include_status=False)
        requested = str(run_id or "").strip()
        if not requested:
            requested = str(current_run_id or "").strip()
        base_payload = {
            "runs": runs,
            "current_run_id": current_run_id,
            "selected_run_id": requested,
            "artifacts": "",
            "metrics": {},
            "status": "",
            "provider": "",
            "instance": "",
            "zone": "",
            "project": "",
            "ip": "",
        }
        if not requested:
            return {
                **base_payload,
                "error": "No run selected. Choose a run from the fleet list or start a new VM.",
            }

        state = self.resolve_state(requested)
        if not state:
            return {
                **base_payload,
                "error": f"Run not found: {requested}",
            }

        runs = self._ensure_selected_run_present(runs, state)
        base_payload["runs"] = runs

        payload = {
            **base_payload,
            "provider": provider_from_state(state),
            "instance": state.get("instance", ""),
            "zone": state.get("zone", ""),
            "project": state.get("project", ""),
            "ip": state.get("ip", ""),
            "status": self.get_status_cached(state, force=False),
        }

        remote = """sudo -u ctf bash -lc '
RUN_DIR=/home/ctf/run
echo "__ART__"
find "${RUN_DIR}/artifacts" -maxdepth 3 -type f 2>/dev/null | sed "s#^${RUN_DIR}/##" | sort || true
echo "__METRICS__"
now="$(date +%s)"
artifact_mtime="$(find "${RUN_DIR}/artifacts" -type f -printf "%T@\\n" 2>/dev/null | sort -nr | head -n1 | cut -d. -f1)"
artifact_count="$(find "${RUN_DIR}/artifacts" -type f 2>/dev/null | wc -l | tr -d " ")"
printf "now=%s\\n" "${now}"
printf "artifact_mtime=%s\\n" "${artifact_mtime:-0}"
printf "artifact_count=%s\\n" "${artifact_count:-0}"
'"""
        rc, out, err = self.ssh_cmd(state, remote, timeout=20)
        if rc != 0:
            payload["error"] = (err or out or "Failed to query artifacts").strip()
            return payload

        art_block = ""
        metrics_block = ""
        if "__ART__" in out:
            after_art = out.split("__ART__", 1)[1]
            if "__METRICS__" in after_art:
                art_block, metrics_block = after_art.split("__METRICS__", 1)
            else:
                art_block = after_art
        payload["artifacts"] = art_block.strip()
        payload["metrics"] = self._parse_metrics_block(metrics_block)
        return payload

    def _remote_artifact_path(self, relpath):
        clean = sanitize_artifact_relpath(relpath)
        if not clean:
            return None
        encoded_path = base64.b64encode(clean.encode("utf-8")).decode("ascii")
        return clean, encoded_path

    def get_artifact_preview(self, relpath, run_id=None, max_bytes=MAX_ARTIFACT_PREVIEW_BYTES):
        state = self.resolve_state(run_id)
        if not state:
            return {"ok": False, "error": "No active run. Start one with ./scripts/ctfvm start ..."}

        artifact = self._remote_artifact_path(relpath)
        if not artifact:
            return {"ok": False, "error": "Invalid artifact path"}
        clean, encoded_path = artifact

        remote = (
            "sudo -u ctf bash -lc '"
            f"rel=$(printf %s {encoded_path} | base64 -d); "
            'full="/home/ctf/run/${rel}"; '
            'if [ ! -f "$full" ]; then echo "__ERR__not found"; exit 0; fi; '
            'mime=$(file -b --mime-type "$full" 2>/dev/null || echo application/octet-stream); '
            'size=$(wc -c < "$full" | tr -d " "); '
            'echo "__META__${mime}|${size}"; '
            f'dd if="$full" bs=1 count={max_bytes} 2>/dev/null | base64; '
            f'if [ "$size" -gt {max_bytes} ]; then echo "__TRUNC__1"; else echo "__TRUNC__0"; fi'
            "'"
        )
        rc, out, err = self.ssh_cmd(state, remote, timeout=25)
        if rc != 0:
            return {"ok": False, "error": (err or out or "Failed to fetch artifact").strip()}
        if out.strip().startswith("__ERR__"):
            return {"ok": False, "error": "Artifact not found"}

        lines = out.splitlines()
        if not lines or not lines[0].startswith("__META__"):
            return {"ok": False, "error": "Unexpected artifact preview format"}

        meta = lines[0][len("__META__") :]
        mime = "application/octet-stream"
        size = 0
        if "|" in meta:
            mime, size_text = meta.split("|", 1)
            try:
                size = int(size_text)
            except ValueError:
                size = 0

        trunc_line = "__TRUNC__0"
        if lines:
            last = lines[-1].strip()
            if last.startswith("__TRUNC__"):
                trunc_line = last
                lines = lines[1:-1]
            else:
                lines = lines[1:]
        truncated = trunc_line == "__TRUNC__1"

        payload_b64 = "".join(lines).strip()
        raw = b""
        if payload_b64:
            try:
                raw = base64.b64decode(payload_b64, validate=False)
            except Exception:
                raw = b""

        guessed, _ = mimetypes.guess_type(clean)
        effective_mime = mime if mime and mime != "application/octet-stream" else (guessed or mime)
        text_like = effective_mime.startswith("text/") or effective_mime in {
            "application/json",
            "application/xml",
            "application/x-sh",
            "application/javascript",
        }
        is_image = effective_mime.startswith("image/")
        image_data_url = ""
        if is_image and raw:
            image_data_url = f"data:{effective_mime};base64,{base64.b64encode(raw).decode('ascii')}"

        if text_like:
            content = raw.decode("utf-8", errors="replace")
        else:
            content = f"[binary file: {effective_mime}, {size} bytes]"

        return {
            "ok": True,
            "path": clean,
            "mime": effective_mime,
            "size": size,
            "truncated": truncated,
            "is_text": text_like,
            "is_image": is_image,
            "image_data_url": image_data_url,
            "content": content,
        }

    def get_artifact_download(self, relpath, run_id=None):
        state = self.resolve_state(run_id)
        if not state:
            return {"ok": False, "error": "No run selected"}

        artifact = self._remote_artifact_path(relpath)
        if not artifact:
            return {"ok": False, "error": "Invalid artifact path"}
        clean, encoded_path = artifact

        remote = (
            "sudo -u ctf bash -lc '"
            f"rel=$(printf %s {encoded_path} | base64 -d); "
            'full="/home/ctf/run/${rel}"; '
            'if [ ! -f "$full" ]; then echo "__ERR__not found"; exit 0; fi; '
            'mime=$(file -b --mime-type "$full" 2>/dev/null || echo application/octet-stream); '
            'size=$(wc -c < "$full" | tr -d " "); '
            'echo "__META__${mime}|${size}"; '
            'base64 "$full"'
            "'"
        )
        rc, out, err = self.ssh_cmd(state, remote, timeout=45)
        if rc != 0:
            return {"ok": False, "error": (err or out or "Failed to download artifact").strip()}
        if out.strip().startswith("__ERR__"):
            return {"ok": False, "error": "Artifact not found"}

        lines = out.splitlines()
        if not lines or not lines[0].startswith("__META__"):
            return {"ok": False, "error": "Unexpected artifact download format"}

        meta = lines[0][len("__META__") :]
        mime = "application/octet-stream"
        if "|" in meta:
            mime = meta.split("|", 1)[0].strip() or mime
        payload = "".join(lines[1:]).strip()
        try:
            content = base64.b64decode(payload, validate=False)
        except Exception:
            content = b""
        guessed, _ = mimetypes.guess_type(clean)
        effective_mime = mime if mime and mime != "application/octet-stream" else (guessed or mime)
        return {
            "ok": True,
            "content": content,
            "mime": effective_mime,
            "filename": Path(clean).name,
        }

    def get_run_bundle_download(self, run_id=None):
        state, err = self.state_for_run_or_error(run_id)
        if err:
            return err
        run_id_value = str((state or {}).get("run_id", "") or "run")
        remote = (
            "sudo -u ctf bash -lc '"
            'tmp=$(mktemp /tmp/ctfvm-bundle.XXXXXX.tar.gz); '
            'cd /home/ctf/run || exit 1; '
            'tar -czf "$tmp" findings.md artifacts logs challenge_prompt.txt 2>/dev/null '
            '|| tar -czf "$tmp" artifacts logs challenge_prompt.txt; '
            'mime=application/gzip; '
            'size=$(wc -c < "$tmp" | tr -d " "); '
            f'if [ "$size" -gt {RUN_BUNDLE_MAX_BYTES} ]; then echo "__ERR__bundle too large"; rm -f "$tmp"; exit 0; fi; '
            'echo "__META__${mime}|${size}"; '
            'base64 "$tmp"; '
            'rm -f "$tmp"'
            "'"
        )
        rc, out, stderr = self.ssh_cmd(state, remote, timeout=60)
        if rc != 0:
            return {"ok": False, "error": (stderr or out or "bundle failed").strip()}
        if out.strip().startswith("__ERR__"):
            return {"ok": False, "error": out.strip()[len("__ERR__") :] or "bundle failed"}
        lines = out.splitlines()
        if not lines or not lines[0].startswith("__META__"):
            return {"ok": False, "error": "Unexpected bundle format"}
        payload = "".join(lines[1:]).strip()
        try:
            content = base64.b64decode(payload, validate=False)
        except Exception:
            content = b""
        return {
            "ok": True,
            "content": content,
            "mime": "application/gzip",
            "filename": f"ctfvm-{run_id_value}.tar.gz",
        }

    def state_for_run_or_error(self, run_id):
        requested = str(run_id or "").strip()
        if not requested:
            _, current_run_id = self.list_runs()
            requested = str(current_run_id or "").strip()
        if not requested:
            return None, {"ok": False, "error": "No run selected"}
        state = self.resolve_state(requested)
        if not state:
            return None, {"ok": False, "error": f"Run not found: {requested}"}
        return state, None

    def send_to_tmux(self, run_id, target, text, enter=True):
        state, err = self.state_for_run_or_error(run_id)
        if err:
            return err
        safe_target_value = safe_target(target)
        if not safe_target_value:
            return {"ok": False, "error": "Invalid tmux target"}
        safe_session = target_session(safe_target_value)
        if not text:
            return {"ok": False, "error": "text is required"}

        payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
        enter_cmd = f"sleep 0.08; tmux send-keys -t {shlex.quote(safe_target_value)} C-m;" if enter else ""
        remote = (
            "sudo -u ctf bash -lc '"
            f"tmux has-session -t {shlex.quote(safe_session)} >/dev/null 2>&1 || exit 1; "
            f'payload="$(printf %s {shlex.quote(payload)} | base64 -d)"; '
            f"tmux send-keys -t {shlex.quote(safe_target_value)} -l -- \"${{payload}}\"; "
            f"{enter_cmd}"
            "'"
        )
        rc, out, stderr = self.ssh_cmd(state, remote, timeout=20)
        if rc != 0:
            return {"ok": False, "error": (stderr or out or "send failed").strip()}
        return {"ok": True}

    def send_keys_tmux(self, run_id, target, keys):
        state, err = self.state_for_run_or_error(run_id)
        if err:
            return err
        safe_target_value = safe_target(target)
        if not safe_target_value:
            return {"ok": False, "error": "Invalid tmux target"}
        safe_session = target_session(safe_target_value)
        safe_keys = [key for key in keys if KEY_RE.match(key or "")]
        if not safe_keys:
            return {"ok": False, "error": "No valid keys provided"}
        keys_cmd = " ".join(shlex.quote(key) for key in safe_keys)
        remote = (
            "sudo -u ctf bash -lc '"
            f"tmux has-session -t {shlex.quote(safe_session)} >/dev/null 2>&1 || exit 1; "
            f"tmux send-keys -t {shlex.quote(safe_target_value)} {keys_cmd}'"
        )
        rc, out, stderr = self.ssh_cmd(state, remote, timeout=15)
        if rc != 0:
            return {"ok": False, "error": (stderr or out or "key failed").strip()}
        return {"ok": True}

    def trust_prompt(self, run_id, target="ctf:supervisor"):
        return self.send_keys_tmux(run_id, target, ["1", "Enter"])

    def set_explicit_status(self, run_id, state, note=""):
        state = _parse_explicit_state(state)
        if not state:
            return {"ok": False, "error": "Invalid explicit status"}
        state_obj, err = self.state_for_run_or_error(run_id)
        if err:
            return err
        payload = {"state": state, "updated_at": _utc_now_iso()}
        note = str(note or "").strip()
        if note:
            payload["note"] = note
        payload_json = json.dumps(payload, separators=(",", ":"))
        encoded = base64.b64encode(payload_json.encode("utf-8")).decode("ascii")
        remote = (
            "sudo -u ctf bash -lc '"
            f'printf %s {shlex.quote(encoded)} | base64 -d > /home/ctf/run/ui-status.json'
            "'"
        )
        rc, out, stderr = self.ssh_cmd(state_obj, remote, timeout=15)
        if rc != 0:
            return {"ok": False, "error": (stderr or out or "status update failed").strip()}
        return {"ok": True}

    def clear_explicit_status(self, run_id):
        state_obj, err = self.state_for_run_or_error(run_id)
        if err:
            return err
        remote = "sudo -u ctf bash -lc 'rm -f /home/ctf/run/ui-status.json'"
        rc, out, stderr = self.ssh_cmd(state_obj, remote, timeout=15)
        if rc != 0:
            return {"ok": False, "error": (stderr or out or "status clear failed").strip()}
        return {"ok": True}

    def _compose_spawn_description(self, desc, flag_format):
        chunks = []
        base_desc = str(desc or "").strip()
        if base_desc:
            chunks.append(base_desc)
        flag_format = str(flag_format or "").strip()
        if flag_format:
            chunks.append(f"Expected flag format: {flag_format}")
        return "\n\n".join(chunks).strip()

    def _validate_challenge_dir(self, raw_path):
        path_text = str(raw_path or "").strip()
        if not path_text:
            return None, "challenge_dir is required"
        path = Path(path_text).expanduser()
        if not path.is_absolute():
            path = (ROOT / path).resolve()
        if not path.exists():
            return None, f"Challenge directory not found: {path}"
        if not path.is_dir():
            return None, f"Challenge path is not a directory: {path}"
        return path, None

    def _discover_batch_challenges(self, raw_root):
        root_dir, err = self._validate_challenge_dir(raw_root)
        if err:
            return None, err
        entries = []
        for child in sorted(root_dir.iterdir(), key=lambda p: p.name.lower()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            entries.append(
                {
                    "challenge_dir": child,
                    "desc": _read_optional_text(child / "description.txt"),
                    "ideas": _read_optional_text(child / "ideas.txt"),
                    "name": child.name,
                }
            )
        if not entries:
            return None, f"No challenge subdirectories found under: {root_dir}"
        return entries, None

    def _build_spawn_command_for_entry(self, payload, challenge_dir, desc="", ideas=""):
        provider = normalize_provider(payload.get("provider") or self.get_spawn_defaults().get("provider") or "gcp")
        self.providers.get(provider)
        challenge_dir_path, err = self._validate_challenge_dir(challenge_dir)
        if err:
            return None, err, None

        desc_text = str(desc or payload.get("desc", "") or "").strip()
        ideas_text = str(ideas or payload.get("ideas", "") or "").strip()
        desc = self._compose_spawn_description(desc_text, payload.get("flag_format"))
        timeout_min = str(payload.get("timeout_min") or self.get_spawn_defaults().get("timeout_min") or "").strip()
        toolbox_variant = str(payload.get("toolbox_variant") or self.get_spawn_defaults().get("toolbox_variant") or "lean").strip()
        zone = str(payload.get("zone", "") or "").strip()
        project = str(payload.get("project", "") or "").strip()
        machine_type = str(payload.get("machine_type", "") or "").strip()
        size_slug = str(payload.get("size_slug", "") or "").strip()
        use_local_image = bool(payload.get("use_local_image"))
        no_auth_sync = bool(payload.get("no_auth_sync"))

        cmd = [str(ROOT / "scripts" / "ctfvm"), "start", "--provider", provider, "--dir", str(challenge_dir_path)]
        if desc:
            cmd += ["--desc", desc]
        if ideas_text:
            cmd += ["--ideas", ideas_text]
        if timeout_min:
            cmd += ["--timeout-min", timeout_min]
        if toolbox_variant in {"lean", "full"}:
            cmd += ["--toolbox-variant", toolbox_variant]
        if zone:
            cmd += ["--zone", zone]
        if project:
            cmd += ["--project", project]
        if use_local_image:
            cmd.append("--use-local-image")
        if no_auth_sync:
            cmd.append("--no-auth-sync")
        if provider == "gcp" and machine_type:
            cmd += ["--machine-type", machine_type]
        if provider == "digitalocean" and size_slug:
            cmd += ["--size-slug", size_slug]

        summary = {
            "provider": provider,
            "challenge_dir": str(challenge_dir_path),
            "challenge_name": challenge_dir_path.name,
            "zone": zone,
            "project": project,
            "machine_type": machine_type if provider == "gcp" else "",
            "size_slug": size_slug if provider == "digitalocean" else "",
            "toolbox_variant": toolbox_variant,
            "use_local_image": use_local_image,
            "flag_format": str(payload.get("flag_format", "") or "").strip(),
            "batch_mode": bool(payload.get("batch_mode")),
        }
        return cmd, None, summary

    def _append_spawn_output(self, job, text):
        combined = (job.get("output", "") or "") + str(text or "")
        if len(combined) > MAX_SPAWN_OUTPUT_BYTES:
            combined = combined[-MAX_SPAWN_OUTPUT_BYTES:]
        job["output"] = combined

    def _extract_run_id_from_output(self, text):
        match = re.search(r"--run-id\s+(\d{8}-\d{6})", str(text or ""))
        if match:
            return match.group(1)
        match = re.search(r"ctfvm-[A-Za-z0-9-]+-(\d{8}-\d{6})", str(text or ""))
        if match:
            return match.group(1)
        return ""

    def _extract_instance_from_output(self, text):
        match = re.search(r"\b(ctfvm-[A-Za-z0-9-]+-\d{8}-\d{6})\b", str(text or ""))
        if match:
            return match.group(1)
        return ""

    def _job_run_selector(self, job):
        summary = dict((job or {}).get("summary", {}) or {})
        instance = str((job or {}).get("instance", "") or "").strip()
        if not instance:
            return ""
        return _run_selector(
            {
                "provider": summary.get("provider", ""),
                "project": summary.get("project", ""),
                "zone": summary.get("zone", ""),
                "instance": instance,
                "run_id": (job or {}).get("run_id", ""),
            }
        )

    def _spawn_worker(self, job_id, proc):
        try:
            if proc.stdout:
                for line in proc.stdout:
                    with self._spawn_lock:
                        job = self._spawn_jobs.get(job_id)
                        if not job:
                            continue
                        self._append_spawn_output(job, line)
                        run_id = self._extract_run_id_from_output(job.get("output", ""))
                        if run_id and not job.get("run_id"):
                            job["run_id"] = run_id
                        instance = self._extract_instance_from_output(job.get("output", ""))
                        if instance and not job.get("instance"):
                            job["instance"] = instance
                        selector = self._job_run_selector(job)
                        if selector:
                            job["run_selector"] = selector
            proc.wait()
        finally:
            rc = proc.returncode if proc.returncode is not None else 1
            with self._spawn_lock:
                job = self._spawn_jobs.get(job_id)
                if job:
                    run_id = self._extract_run_id_from_output(job.get("output", ""))
                    if run_id:
                        job["run_id"] = run_id
                    instance = self._extract_instance_from_output(job.get("output", ""))
                    if instance:
                        job["instance"] = instance
                    selector = self._job_run_selector(job)
                    if selector:
                        job["run_selector"] = selector
                    job["status"] = "succeeded" if rc == 0 else "failed"
                    job["return_code"] = rc
                    job["finished_at"] = _utc_now_iso()
            self.discover_runs(force=True)

    def _start_spawn_job(self, cmd, summary):
        job_id = uuid.uuid4().hex[:10]
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                cwd=str(ROOT),
                env=os.environ.copy(),
                bufsize=1,
            )
        except Exception as exc:
            return {"ok": False, "error": f"Failed to start VM job: {exc}"}

        job = {
            "job_id": job_id,
            "status": "running",
            "started_at": _utc_now_iso(),
            "finished_at": "",
            "return_code": None,
            "run_id": "",
            "run_selector": "",
            "instance": "",
            "summary": summary,
            "output": "",
        }
        with self._spawn_lock:
            self._spawn_jobs[job_id] = job
            self._spawn_job_order.insert(0, job_id)
            while len(self._spawn_job_order) > MAX_SPAWN_JOBS:
                stale_id = self._spawn_job_order.pop()
                self._spawn_jobs.pop(stale_id, None)

        thread = threading.Thread(target=self._spawn_worker, args=(job_id, proc), daemon=True)
        thread.start()
        return job_id

    def start_run(self, payload):
        payload = dict(payload or {})
        batch_mode = bool(payload.get("batch_mode"))
        if batch_mode:
            entries, err = self._discover_batch_challenges(payload.get("challenge_dir"))
            if err:
                return {"ok": False, "error": err}
            job_ids = []
            for entry in entries:
                cmd, cmd_err, summary = self._build_spawn_command_for_entry(
                    payload,
                    entry["challenge_dir"],
                    desc=entry.get("desc", ""),
                    ideas=entry.get("ideas", ""),
                )
                if cmd_err:
                    return {"ok": False, "error": cmd_err}
                job_ids.append(self._start_spawn_job(cmd, summary))
            return {
                "ok": True,
                "mode": "batch",
                "job_ids": job_ids,
                "count": len(job_ids),
            }

        cmd, err, summary = self._build_spawn_command_for_entry(
            payload,
            payload.get("challenge_dir"),
            desc=payload.get("desc", ""),
            ideas=payload.get("ideas", ""),
        )
        if err:
            return {"ok": False, "error": err}
        job_id = self._start_spawn_job(cmd, summary)
        return {"ok": True, "mode": "single", "job_id": job_id, "count": 1}

    def list_spawn_jobs(self):
        with self._spawn_lock:
            jobs = []
            for job_id in self._spawn_job_order:
                job = self._spawn_jobs.get(job_id)
                if not job:
                    continue
                jobs.append(
                    {
                        "job_id": job["job_id"],
                        "status": job["status"],
                        "started_at": job["started_at"],
                        "finished_at": job.get("finished_at", ""),
                        "return_code": job.get("return_code"),
                        "run_id": job.get("run_id", ""),
                        "run_selector": job.get("run_selector", ""),
                        "instance": job.get("instance", ""),
                        "summary": dict(job.get("summary", {})),
                        "output": job.get("output", ""),
                    }
                )
            return jobs


def sanitize_artifact_relpath(relpath):
    relpath = (relpath or "").strip().replace("\\", "/")
    if not relpath:
        return None
    if relpath.startswith("/") or relpath.startswith("~"):
        return None
    parts = [part for part in relpath.split("/") if part and part != "."]
    if any(part == ".." for part in parts):
        return None
    clean = "/".join(parts)
    if not clean.startswith("artifacts/"):
        return None
    return clean


TARGET_RE = re.compile(r"^[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+$")
KEY_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def safe_target(target):
    target = (target or "ctf:supervisor").strip() or "ctf:supervisor"
    if not TARGET_RE.match(target):
        return None
    return target


def target_session(target):
    if ":" not in target:
        return "ctf"
    return target.split(":", 1)[0]


SERVICE = CTFVMUIService(ProviderRegistry([GcpProviderBackend(), DigitalOceanProviderBackend()]))
