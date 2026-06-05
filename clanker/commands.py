"""Read-only commands ported from the bash CLI (Phase 2 strangler step).

These operate purely over the shared core: RunRegistry for state, the
CloudProvider layer for cloud status/discovery, and the typed control client for
file transfer. The bash ``scripts/ctfvm`` resolves the target (its selector /
picker logic stays in bash for now) and delegates the command body here.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .agents import build_agent_backend, list_backends
from .config import RUNS_DIR, STATE_FILE, Settings, load_json, state_valid
from .controlclient import ControlPlaneClient, ControlPlaneError
from .identity import normalize_instance_name, provider_from_state
from .models import RunRecord
from .secretstore import get_profile, get_secret, list_profiles, set_profile, set_secret
from .providers.base import (
    PROVIDER_LABELS,
    PROVIDER_LOCATION_LABELS,
    PROVIDER_SCOPE_LABELS,
    CloudProviderRegistry,
)
from .state import RunRegistry

EXEC_TIMEOUT = int(os.environ.get("CTFVM_CONTROL_TIMEOUT_SEC", "1800") or "1800")
DOWNLOAD_TIMEOUT = int(os.environ.get("CTFVM_CONTROL_DOWNLOAD_TIMEOUT_SEC", "600") or "600")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _resolve(registry: RunRegistry, *, run_id: str = "", instance: str = "") -> RunRecord | None:
    for selector in (run_id, instance):
        selector = (selector or "").strip()
        if not selector:
            continue
        record = registry.resolve(selector)
        if record:
            return record
    if not run_id and not instance:
        return registry.load_state()
    return None


def _client_or_error(record: RunRecord) -> ControlPlaneClient:
    try:
        return ControlPlaneClient.from_run(record)
    except ControlPlaneError as exc:
        raise ControlPlaneError(
            f"{exc}. Use a break-glass command (attach/shell) for runs without a control plane."
        ) from exc


def _extract_tar(archive: Path, dest: Path, *, gzip: bool) -> None:
    flag = "-xzf" if gzip else "-xf"
    subprocess.run(["tar", "-C", str(dest), flag, str(archive)], check=True)


def _write_staged(staging: Path, relpath: str, content: str, mode: str = "") -> None:
    dest = staging / relpath
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content)
    if mode:
        os.chmod(dest, int(mode, 8))


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------

def _extract_setup_token(stdout: str) -> str:
    """Pull the OAuth token out of `claude setup-token` output (best effort)."""
    for line in reversed([l.strip() for l in stdout.splitlines() if l.strip()]):
        if line.startswith("sk-ant-") or line.startswith("oauth"):
            return line
        if len(line) > 40 and " " not in line and not line.lower().startswith("http"):
            return line
    return ""


def cmd_auth_claude(token: str = "", name: str = "") -> int:
    token = (token or "").strip()
    if not token:
        try:
            proc = subprocess.run(["claude", "setup-token"], stdout=subprocess.PIPE, text=True)
        except FileNotFoundError:
            sys.stderr.write(
                "claude CLI not found. Install Claude Code, then either run\n"
                "  claude setup-token\n"
                "and pass it via `clanker auth claude --token <token>`.\n"
            )
            return 1
        if proc.returncode != 0:
            sys.stderr.write("`claude setup-token` failed; re-run it and pass `--token <token>`.\n")
            return 1
        token = _extract_setup_token(proc.stdout or "")
        if not token:
            sys.stderr.write(
                "Could not parse a token from `claude setup-token` output;\n"
                "copy it and run `clanker auth claude --token <token>`.\n"
            )
            return 1
    name = (name or "").strip()
    if name:
        set_profile(name, {"backend": "claude-code", "claude_oauth_token": token})
        print(f"Stored Claude OAuth token in profile '{name}' (.ctfvm/secrets.json, 0600).")
    else:
        set_secret("claude_oauth_token", token)
        print("Stored Claude OAuth token in .ctfvm/secrets.json (0600).")
    return 0


def cmd_auth_codex(name: str, *, api_key: str = "", codex_home: str = "") -> int:
    name = (name or "").strip()
    if not name:
        sys.stderr.write("`auth codex` requires --name <profile>.\n")
        return 2
    api_key = (api_key or "").strip()
    codex_home = (codex_home or "").strip()
    if not api_key and not codex_home:
        sys.stderr.write(
            "Provide one of --api-key <OPENAI_API_KEY> or --codex-home <dir with this account's auth.json>.\n"
        )
        return 2
    profile: dict = {"backend": "codex"}
    if api_key:
        profile["openai_api_key"] = api_key
        profile["no_auth_sync"] = "1"
    if codex_home:
        profile["codex_home"] = codex_home
    set_profile(name, profile)
    how = "API key" if api_key else f"session dir {codex_home}"
    print(f"Stored Codex profile '{name}' ({how}).")
    return 0


def agents_info(settings: Settings | None = None) -> list[dict]:
    """Structured view of the registered agent backends + readiness — powers
    `clanker agents` and the `/api/v1/agents` endpoint (spawn form)."""
    settings = settings or Settings()
    out = []
    for be in list_backends():
        auth = be.materialize_auth(settings)
        out.append({
            "name": be.name,
            "display_name": be.display_name,
            "default_model": be.default_model,
            "cli": be.cli_binary,
            "cli_local": bool(shutil.which(be.cli_binary)) if be.cli_binary else False,
            "authenticated": bool(auth.authenticated),
            "auth_note": auth.note,
        })
    return out


def cmd_agents(settings: Settings | None = None) -> int:
    for a in agents_info(settings):
        model = a["default_model"] or "(backend default)"
        auth = "ready" if a["authenticated"] else "needs setup"
        cli = "✓" if a["cli_local"] else "·"
        print(f"{a['name']:<13} {a['display_name']:<14} model={model:<18} cli-local={cli}  auth={auth}")
        if not a["authenticated"] and a["auth_note"]:
            print(f"    -> {a['auth_note']}")
    return 0


def discover_challenges(root: str) -> list[dict]:
    """Each immediate subfolder of ``root`` is a challenge (skips dotfiles).
    Picks up description.txt / ideas.txt if present."""
    base = Path(root).expanduser()
    if not base.is_dir():
        return []
    out = []
    for child in sorted(base.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        out.append({
            "challenge_dir": str(child),
            "name": child.name,
            "description": _read_text(child / "description.txt"),
            "ideas": _read_text(child / "ideas.txt"),
        })
    return out


def _read_text(path: Path) -> str:
    try:
        return path.read_text().strip()
    except Exception:
        return ""


def cmd_fanout(root: str, *, provider: str = "", agent: str = "", model: str = "",
               reasoning_effort: str = "", service=None, wait: bool = True, sleep=None) -> int:
    """Deploy a folder of challenges — one VM per immediate subfolder."""
    challenges = discover_challenges(root)
    if not challenges:
        sys.stderr.write(f"No challenge subfolders under {root}\n")
        return 1
    print(f"Found {len(challenges)} challenge(s): {', '.join(c['name'] for c in challenges)}")

    from .server.service import UiService  # lazy: avoids a server<->commands import cycle
    svc = service or UiService()
    payload: dict = {"challenge_root": str(Path(root).expanduser())}
    if provider:
        payload["provider"] = provider
    if agent:
        payload["agent_backend"] = agent
    if model:
        payload["model"] = model
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort

    job_ids = svc.spawn(payload)
    print(f"Spawning {len(job_ids)} run(s): {', '.join(job_ids)}")
    if not wait:
        return 0

    # jobs run as daemon threads, so block until they finish (the provisioning
    # subprocesses are tied to this process); print each as it completes.
    import time
    _sleep = sleep or time.sleep
    done: set[str] = set()
    while len(done) < len(job_ids):
        for jid in job_ids:
            j = svc.jobs.get(jid)
            if j and j.state in ("done", "error") and jid not in done:
                done.add(jid)
                print(f"  {jid}: {j.state}  run_id={j.run_id or '?'}")
        if len(done) < len(job_ids):
            _sleep(3)
    print("fan-out complete.")
    return 0


def cmd_config_profiles() -> int:
    profiles = list_profiles()
    if not profiles:
        print("(no credential profiles; create with `clanker auth claude|codex --name <n>`)")
        return 0
    for name in sorted(profiles):
        p = profiles[name]
        backend = p.get("backend", "?")
        creds = ", ".join(k for k in p if k != "backend") or "(none)"
        print(f"{name:<16} {backend:<12} {creds}")
    return 0


def cmd_config_show(*, settings: Settings | None = None) -> int:
    """Print the resolved effective config, grouped, with provenance. Secrets are
    shown only as set/unset."""
    settings = settings or Settings()
    rows = settings.effective()
    groups: dict[str, list] = {}
    for spec, value, source in rows:
        groups.setdefault(spec.group, []).append((spec, value, source))

    width = max((len(s.env_var) for s, _, _ in rows), default=10)
    for group in sorted(groups):
        print(f"[{group}]")
        for spec, value, source in groups[group]:
            if spec.secret:
                shown = "***set***" if value else "(unset)"
            else:
                shown = str(value) if value not in (None, "") else "(unset)"
            tag = "" if spec.consumed_by == "python" else f"  ·{spec.consumed_by}"
            print(f"  {spec.env_var:<{width}} = {shown:<28} [{source}]{tag}")
        print()
    print("source precedence: cli > .env > env > secret > .ctfvm/config.json > default")
    print("·bash/·both knobs are consumed by the bash CLI; edit .env to set them.")
    return 0


def _codex_session_present(settings: Settings) -> bool:
    """Whether a local Codex session (~/.codex/auth.json) exists. expanduser so a
    tilde'd CTFVM_CODEX_HOME (e.g. ``~/.codex`` from .env, where the parser keeps
    the literal tilde) resolves — matching the materialize_auth path used by
    `start`; otherwise this falsely reports "no local session"."""
    codex_home = Path(str(settings.get("codex_home", default=str(Path.home() / ".codex")))).expanduser()
    return (codex_home / "auth.json").exists()


def cmd_auth_show() -> int:
    settings = Settings()
    codex_ok = _codex_session_present(settings)
    claude_token = bool(
        get_secret("claude_oauth_token")
        or settings.get("claude_oauth_token", env_var="CLAUDE_CODE_OAUTH_TOKEN", default="")
    )
    claude_key = bool(settings.get("anthropic_api_key", env_var="ANTHROPIC_API_KEY", default=""))
    print(f"codex:        {'session present (~/.codex/auth.json)' if codex_ok else 'no local session (run `codex login`)'}")
    if claude_token:
        print("claude-code:  OAuth token stored")
    elif claude_key:
        print("claude-code:  ANTHROPIC_API_KEY set")
    else:
        print("claude-code:  no credentials (run `clanker auth claude`)")
    return 0


# ---------------------------------------------------------------------------
# stage-agent — materialize the full agent payload into a local staging dir
# ---------------------------------------------------------------------------

def cmd_stage_agent(
    backend_name: str,
    staging_dir: str,
    *,
    settings: Settings | None = None,
    model: str = "",
    reasoning_effort: str = "",
    ida_mcp_url: str = "",
    account: str = "",
) -> int:
    if settings is None:
        profile = get_profile(account) if account else None
        if account and not profile:
            sys.stderr.write(f"Unknown credential profile: {account!r}\n")
            return 2
        settings = Settings(profile=profile)
    backend = build_agent_backend(backend_name)
    ida = ida_mcp_url or settings.get("ida_mcp_url", env_var="CTFVM_DEFAULT_IDA_MCP_URL", default="")
    spec = backend.build_spec(model=model, reasoning_effort=reasoning_effort, ida_mcp_url=ida)
    auth = backend.materialize_auth(settings)

    staging = Path(staging_dir)
    staging.mkdir(parents=True, exist_ok=True)

    # 1. rendered config / MCP / role files
    for sf in backend.render_config(spec):
        _write_staged(staging, sf.remote_relpath, sf.content, sf.mode)

    # 2. local auth files copied verbatim (e.g. Codex session)
    for af in auth.local_files:
        dest = staging / af.remote_relpath
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(af.local_path, dest)
        if af.mode:
            os.chmod(dest, int(af.mode, 8))

    # 3. the agent/ control files consumed by supervisor.sh
    _write_staged(staging, "agent/backend", backend.name + "\n")
    _write_staged(staging, "agent/launch.cmd", backend.supervisor_launch_cmd(spec) + "\n")
    env_lines = "".join(f"{k}={v}\n" for k, v in sorted(auth.container_env.items()))
    _write_staged(staging, "agent/container.env", env_lines, mode="600")
    _write_staged(staging, "agent/wipe-paths.txt", "".join(p + "\n" for p in auth.wipe_remote_paths))

    if not auth.authenticated:
        sys.stderr.write(f"AUTH_REQUIRED: {auth.note}\n")
        return 3

    print(f"Staged {backend.display_name} payload into {staging.resolve()}")
    if auth.note:
        print(f"note: {auth.note}")
    return 0


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def cmd_status(
    registry: RunRegistry,
    providers: CloudProviderRegistry,
    *,
    run_id: str = "",
    instance: str = "",
) -> int:
    record = _resolve(registry, run_id=run_id, instance=instance)
    if not record:
        sys.stderr.write("No matching run found.\n")
        return 1

    # Look up raw state by a SPECIFIC selector — never load_raw("") which would
    # fall back to the current run and show a different run's extras.
    raw: dict = {}
    for selector in (record.run_id, record.instance):
        if selector:
            raw = registry.load_raw(selector) or {}
            if raw:
                break
    status = providers.get_status(record)

    scope = PROVIDER_SCOPE_LABELS.get(record.provider, "Project")
    location = PROVIDER_LOCATION_LABELS.get(record.provider, "Zone")
    label = PROVIDER_LABELS.get(record.provider, record.provider)

    print(f"Provider: {label}")
    print(f"Instance: {record.instance}")
    print(f"{scope}:  {record.project}")
    print(f"{location}:     {record.zone}")
    print(f"Status:   {status or 'UNKNOWN'}")
    print(f"IP:       {record.ip or 'unknown'}")

    if record.control_port or record.ip:
        print(f"Control:  {record.control_endpoint}")

    boot_image = str(raw.get("boot_image", "") or "")
    boot_family = str(raw.get("boot_image_family", "") or "")
    boot_project = str(raw.get("boot_image_project", "") or "")
    if boot_image:
        suffix = f" (project {boot_project})" if boot_project else ""
        print(f"Boot:     image {boot_image}{suffix}")
    elif boot_family:
        suffix = f" (project {boot_project})" if boot_project else ""
        print(f"Boot:     family {boot_family}{suffix}")

    toolbox_ref = str(raw.get("toolbox_image_ref", "") or "")
    toolbox_source = str(raw.get("toolbox_image_source", "") or "")
    if toolbox_ref:
        print(f"Toolbox:  {toolbox_ref}")
    elif toolbox_source:
        print(f"Toolbox:  {toolbox_source}")

    print(f"Run ID:   {record.run_id}")
    state_path = RUNS_DIR / f"{record.run_id}.json"
    print(f"State:    {state_path if state_path.exists() else STATE_FILE}")
    return 0


# ---------------------------------------------------------------------------
# cleanup-state
# ---------------------------------------------------------------------------

def cmd_cleanup_state(
    providers: CloudProviderRegistry,
    *,
    dry_run: bool = False,
    prune_non_running: bool = False,
) -> int:
    removed = kept = checked = 0
    promoted_current = False

    def cleanup_one(path: Path, label: str) -> None:
        nonlocal removed, kept, checked
        if not path.is_file():
            return
        state = load_json(path) or {}
        provider = provider_from_state(state)
        inst = str(state.get("instance", "") or "").strip()
        zone = str(state.get("zone", "") or "").strip()
        project = str(state.get("project", "") or "").strip()
        checked += 1

        remove_now = False
        reason = ""
        if not inst or not zone or not project:
            remove_now, reason = True, "incomplete metadata"
        else:
            try:
                backend = providers.get(provider)
                cli_ok = backend.cli_available
            except KeyError:
                # Intentional divergence from the bash port: a state file with an
                # unknown/typo provider is KEPT (treated as "CLI unavailable")
                # rather than deleted. Keeping un-classifiable state is safer than
                # destroying it during a partial upgrade; genuinely broken files
                # are still removed via the "incomplete metadata" path above.
                backend, cli_ok = None, False
            if not cli_ok:
                reason = "provider CLI unavailable"
            else:
                status = backend.get_status(RunRecord.from_mapping(state))
                if status == "":
                    remove_now, reason = True, "instance not found"
                elif prune_non_running and status not in ("RUNNING", "active"):
                    remove_now, reason = True, f"instance status={status}"

        if remove_now:
            removed += 1
            if dry_run:
                print(f"[dry-run] remove {label}: {path} ({reason})")
            else:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                print(f"Removed {label}: {path} ({reason})")
        else:
            kept += 1

    if RUNS_DIR.exists():
        for path in sorted(RUNS_DIR.glob("*.json")):
            cleanup_one(path, "run")
    if STATE_FILE.is_file():
        cleanup_one(STATE_FILE, "current")

    if not dry_run and not STATE_FILE.exists() and RUNS_DIR.exists():
        remaining = sorted(RUNS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if remaining:
            STATE_FILE.write_bytes(remaining[0].read_bytes())
            promoted_current = True
            print(f"Set current state from: {remaining[0]}")

    print(f"Cleanup complete: checked={checked}, removed={removed}, kept={kept}")
    if dry_run:
        print("Dry-run only: no files were changed.")
    if promoted_current:
        print("Current state was missing and has been restored from the newest remaining run.")
    return 0


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------

def cmd_fetch(
    registry: RunRegistry,
    *,
    run_id: str = "",
    instance: str = "",
    out_dir: str = "",
) -> int:
    record = _resolve(registry, run_id=run_id, instance=instance)
    if not record:
        sys.stderr.write("No matching run found.\n")
        return 1
    client = _client_or_error(record)

    rid = record.run_id or "manual"
    out = Path(out_dir or f"./ctfvm-output-{rid}")
    out.mkdir(parents=True, exist_ok=True)

    remote_tar = f"/tmp/ctfvm-{rid}.tar.gz"
    run_dir = record.remote_run_dir
    tar_cmd = (
        f"sudo -u ctf bash -lc 'cd {run_dir} && "
        f"tar -czf {remote_tar} findings.md artifacts logs inject.queue challenge_prompt.txt 2>/dev/null "
        f"|| tar -czf {remote_tar} artifacts logs challenge_prompt.txt'"
    )
    result = client.exec(tar_cmd, timeout=EXEC_TIMEOUT)
    if not result.ok:
        sys.stderr.write(
            f"remote archive step failed (rc={result.returncode}): "
            f"{result.stderr_text().strip() or 'see VM logs'}\n"
        )
        return 1
    payload = client.download_file(remote_tar, timeout=DOWNLOAD_TIMEOUT)
    archive = out / f"ctfvm-{rid}.tar.gz"
    archive.write_bytes(payload)
    try:
        client.exec(f"rm -f {remote_tar}", timeout=30)
    except ControlPlaneError:
        pass

    _extract_tar(archive, out, gzip=True)
    print(f"Fetched artifacts into: {out.resolve()}")
    return 0


# ---------------------------------------------------------------------------
# sync-down
# ---------------------------------------------------------------------------

def cmd_sync_down(
    registry: RunRegistry,
    *,
    run_id: str = "",
    instance: str = "",
    out_dir: str = "",
    remote_path: str = "/home/ctf/run/challenge",
) -> int:
    record = _resolve(registry, run_id=run_id, instance=instance)
    if not record:
        sys.stderr.write("No matching run found.\n")
        return 1
    client = _client_or_error(record)

    rid = record.run_id or "manual"
    out = Path(out_dir or f"./ctfvm-live-{rid}")
    out.mkdir(parents=True, exist_ok=True)

    # fail clearly if the remote path is missing
    check = client.exec(f"sudo -u ctf bash -lc 'test -d \"{remote_path}\"'", timeout=60)
    if not check.ok:
        sys.stderr.write(f"Remote path not found: {remote_path}\n")
        return 1

    suffix = os.getpid()
    remote_tar = f"/tmp/ctfvm-sync-down-{rid}-{suffix}.tar"
    result = client.exec(
        f"sudo -u ctf bash -lc 'tmp={remote_tar}; rm -f \"$tmp\"; "
        f"tar -C \"{remote_path}\" -cf \"$tmp\" .'",
        timeout=EXEC_TIMEOUT,
    )
    if not result.ok:
        sys.stderr.write(
            f"remote archive step failed (rc={result.returncode}): "
            f"{result.stderr_text().strip() or 'see VM logs'}\n"
        )
        return 1
    payload = client.download_file(remote_tar, timeout=DOWNLOAD_TIMEOUT)
    with tempfile.NamedTemporaryFile(dir=out, suffix=".tar", delete=False) as tf:
        tf.write(payload)
        archive = Path(tf.name)
    try:
        _extract_tar(archive, out, gzip=False)
    finally:
        archive.unlink(missing_ok=True)
    try:
        client.exec(f"rm -f {remote_tar}", timeout=30)
    except ControlPlaneError:
        pass

    print(f"Synced remote {remote_path} -> {out.resolve()}")
    return 0
