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
from ..commands import agents_info, confine_remote_path, discover_challenges
from ..config import MAX_SPAWN_JOBS, MAX_UPLOAD_BYTES, ROOT, Settings
from ..controlclient import ControlPlaneClient, ControlPlaneError
from ..models import (
    DEFAULT_AGENT_BACKEND,
    DEFAULT_REMOTE_RUN_DIR,
    RUNNER_CHALLENGE,
    RUNNER_WORKER,
    ChallengeState,
    ProvisioningJob,
    RunRecord,
    Subagent,
    UploadResult,
)
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
        # last-known challenge_state per run, so the sidebar (/runs) can render
        # every pill instantly without a control-plane round trip per run.
        self._cs_cache: dict[str, "ChallengeState"] = {}
        # run_ids handed out this process — to dedup across concurrent batches
        # whose state isn't persisted yet (see _assign_run_ids).
        self._assigned_run_ids: set[str] = set()

    # --- health / runs -----------------------------------------------------
    def health(self) -> dict:
        return {
            "uptime_sec": int(time.time() - self._started),
            "known_runs": len(self.registry.list_runs()[0]),
            "active_jobs": self.jobs.active_count(),
            "dns_proxy": self.dns_proxy_status(),
        }

    def dns_proxy_status(self) -> dict:
        """For each VPN tunnel that started a DNS forwarder (a ``*-dns.pid`` next
        to the tunnel state), is the forwarder process still alive? Powers the
        top-bar indicator so a crashed/killed proxy is obvious. ``expected`` is
        how many tunnels should have a proxy; ``running`` how many do."""
        expected = running = 0
        vpn_dir = ROOT / ".ctfvm" / "vpn"
        if vpn_dir.is_dir():
            for sub in vpn_dir.iterdir():
                if not sub.is_dir() or not any(sub.glob("*.json")):
                    continue  # only count dirs that still have a live tunnel state
                for pid_f in sub.glob("*-dns.pid"):
                    expected += 1
                    try:
                        os.kill(int(pid_f.read_text().strip()), 0)
                        running += 1
                    except ProcessLookupError:
                        pass  # dead
                    except PermissionError:
                        running += 1  # alive but root-owned (forwarder runs via sudo)
                    except (ValueError, OSError):
                        pass
        return {"expected": expected, "running": running}

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
        # A hosted challenge shares the worker's VM — use the worker's runtime
        # status, not a dead lookup of the challenge's synthetic instance.
        if record.parent_worker_id:
            worker = self.registry.resolve(record.parent_worker_id)
            runtime_status = self.registry.get_status_cached(worker, force=force_status) if worker else "active"
        else:
            runtime_status = self.registry.get_status_cached(record, force=force_status)
        try:
            client = self._client_factory(record)
        except ControlPlaneError as exc:
            # run exists but no control plane -> partial snapshot with error (ok:true).
            # If a spawn job is still provisioning this run, surface its output
            # (e.g. "Waiting for VM control plane availability…") so the UI shows
            # progress rather than a bare credentials error.
            snap = snapshot_mod._error_snapshot(record, runtime_status, str(exc))
            snap.provisioning = [
                ProvisioningJob(
                    job_id=j.job_id, state=j.state,
                    started_at=j.started_at, finished_at=j.finished_at,
                    output_tail=j.output,
                )
                for j in self.jobs.for_run(record.run_id)
            ]
            return self._cache_cs(record, self._relabel_if_provisioning(snap, record))
        snap = snapshot_mod.fetch_snapshot(
            client, record, include_artifacts=include_artifacts, runtime_status=runtime_status,
        )
        # A challenge hosted on a worker shares the VM (and its tmux server) with
        # other challenges. Keep only this challenge's session + its subagents so
        # the snapshot doesn't leak sibling challenges' panes.
        if record.parent_worker_id:
            sess = self._slug_of(record)
            snap.panes = [p for p in snap.panes
                          if p.session == sess or p.session.startswith(f"subagent-{sess}")]
        snap.subagents = self._subagents_from_snapshot(snap)
        return self._cache_cs(record, self._relabel_if_provisioning(snap, record))

    def _cache_cs(self, record: RunRecord, snap):
        if snap.challenge_state and record.run_id:
            self._cs_cache[record.run_id] = snap.challenge_state
        return snap

    def cached_challenge_state(self, run_id: str):
        """Last-known challenge_state for a run (or None) — no control-plane call.
        Powers the instant sidebar pills in /runs."""
        return self._cs_cache.get(run_id)

    # A run that's still coming up (no control plane yet, or control plane up but
    # the agent/tmux not launched) otherwise derives to "Stopped"/"Halted" — both
    # of which read as failure. While it's young (or a spawn job is still working
    # it), that's really "Provisioning", not a dead run.
    PROVISION_WINDOW_SEC = 1200  # 20 min — covers slow image pull/load

    def _relabel_if_provisioning(self, snap, record: RunRecord):
        cs = snap.challenge_state
        if not cs or cs.state not in ("stopped", "halted"):
            return snap  # solved/blocked/progressing/stalled are accurate as-is
        if snap.panes:
            # The agent's tmux is already up (it launched, then exited or dropped
            # to a shell). That's a real halt, not "still provisioning" — don't
            # mask it, even while the run is young.
            return snap
        jobs = self.jobs.for_run(record.run_id)
        if any(j.state in ("queued", "running") for j in jobs):
            snap.challenge_state = self._provisioning_cs(cs)
            return snap
        # A spawn job that exited with an error means `ctfvm start` FAILED (e.g.
        # the toolbox image pull 404'd) — surface that, don't keep showing
        # "Provisioning" until it ages out. The job output holds the reason.
        failed = next((j for j in jobs if j.state == "error"), None)
        if failed:
            reason = _provision_failure_reason(failed.output)
            snap.error = f"Provisioning failed: {reason}" if reason else (snap.error or "Provisioning failed.")
            snap.challenge_state = ChallengeState(
                state="failed", label="Failed",
                summary="Provisioning failed before the agent started — see the error.",
                last_activity_age_sec=cs.last_activity_age_sec,
                last_activity_label=cs.last_activity_label,
            )
            return snap
        # No spawn job to consult (e.g. a CLI start) — fall back to age: a young
        # unreachable run is still coming up, an old one is genuinely stopped.
        age = _run_age_seconds(record)
        if age is not None and age < self.PROVISION_WINDOW_SEC:
            snap.challenge_state = self._provisioning_cs(cs)
        return snap

    @staticmethod
    def _provisioning_cs(cs) -> ChallengeState:
        return ChallengeState(
            state="provisioning", label="Provisioning",
            summary="VM is starting up — control plane and agent are not ready yet.",
            last_activity_age_sec=cs.last_activity_age_sec,
            last_activity_label=cs.last_activity_label,
        )

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

    # --- upload (operator -> running run) ----------------------------------
    def upload(self, run_id: str, dest: str, body: bytes, *, mode: str = "",
               as_tar: bool = False, allow_abs: bool = False) -> UploadResult:
        """Push operator-supplied bytes to a running run. Default-confined to the
        run's workspace (``allow_abs`` is the explicit escape hatch); size-capped."""
        record = self._record(run_id)
        if not isinstance(body, (bytes, bytearray)) or len(body) == 0:
            raise ApiError("BAD_REQUEST", "upload body is empty", 400)
        if len(body) > MAX_UPLOAD_BYTES:
            raise ApiError("BAD_REQUEST", f"upload exceeds {MAX_UPLOAD_BYTES} byte limit", 413)
        try:
            target = confine_remote_path(record.remote_run_dir, dest, allow_abs=allow_abs)
        except ValueError as exc:
            raise ApiError("INVALID_PATH", str(exc), 400) from exc
        client = self._client(record)
        body = bytes(body)
        try:
            if as_tar:
                client.upload_tar(target, body)
            else:
                client.upload_file(target, body, mode=mode)
        except ControlPlaneError as exc:
            raise ApiError("REMOTE_ERROR", str(exc), 502) from exc
        return UploadResult(path=target, size_bytes=len(body), mode=mode, as_tar=as_tar)

    # --- workers -----------------------------------------------------------
    def spawn_workers(self, payload: dict) -> list[str]:
        """Provision N empty worker VMs (golden image, control plane up, no agent).
        Challenges are added later via add_challenge."""
        try:
            count = int(payload.get("count"))
        except (TypeError, ValueError):
            raise ApiError("BAD_REQUEST", "count is required (integer >= 1)", 400)
        if count < 1:
            raise ApiError("BAD_REQUEST", "count must be >= 1", 400)
        if count > MAX_SPAWN_JOBS:
            raise ApiError("BAD_REQUEST", f"count must be <= {MAX_SPAWN_JOBS}", 400)
        base = {k: v for k, v in payload.items() if k != "count"}
        specs = [dict(base) for _ in range(count)]
        self._assign_run_ids(specs)
        self._assign_worker_names(specs)
        job_ids: list[str] = []
        for spec in specs:
            try:
                job = self.jobs.submit(self._build_worker_start_cmd(spec))
            except JobLimitError as exc:
                raise ApiError("JOB_LIMIT", str(exc), 429) from exc
            job_ids.append(job.job_id)
        return job_ids

    def worker_start_commands(self, payload: dict) -> list[list[str]]:
        """Build the `ctfvm start --worker` commands without submitting them — for
        the CLI, which runs them synchronously rather than via the job tracker."""
        count = max(1, int(payload.get("count") or 1))
        base = {k: v for k, v in payload.items() if k != "count"}
        specs = [dict(base) for _ in range(count)]
        self._assign_run_ids(specs)
        self._assign_worker_names(specs)
        return [self._build_worker_start_cmd(s) for s in specs]

    def _assign_worker_names(self, specs: list[dict]) -> None:
        used: set[str] = set()
        try:
            for listing in self.registry.list_runs()[0]:
                nm = str(getattr(listing.record, "challenge_name", "") or "").strip()
                if nm:
                    used.add(nm)
        except Exception:
            pass
        i = 1
        for spec in specs:
            if spec.get("name"):
                used.add(str(spec["name"]))
                continue
            while f"worker-{i:02d}" in used:
                i += 1
            spec["name"] = f"worker-{i:02d}"
            used.add(spec["name"])
            i += 1

    @staticmethod
    def _build_worker_start_cmd(spec: dict) -> list[str]:
        settings = Settings()
        spec = dict(spec)
        for key in ("provider", "toolbox_variant", "timeout_min"):
            if not spec.get(key):
                resolved = settings.get(key)
                if resolved not in (None, ""):
                    spec[key] = resolved
        cmd = [str(ROOT / "scripts" / "ctfvm"), "start", "--worker"]
        flag_map = {
            "run_id": "--run-id", "name": "--name", "provider": "--provider",
            "zone": "--zone", "project": "--project", "machine_type": "--machine-type",
            "size_slug": "--size-slug", "toolbox_variant": "--toolbox-variant",
            "timeout_min": "--timeout-min",
        }
        for key, flag in flag_map.items():
            val = spec.get(key)
            if val not in (None, ""):
                cmd += [flag, str(val)]
        if spec.get("no_vpn"):
            cmd.append("--no-vpn")
        return cmd

    def _worker_record(self, worker_id: str) -> RunRecord:
        """Resolve a worker by run_id / instance / run_key, or by its display name
        (e.g. ``worker-01``) — the name is what `worker ls` shows."""
        rec = self.registry.resolve(worker_id)
        if not rec:
            for listing in self.registry.list_runs()[0]:
                if (listing.record.runner_type == RUNNER_WORKER
                        and listing.record.challenge_name == worker_id):
                    rec = listing.record
                    break
        if not rec:
            raise ApiError("NOT_FOUND", f"Worker not found: {worker_id}", 404)
        return rec

    def add_challenge(self, worker_id: str, payload: dict, archive: bytes = b"") -> dict:
        """Place a challenge on a worker: create its workspace, upload the
        challenge + agent config + (per-challenge) credentials, launch the agent
        in its own tmux session, and register a challenge run record sharing the
        worker's control endpoint. Control-plane only (Invariant 1)."""
        worker = self._worker_record(worker_id)
        if worker.runner_type != RUNNER_WORKER:
            raise ApiError("BAD_REQUEST", f"run {worker_id} is not a worker", 400)
        name = str(payload.get("name") or "").strip()
        if not name and payload.get("challenge_dir"):
            name = os.path.basename(str(payload["challenge_dir"]).rstrip("/"))
        existing = {self._slug_of(l.record) for l in self.registry.list_runs()[0]
                    if l.record.parent_worker_id == worker.run_id}
        slug = _unique_slug(_slugify(name or "challenge"), existing)
        workspace = f"{DEFAULT_REMOTE_RUN_DIR}/{slug}"
        session = f"{slug}:supervisor"
        backend = str(payload.get("agent_backend") or worker.agent_backend or DEFAULT_AGENT_BACKEND)
        # Build the SAME initial prompt a normal run gets (description + ideas +
        # the shared instructions), fold in flag format exactly like spawn does,
        # then remap /workspace -> the dockerless workspace.
        from ..commands import build_challenge_prompt
        description = str(payload.get("description") or "")
        flag_format = str(payload.get("flag_format") or "").strip()
        if flag_format:
            description = (f"{description}\n\n" if description.strip() else "") + f"Expected flag format: {flag_format}"
        prompt = build_challenge_prompt(description, str(payload.get("ideas") or "")).replace("/workspace", workspace)
        client = self._client(worker)
        try:
            client.exec(
                f"sudo -u ctf mkdir -p {workspace}/challenge {workspace}/logs "
                f"{workspace}/artifacts {workspace}/agent {workspace}/.codex/skills", timeout=60)
            if archive:
                client.upload_tar(f"{workspace}/challenge", bytes(archive))
            client.upload_file(f"{workspace}/challenge_prompt.txt", prompt.encode("utf-8"), mode="0644")
            # ship the same prompts/ + skills the normal run flow does
            self._upload_local_dir(client, ROOT / "prompts", f"{workspace}/prompts")
            self._upload_local_dir(client, ROOT / "skills", f"{workspace}/.codex/skills")
            self._stage_agent_remote(client, workspace, backend, payload)
            self._launch_challenge_session(client, workspace, slug, session)
        except ControlPlaneError as exc:
            raise ApiError("REMOTE_ERROR", str(exc), 502) from exc
        rec = self._make_challenge_record(worker, slug, workspace, session, backend)
        self.registry.save_record(rec)
        return {"run_id": rec.run_id, "slug": slug, "tmux_session": session,
                "workspace": workspace, "parent_worker_id": worker.run_id}

    @staticmethod
    def _upload_local_dir(client, local_dir, remote_dir: str) -> None:
        """Tar a local dir's contents and extract it into remote_dir over the
        control plane (no-op if the dir is missing). Used to ship prompts/ + skills/."""
        import io
        import tarfile
        from pathlib import Path as _Path
        local_dir = _Path(local_dir)
        if not local_dir.is_dir():
            return
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            tf.add(str(local_dir), arcname=".")
        client.upload_tar(remote_dir, buf.getvalue())

    def _stage_agent_remote(self, client, workspace: str, backend_name: str, payload: dict) -> None:
        """Render the agent config + inject this challenge's credentials into its
        workspace over the control plane. Creds are injected here, at challenge
        launch — never baked into the image (see docs/PROPOSAL §1.5)."""
        from ..secretstore import get_profile
        account = str(payload.get("account") or "").strip()
        profile = get_profile(account) if account else None
        if account and not profile:
            raise ApiError("BAD_REQUEST", f"unknown credential profile: {account!r}", 400)
        settings = Settings(profile=profile)
        backend = build_agent_backend(backend_name)
        spec = backend.build_spec(
            model=str(payload.get("model") or ""),
            reasoning_effort=str(payload.get("reasoning_effort") or ""),
            ida_mcp_url=settings.get("ida_mcp_url", default=""))
        auth = backend.materialize_auth(settings)
        if not auth.authenticated:
            raise ApiError("AUTH_REQUIRED", auth.note or "no agent credentials", 400)
        from pathlib import Path as _Path
        # Rendered config assumes the container workspace (/workspace, where
        # HOME lived in Docker). Dockerless workers run from the per-challenge
        # dir, so remap /workspace -> the real workspace (fixes e.g. the codex
        # trusted-projects path so the agent doesn't block on a trust prompt).
        for sf in backend.render_config(spec):
            content = sf.content.replace("/workspace", workspace)
            client.upload_file(f"{workspace}/{sf.remote_relpath}", content.encode("utf-8"), mode=sf.mode or "")
        for af in auth.local_files:  # e.g. Codex auth.json — per-challenge cred injection
            client.upload_file(f"{workspace}/{af.remote_relpath}", _Path(af.local_path).read_bytes(), mode=af.mode or "")
        client.upload_file(f"{workspace}/agent/backend", (backend.name + "\n").encode("utf-8"))
        client.upload_file(f"{workspace}/agent/launch.cmd", (backend.supervisor_launch_cmd(spec) + "\n").encode("utf-8"))
        env_lines = "".join(f"{k}={v}\n" for k, v in sorted(auth.container_env.items()))
        client.upload_file(f"{workspace}/agent/container.env", env_lines.encode("utf-8"), mode="600")

    def _launch_challenge_session(self, client, workspace: str, slug: str, session: str) -> None:
        from pathlib import Path as _Path
        for fname in ("supervisor.sh", "subagent-tmux-bridge.sh"):
            p = ROOT / "runner" / fname
            try:
                client.upload_file(f"{workspace}/{fname}", _Path(p).read_bytes(), mode="0755")
            except OSError:
                pass
        client.exec(f"sudo chown -R ctf:ctf {workspace}", timeout=60)
        sess = session.split(":")[0]
        sup = f"{workspace}/supervisor.sh --run-dir {workspace} --prompt-file {workspace}/challenge_prompt.txt"
        client.exec(
            f"sudo -u ctf bash -lc 'tmux has-session -t {sess} 2>/dev/null && tmux kill-session -t {sess} || true; "
            f"tmux new-session -d -s {sess} -n supervisor \"{sup}\"'",
            timeout=60)

    def _make_challenge_record(self, worker: RunRecord, slug: str, workspace: str,
                               session: str, backend: str) -> RunRecord:
        # ip is left blank so the challenge does NOT dedup against the worker on the
        # shared-IP identity key; control_host carries the endpoint instead.
        return RunRecord(
            provider=worker.provider, run_id=self._fresh_run_id(),
            instance=f"{worker.instance}-{slug}" if worker.instance else slug,
            zone=worker.zone, project=worker.project, ip="",
            started_at=_iso_now(), challenge_name=slug,
            remote_run_dir=workspace, agent_backend=backend,
            control_scheme=worker.control_scheme,
            control_host=worker.control_host or worker.ip,
            control_port=worker.control_port,
            control_user=worker.control_user, control_password=worker.control_password,
            runner_type=RUNNER_CHALLENGE, parent_worker_id=worker.run_id, tmux_session=session,
        )

    def _fresh_run_id(self) -> str:
        spec = [{}]
        self._assign_run_ids(spec)
        return spec[0]["run_id"]

    def remove_challenge(self, worker_id: str, slug: str) -> dict:
        """Stop a challenge's agent session on its worker and drop its run record.
        Leaves the workspace on disk (operator can re-add)."""
        worker = self._worker_record(worker_id)
        client = self._client(worker)
        sess = _slugify(slug)
        try:
            client.exec(f"sudo -u ctf bash -lc 'tmux kill-session -t {sess} 2>/dev/null || true'", timeout=30)
        except ControlPlaneError as exc:
            raise ApiError("REMOTE_ERROR", str(exc), 502) from exc
        removed = None
        for listing in self.registry.list_runs()[0]:
            r = listing.record
            if r.parent_worker_id == worker.run_id and self._slug_of(r) == sess:
                removed = r.run_id
                self.registry.delete_record(r.run_id)
                break
        return {"worker_id": worker.run_id, "slug": sess, "removed_run_id": removed}

    def list_workers(self) -> list[dict]:
        """Group runs into workers + the challenges hosted on each. Standalone
        (non-worker, non-hosted) runs are not included."""
        listings, _ = self.registry.list_runs()
        workers = {l.record.run_id: {"worker": l, "challenges": []}
                   for l in listings if l.record.runner_type == RUNNER_WORKER}
        for l in listings:
            pw = l.record.parent_worker_id
            if pw and pw in workers:
                workers[pw]["challenges"].append(l)
        return list(workers.values())

    @staticmethod
    def _slug_of(record: RunRecord) -> str:
        return str(record.tmux_session or "").split(":")[0] or _slugify(record.challenge_name or "")

    # --- spawn / jobs ------------------------------------------------------
    def agents(self) -> list[dict]:
        return agents_info()

    # form fields that can be pre-filled from configured defaults (.env / env /
    # config.json). schema-only defaults stay blank so the form shows the
    # built-in default as a placeholder rather than a redundant explicit value.
    _SPAWN_DEFAULT_KEYS = (
        "agent_backend", "model", "reasoning_effort", "provider",
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

    def vpn_status(self, run_id: str) -> dict:
        """Local VPN state for a run. VPN bring-up is a *local* privileged step
        (`ctfvm vpn up`, needs sudo on the operator's machine), so the UI reports
        status + the command to run rather than doing it itself."""
        up_cmd = f"./scripts/ctfvm vpn --run-id {run_id} up"
        # Was VPN requested for this run? (vs --no-vpn). Default True for older
        # runs / VPN-on default, so the UI errs toward prompting.
        raw = self.registry.load_raw(run_id) or {}
        requested = str(raw.get("vpn_requested", "1")).strip() not in ("0", "false", "False", "")
        state = None
        vpn_dir = ROOT / ".ctfvm" / "vpn"
        if vpn_dir.is_dir():
            for sub in sorted(vpn_dir.iterdir()):
                if not sub.is_dir():
                    continue
                for f in sub.glob("*.json"):
                    try:
                        data = _json.loads(f.read_text())
                    except Exception:
                        continue
                    if str(data.get("run_id") or "") == run_id:
                        state = data
                        break
                if state:
                    break
        if not state:
            return {"connected": False, "requested": requested, "up_command": up_cmd}
        return {
            "connected": True,
            "requested": requested,
            "interface": state.get("local_interface") or state.get("name"),
            "local_ip": state.get("local_ip"),
            "remote_ip": state.get("remote_ip"),
            "cidrs": state.get("local_cidrs") or [],
            "nat": bool(state.get("nat_enabled")),
            "up_command": up_cmd,
            "down_command": f"./scripts/ctfvm vpn --run-id {run_id} down",
        }

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
        # Assign a unique run_id to every run up front. `ctfvm start` would
        # otherwise mint `date +%Y%m%d-%H%M%S` itself, and a folder deploy
        # launches its children in the same second -> identical run_ids ->
        # colliding `.ctfvm/runs/<run_id>.json` (only the instance-keyed copy
        # survives), so the runs can't be selected/VPN'd individually.
        self._assign_run_ids(expanded)
        self._assign_names(expanded)
        job_ids: list[str] = []
        for spec in expanded:
            cmd = self._build_start_cmd(spec)
            try:
                job = self.jobs.submit(cmd)
            except JobLimitError as exc:
                raise ApiError("JOB_LIMIT", str(exc), 429) from exc
            job_ids.append(job.job_id)
        return job_ids

    def _assign_run_ids(self, specs: list[dict], *, now=None) -> None:
        """Give each spec a distinct run_id (``YYYYmmdd-HHMMSS``), preserving any
        caller-supplied one. Distinct seconds per item keep the rigid run_id
        format that identity/state/selectors all assume. Dedup is GLOBAL — against
        this server's already-assigned ids (in-flight runs whose state isn't
        written yet) and the registry — so two separate batches in the same
        second (e.g. the same folder for codex then claude) never collide, which
        would otherwise give them the same instance name + clobber each other's
        state (and lose the codex run's challenge_name)."""
        from datetime import datetime, timedelta, timezone
        base = (now or (lambda: datetime.now(timezone.utc)))()
        used: set[str] = set(self._assigned_run_ids)
        try:
            for listing in self.registry.list_runs()[0]:
                if listing.record.run_id:
                    used.add(listing.record.run_id)
        except Exception:  # never block a spawn on a listing hiccup
            pass
        used |= {str(s["run_id"]) for s in specs if s.get("run_id")}
        offset = 0
        for spec in specs:
            if spec.get("run_id"):
                self._assigned_run_ids.add(str(spec["run_id"]))
                continue
            rid = (base + timedelta(seconds=offset)).strftime("%Y%m%d-%H%M%S")
            while rid in used:
                offset += 1
                rid = (base + timedelta(seconds=offset)).strftime("%Y%m%d-%H%M%S")
            spec["run_id"] = rid
            used.add(rid)
            self._assigned_run_ids.add(rid)
            offset += 1

    def _assign_names(self, specs: list[dict]) -> None:
        """Give each run a display name = its challenge folder name. On collision
        (within this batch or against an existing run), prefix with the parent
        folder to disambiguate — e.g. ``web/01`` and ``pwn/01`` -> ``web-01`` /
        ``pwn-01``. Preserves any caller-supplied name."""
        used: set[str] = set()
        try:
            for listing in self.registry.list_runs()[0]:
                nm = str(getattr(listing.record, "challenge_name", "") or "").strip()
                if nm:
                    used.add(nm)
        except Exception:  # naming is best-effort; never block a spawn on it
            pass
        for spec in specs:
            if spec.get("name"):
                used.add(str(spec["name"]))
                continue
            cdir = str(spec.get("challenge_dir") or "").strip().rstrip("/")
            if not cdir:
                continue
            base = os.path.basename(cdir) or "run"
            name = base
            if name in used:
                parent = os.path.basename(os.path.dirname(cdir))
                name = f"{parent}-{base}" if parent else base
                stem, i = name, 2
                while name in used:  # still colliding -> numeric suffix
                    name = f"{stem}-{i}"
                    i += 1
            spec["name"] = name
            used.add(name)

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
        for key in ("agent_backend", "model", "reasoning_effort", "provider", "toolbox_variant", "timeout_min"):
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
            "run_id": "--run-id", "name": "--name",
            "provider": "--provider", "agent_backend": "--agent", "zone": "--zone",
            "project": "--project", "description": "--desc", "ideas": "--ideas",
            "model": "--model", "reasoning_effort": "--reasoning-effort",
            "machine_type": "--machine-type", "size_slug": "--size-slug",
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
        if spec.get("no_auth_sync"):
            cmd.append("--no-auth-sync")
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


def _slugify(name: str) -> str:
    """Filesystem/tmux-safe slug: lowercase, [a-z0-9-], collapsed dashes."""
    import re
    s = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower()).strip("-")
    return s or "challenge"


def _unique_slug(slug: str, existing: set) -> str:
    if slug not in existing:
        return slug
    i = 2
    while f"{slug}-{i}" in existing:
        i += 1
    return f"{slug}-{i}"


def _provision_failure_reason(output: str) -> str:
    """Pull a one-line reason out of a failed spawn job's output — the last line
    that looks like an error, else the last non-empty line."""
    lines = [ln.strip() for ln in (output or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    markers = ("error", "failed", "not configured", "denied", "no such", "not found", "timed out")
    for ln in reversed(lines):
        if any(m in ln.lower() for m in markers):
            return ln[:300]
    return lines[-1][:300]


def _run_age_seconds(record: RunRecord):
    """Seconds since the run started, from started_at (ISO) or the run_id
    timestamp. None if neither parses — callers treat None as 'not young'."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    started = str(record.started_at or "").strip()
    for value, fmt in ((started, "%Y-%m-%dT%H:%M:%SZ"), (str(record.run_id or "").strip(), "%Y%m%d-%H%M%S")):
        if not value:
            continue
        try:
            dt = datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
            return max((now - dt).total_seconds(), 0.0)
        except ValueError:
            continue
    return None
