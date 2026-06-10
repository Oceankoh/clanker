"""Artifact preview / download / bundle — control-plane only.

Paths are validated with ``sanitize_relpath`` (R1) before any remote call. Full
downloads use the control plane's ``/files/download`` directly; previews read a
capped prefix via a small remote Python program (JSON envelope, no markers).
"""

from __future__ import annotations

import base64
import json
import mimetypes
import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath

from .config import MAX_ARTIFACT_PREVIEW_BYTES, RUN_BUNDLE_MAX_BYTES
from .controlclient import ControlPlaneClient, ControlPlaneError
from .models import DEFAULT_REMOTE_RUN_DIR
from .remote import remote_python
from .validation import sanitize_relpath

_TEXT_MIMES = {"application/json", "application/xml", "application/x-sh", "application/javascript"}


class ArtifactError(ValueError):
    """Invalid artifact request (bad path / not found) — maps to 400/404."""


@dataclass
class ArtifactPreview:
    relpath: str
    mime: str
    size: int
    truncated: bool
    is_text: bool
    is_image: bool
    content: str = ""          # text, when is_text
    image_data_url: str = ""   # data URL, when is_image
    b64: str = ""              # raw previewed bytes (base64), for hex view of binaries


@dataclass
class ArtifactDownload:
    filename: str
    mime: str
    content: bytes


def _remote_abspath(remote_run_dir: str, relpath: str) -> tuple[str, str, str]:
    clean = sanitize_relpath(relpath)
    if not clean:
        raise ArtifactError("Invalid artifact path")
    run = remote_run_dir.rstrip("/")
    return clean, f"{run}/{clean}", f"{run}/artifacts"


# Both bodies run as the `ctf` user and resolve realpath, refusing to read
# anything whose resolved path escapes the artifacts dir (blocks symlink-out
# traversal that sanitize_relpath's string check can't catch).
_PREVIEW_BODY = r'''
import base64, json, os, subprocess

out = {"ok": False}
prefix = os.path.realpath(ART_DIR)
rp = os.path.realpath(PATH)
if (rp == prefix or rp.startswith(prefix + os.sep)) and os.path.isfile(rp):
    try:
        mime = subprocess.run(["file", "-b", "--mime-type", rp], capture_output=True, text=True).stdout.strip()
    except Exception:
        mime = ""
    size = os.path.getsize(rp)
    with open(rp, "rb") as f:
        data = f.read(CAP + 1)
    truncated = len(data) > CAP
    out = {"ok": True, "mime": mime or "application/octet-stream", "size": size,
           "truncated": truncated, "b64": base64.b64encode(data[:CAP]).decode("ascii")}
print(json.dumps(out))
'''

_DOWNLOAD_BODY = r'''
import base64, json, os, subprocess

out = {"ok": False}
prefix = os.path.realpath(ART_DIR)
rp = os.path.realpath(PATH)
if (rp == prefix or rp.startswith(prefix + os.sep)) and os.path.isfile(rp):
    try:
        mime = subprocess.run(["file", "-b", "--mime-type", rp], capture_output=True, text=True).stdout.strip()
    except Exception:
        mime = ""
    with open(rp, "rb") as f:
        out = {"ok": True, "mime": mime or "application/octet-stream",
               "b64": base64.b64encode(f.read()).decode("ascii")}
print(json.dumps(out))
'''


def preview_artifact(
    client: ControlPlaneClient,
    remote_run_dir: str,
    relpath: str,
    *,
    cap: int = MAX_ARTIFACT_PREVIEW_BYTES,
    timeout: int = 25,
) -> ArtifactPreview:
    clean, abspath, art_dir = _remote_abspath(remote_run_dir, relpath)
    header = f"PATH = {abspath!r}\nART_DIR = {art_dir!r}\nCAP = {int(cap)}\n"
    result = client.exec(remote_python(header + _PREVIEW_BODY), timeout=timeout)
    if not result.ok:
        raise ControlPlaneError(result.stderr_text().strip() or "Failed to fetch artifact")
    try:
        data = json.loads(result.stdout.decode("utf-8") or "{}")
    except Exception:
        raise ControlPlaneError("Unexpected artifact preview format")
    if not data.get("ok"):
        raise ArtifactError("Artifact not found")

    raw = base64.b64decode((data.get("b64") or "").encode("ascii"), validate=False)
    mime = str(data.get("mime") or "application/octet-stream")
    guessed, _ = mimetypes.guess_type(clean)
    effective = mime if mime and mime != "application/octet-stream" else (guessed or mime)
    is_text = effective.startswith("text/") or effective in _TEXT_MIMES
    is_image = effective.startswith("image/")
    raw_b64 = str(data.get("b64") or "")
    return ArtifactPreview(
        relpath=clean, mime=effective, size=int(data.get("size") or 0),
        truncated=bool(data.get("truncated")), is_text=is_text, is_image=is_image,
        content=raw.decode("utf-8", "replace") if is_text else f"[binary file: {effective}, {data.get('size', 0)} bytes]",
        image_data_url=(f"data:{effective};base64,{raw_b64}" if (is_image and raw) else ""),
        b64=("" if is_text else raw_b64),  # bytes for hex view of binaries (omit for text)
    )


def download_artifact(
    client: ControlPlaneClient,
    remote_run_dir: str,
    relpath: str,
    *,
    timeout: int = 60,
) -> ArtifactDownload:
    clean, abspath, art_dir = _remote_abspath(remote_run_dir, relpath)
    # Read as the ctf user (not root) with a realpath guard, so a symlink under
    # artifacts/ can't be followed out of the run directory.
    header = f"PATH = {abspath!r}\nART_DIR = {art_dir!r}\n"
    result = client.exec(remote_python(header + _DOWNLOAD_BODY), timeout=timeout)
    if not result.ok:
        raise ControlPlaneError(result.stderr_text().strip() or "Failed to download artifact")
    try:
        data = json.loads(result.stdout.decode("utf-8") or "{}")
    except Exception:
        raise ControlPlaneError("Unexpected artifact download format")
    if not data.get("ok"):
        raise ArtifactError("Artifact not found")
    content = base64.b64decode((data.get("b64") or "").encode("ascii"), validate=False)
    mime = str(data.get("mime") or "application/octet-stream")
    guessed, _ = mimetypes.guess_type(clean)
    effective = mime if mime and mime != "application/octet-stream" else (guessed or mime)
    return ArtifactDownload(filename=PurePosixPath(clean).name, mime=effective, content=content)


def build_bundle(
    client: ControlPlaneClient,
    run_id: str,
    run_dir: str = DEFAULT_REMOTE_RUN_DIR,
    *,
    max_bytes: int = RUN_BUNDLE_MAX_BYTES,
    timeout: int = 60,
) -> ArtifactDownload:
    """Tar findings + artifacts + logs server-side, then download it.

    ``run_dir`` is the run's remote dir; worker-hosted challenges live under
    ``/home/ctf/run/<slug>``, so bundling must cd there — not the shared parent —
    or it would tar sibling challenges instead of this one's artifacts.
    """
    cd_dir = (str(run_dir or "").strip() or DEFAULT_REMOTE_RUN_DIR).rstrip("/")
    if "'" in cd_dir:
        raise ArtifactError("Invalid run dir")
    remote_tar = "/tmp/ctfvm-bundle-$$.tar.gz"
    make = (
        "sudo -u ctf bash -lc '"
        f'cd {cd_dir} || exit 1; tmp={remote_tar}; '
        'tar -czf "$tmp" findings.md artifacts logs challenge_prompt.txt 2>/dev/null '
        '|| tar -czf "$tmp" artifacts logs challenge_prompt.txt; '
        f'size=$(wc -c < "$tmp" | tr -d " "); '
        f'if [ "$size" -gt {int(max_bytes)} ]; then rm -f "$tmp"; echo TOO_LARGE; exit 0; fi; '
        'echo "$tmp"'
        "'"
    )
    result = client.exec(make, timeout=timeout)
    if not result.ok:
        raise ControlPlaneError(result.stderr_text().strip() or "bundle failed")
    line = result.stdout_text().strip().splitlines()[-1] if result.stdout_text().strip() else ""
    if line == "TOO_LARGE":
        raise ArtifactError("bundle too large")
    if not line.startswith("/tmp/"):
        raise ControlPlaneError("Unexpected bundle output")
    try:
        content = client.download_file(line, timeout=timeout)
    finally:
        try:
            client.exec(f"sudo -u ctf bash -lc {shlex.quote('rm -f ' + shlex.quote(line))}", timeout=15)
        except ControlPlaneError:
            pass
    rid = (run_id or "run").strip() or "run"
    return ArtifactDownload(filename=f"ctfvm-{rid}.tar.gz", mime="application/gzip", content=content)
