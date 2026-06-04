"""UiService — composes the core for the HTTP layer.

Thin orchestration over RunRegistry (state), the CloudProvider layer (status),
the typed control client (transport), and the snapshot/steering/artifacts
modules. Raises ``ApiError`` with a stable code; the app maps it to the error
envelope. No HTTP concerns here.
"""

from __future__ import annotations

import time

import json as _json
import os
import shutil
import subprocess
from pathlib import Path

from .. import artifacts as artifacts_mod
from .. import snapshot as snapshot_mod
from .. import steering as steering_mod
from .. import transcript as transcript_mod
from ..agents import build_agent_backend
from ..artifacts import ArtifactError
from ..commands import agents_info, discover_challenges
from ..config import ROOT, Settings
from ..controlclient import ControlPlaneClient, ControlPlaneError
from ..models import RunRecord, Subagent
from ..providers import build_provider_registry, build_run_registry
from ..remote import remote_python
from .jobs import JobLimitError, SpawnJobTracker


class ApiError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class UiService:
    def __init__(self, *, registry=None, providers=None, jobs=None, client_factory=None):
        self.providers = providers or build_provider_registry()
        self.registry = registry or build_run_registry(self.providers)
        self.jobs = jobs or SpawnJobTracker(now=lambda: _iso_now())
        # injectable so tests can supply a fake control client
        self._client_factory = client_factory or ControlPlaneClient.from_run
        self._started = time.time()

    # --- health / runs -----------------------------------------------------
    def health(self) -> dict:
        return {
            "uptime_sec": int(time.time() - self._started),
            "known_runs": len(self.registry.list_runs()[0]),
            "active_jobs": self.jobs.active_count(),
        }

    def list_runs(self, *, include_status=False, force_discovery=False, only_live=False):
        return self.registry.list_runs(
            include_status=include_status, force_status=include_status,
            force_discovery=force_discovery, only_live=only_live,
        )

    def _record(self, run_id: str) -> RunRecord:
        record = self.registry.resolve(run_id)
        if not record:
            raise ApiError("NOT_FOUND", f"Run not found: {run_id}", 404)
        return record

    def _client(self, record: RunRecord) -> ControlPlaneClient:
        try:
            return self._client_factory(record)
        except ControlPlaneError as exc:
            raise ApiError("REMOTE_ERROR", str(exc), 502) from exc

    # --- snapshot ----------------------------------------------------------
    def snapshot(self, run_id: str, *, include_artifacts=True, force_status=False):
        record = self._record(run_id)
        runtime_status = self.registry.get_status_cached(record, force=force_status)
        try:
            client = self._client_factory(record)
        except ControlPlaneError as exc:
            # run exists but no control plane -> partial snapshot with error (ok:true)
            snap = snapshot_mod._error_snapshot(record, runtime_status, str(exc))
            return snap
        snap = snapshot_mod.fetch_snapshot(
            client, record, include_artifacts=include_artifacts, runtime_status=runtime_status,
        )
        snap.subagents = self._subagents_from_snapshot(snap)
        return snap

    @staticmethod
    def _subagents_from_snapshot(snap) -> list[Subagent]:
        seen: dict[str, Subagent] = {}
        for p in snap.panes:
            if p.session.startswith("subagent-") and p.session not in seen:
                seen[p.session] = Subagent(
                    id=p.session[len("subagent-"):],
                    tmux_target=p.target,
                    kind="",
                    label=p.window_name,
                    state="running",
                    steerable=True,
                )
        return list(seen.values())

    def subagents(self, run_id: str) -> list[Subagent]:
        return self.snapshot(run_id, include_artifacts=False).subagents

    def transcript(self, run_id: str) -> tuple[str, list]:
        """Fetch + parse the agent's session transcript into chat events
        (control-plane only). Returns (backend_name, events)."""
        record = self._record(run_id)
        backend = build_agent_backend(record.agent_backend)
        client = self._client(record)
        try:
            result = client.exec(remote_python(backend.transcript_script(record.remote_run_dir)), timeout=25)
        except ControlPlaneError as exc:
            raise ApiError("REMOTE_ERROR", str(exc), 502) from exc
        if not result.ok:
            raise ApiError("REMOTE_ERROR", result.stderr_text().strip() or "transcript fetch failed", 502)
        try:
            payload = _json.loads(result.stdout.decode("utf-8") or "{}")
        except Exception:
            payload = {}
        import base64 as _b64
        text = _b64.b64decode((payload.get("b64") or "").encode("ascii"), validate=False).decode("utf-8", "replace")
        return backend.name, transcript_mod.parse_transcript(backend.name, text)

    # --- steering / status -------------------------------------------------
    def pane_send(self, run_id, target, text, enter=True):
        self._steer(lambda c: steering_mod.send_text(c, target, text, enter=enter), run_id)

    def pane_keys(self, run_id, target, keys):
        self._steer(lambda c: steering_mod.send_keys(c, target, keys), run_id)

    def pane_trust(self, run_id, target="ctf:supervisor"):
        self._steer(lambda c: steering_mod.trust_prompt(c, target), run_id)

    def set_status(self, run_id, state, note=""):
        self._steer(lambda c: steering_mod.set_explicit_status(c, state, note), run_id)

    def clear_status(self, run_id):
        self._steer(lambda c: steering_mod.clear_explicit_status(c), run_id)

    def _steer(self, fn, run_id):
        record = self._record(run_id)
        client = self._client(record)
        try:
            fn(client)
        except steering_mod.SteeringError as exc:
            raise ApiError("BAD_REQUEST", str(exc), 400) from exc
        except ControlPlaneError as exc:
            raise ApiError("REMOTE_ERROR", str(exc), 502) from exc

    # --- artifacts ---------------------------------------------------------
    def artifact_preview(self, run_id, relpath):
        record = self._record(run_id)
        client = self._client(record)
        return self._artifact(lambda: artifacts_mod.preview_artifact(client, record.remote_run_dir, relpath))

    def artifact_download(self, run_id, relpath):
        record = self._record(run_id)
        client = self._client(record)
        return self._artifact(lambda: artifacts_mod.download_artifact(client, record.remote_run_dir, relpath))

    def bundle(self, run_id):
        record = self._record(run_id)
        client = self._client(record)
        return self._artifact(lambda: artifacts_mod.build_bundle(client, record.run_id))

    @staticmethod
    def _artifact(fn):
        try:
            return fn()
        except ArtifactError as exc:
            msg = str(exc)
            if "not found" in msg.lower():
                raise ApiError("NOT_FOUND", msg, 404) from exc
            if "too large" in msg.lower():
                raise ApiError("BAD_REQUEST", msg, 413) from exc
            raise ApiError("INVALID_PATH", msg, 400) from exc
        except ControlPlaneError as exc:
            raise ApiError("REMOTE_ERROR", str(exc), 502) from exc

    # --- spawn / jobs ------------------------------------------------------
    def agents(self) -> list[dict]:
        return agents_info()

    # form fields that can be pre-filled from configured defaults (.env / env /
    # config.json). schema-only defaults stay blank so the form shows the
    # built-in default as a placeholder rather than a redundant explicit value.
    _SPAWN_DEFAULT_KEYS = (
        "agent_backend", "model", "provider",
        "gcp_zone", "gcp_project", "gcp_machine_type",
        "do_region", "do_size_slug",
        "toolbox_variant", "timeout_min",
    )

    def spawn_defaults(self) -> dict:
        settings = Settings()
        out: dict[str, dict] = {}
        for key in self._SPAWN_DEFAULT_KEYS:
            value, source = settings.resolve(key)
            out[key] = {
                "value": "" if value is None else str(value),
                "source": source,
                "configured": source != "default",  # explicitly set on this instance
            }
        return out

    def profiles(self) -> list[dict]:
        """Named credential profiles (for the spawn form's account picker).
        Never returns secret values — just name + which backend they auth."""
        from ..secretstore import list_profiles
        out = []
        for name, data in sorted(list_profiles().items()):
            out.append({"name": name, "backend": str((data or {}).get("backend") or "")})
        return out

    def choose_directory(self, current_path: str = "", batch: bool = False) -> dict:
        """macOS Finder folder picker (local convenience). Degrades elsewhere:
        callers just type the path."""
        if os.uname().sysname.lower() != "darwin":
            raise ApiError("BAD_REQUEST", "Finder selection is only available on macOS; type the path instead.", 400)
        if shutil.which("osascript") is None:
            raise ApiError("BAD_REQUEST", "osascript is not available.", 400)
        prompt = "Select challenges root folder" if batch else "Select challenge folder"
        cmd = ["osascript"]
        for line in ("on run argv", "set p to item 1 of argv",
                     "set f to choose folder with prompt p", "return POSIX path of f", "end run"):
            cmd += ["-e", line]
        cmd.append(prompt)
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=300)
        except subprocess.TimeoutExpired as exc:
            raise ApiError("REMOTE_ERROR", "Finder selection timed out.", 504) from exc
        if proc.returncode != 0:
            text = (proc.stderr or proc.stdout or "").strip()
            if "user canceled" in text.lower():
                return {"canceled": True}
            raise ApiError("BAD_REQUEST", text or "Finder selection failed.", 400)
        selected = (proc.stdout or "").strip()
        if not selected:
            raise ApiError("BAD_REQUEST", "No folder selected.", 400)
        path = Path(selected).expanduser()
        desc = ""
        if not batch:
            try:
                desc = (path / "description.txt").read_text().strip()
            except Exception:
                desc = ""
        return {"path": str(path), "description": desc, "canceled": False}

    def spawn(self, payload: dict) -> list[str]:
        batch = payload.get("batch")
        if batch is not None and not isinstance(batch, list):
            raise ApiError("BAD_REQUEST", "batch must be a list", 400)
        specs = batch or [payload]
        expanded = self._expand_specs(specs)
        job_ids: list[str] = []
        for spec in expanded:
            cmd = self._build_start_cmd(spec)
            try:
                job = self.jobs.submit(cmd)
            except JobLimitError as exc:
                raise ApiError("JOB_LIMIT", str(exc), 429) from exc
            job_ids.append(job.job_id)
        return job_ids

    @staticmethod
    def _expand_specs(specs: list) -> list[dict]:
        """A spec with ``challenge_root`` fans out into one spec per immediate
        subfolder (recursive folder deploy). Others pass through unchanged."""
        out: list[dict] = []
        for spec in specs:
            if not isinstance(spec, dict):
                raise ApiError("BAD_REQUEST", "each spawn spec must be an object", 400)
            root = str(spec.get("challenge_root") or "").strip()
            if not root:
                out.append(spec)
                continue
            children = discover_challenges(root)
            if not children:
                raise ApiError("BAD_REQUEST", f"no challenge subfolders under {root}", 400)
            for ch in children:
                merged = {k: v for k, v in spec.items() if k != "challenge_root"}
                merged["challenge_dir"] = ch["challenge_dir"]
                if not merged.get("description"):
                    merged["description"] = ch["description"] or ch["name"]
                if not merged.get("ideas"):
                    merged["ideas"] = ch["ideas"]
                out.append(merged)
        return out

    @staticmethod
    def _build_start_cmd(spec: dict) -> list[str]:
        challenge_dir = str(spec.get("challenge_dir") or "").strip()
        if not challenge_dir:
            raise ApiError("BAD_REQUEST", "challenge_dir is required", 400)
        # fall back to configured defaults (CTFVM_AGENT / CTFVM_MODEL / …) so a
        # spawn that omits them honors .env rather than always using codex.
        settings = Settings()
        spec = dict(spec)
        for key in ("agent_backend", "model", "provider", "toolbox_variant", "timeout_min"):
            if not spec.get(key):
                resolved = settings.get(key)
                if resolved not in (None, ""):
                    spec[key] = resolved

        # flag format folds into the description (which becomes the agent prompt),
        # so the agent knows the shape of what it's hunting for.
        flag_format = str(spec.get("flag_format") or "").strip()
        if flag_format:
            desc = str(spec.get("description") or "").strip()
            spec["description"] = (f"{desc}\n\n" if desc else "") + f"Expected flag format: {flag_format}"

        cmd = [str(ROOT / "scripts" / "ctfvm"), "start", "--dir", challenge_dir]
        flag_map = {
            "provider": "--provider", "agent_backend": "--agent", "zone": "--zone",
            "project": "--project", "description": "--desc", "ideas": "--ideas",
            "model": "--model", "machine_type": "--machine-type", "size_slug": "--size-slug",
            "toolbox_variant": "--toolbox-variant", "timeout_min": "--timeout-min",
        }
        for key, flag in flag_map.items():
            val = spec.get(key)
            if val not in (None, ""):
                cmd += [flag, str(val)]
        # account/profile is a UI-only selection (not a Settings key)
        account = str(spec.get("account") or "").strip()
        if account:
            cmd += ["--account", account]
        if spec.get("no_vpn"):
            cmd.append("--no-vpn")
        if spec.get("use_local_image"):
            cmd.append("--use-local-image")
        return cmd

    def jobs_list(self):
        return self.jobs.list_all()

    def job_get(self, job_id):
        job = self.jobs.get(job_id)
        if not job:
            raise ApiError("NOT_FOUND", f"Job not found: {job_id}", 404)
        return job


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
