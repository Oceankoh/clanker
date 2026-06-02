"""Read-only commands ported from the bash CLI (Phase 2 strangler step).

These operate purely over the shared core: RunRegistry for state, the
CloudProvider layer for cloud status/discovery, and the typed control client for
file transfer. The bash ``scripts/ctfvm`` resolves the target (its selector /
picker logic stays in bash for now) and delegates the command body here.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import RUNS_DIR, STATE_FILE, load_json, state_valid
from .controlclient import ControlPlaneClient, ControlPlaneError
from .identity import normalize_instance_name, provider_from_state
from .models import RunRecord
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
